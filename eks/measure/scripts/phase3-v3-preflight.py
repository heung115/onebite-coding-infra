"""ALB reachability diagnostics and fail-closed pre-FIS gates for Phase 3 v3."""

from __future__ import annotations

import json
import ipaddress
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen
from urllib.parse import urlsplit


REGION = "ap-northeast-2"
CLUSTER = "onebite-eks-measure"
NAMESPACE = "measure"
APP = "backend-probe"
NODEPOOL = "phase3-spot"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def aws_json(recorder: Any, *argv: str, label: str) -> dict[str, Any]:
    result = recorder.aws(*argv)
    if result.returncode:
        raise RuntimeError(f"{label} failed with exit {result.returncode}: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc


def kubectl_json(recorder: Any, *args: str) -> dict[str, Any]:
    result = recorder.kubectl(*args)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"kubectl {' '.join(args)} failed")
    return json.loads(result.stdout)


def is_ready(resource: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in resource.get("status", {}).get("conditions", [])
    )


def instance_id(node: dict[str, Any]) -> str:
    provider_id = node.get("spec", {}).get("providerID", "")
    candidate = provider_id.rsplit("/", 1)[-1]
    if not candidate.startswith("i-"):
        raise RuntimeError(f"Node {node.get('metadata', {}).get('name')} has no EC2 provider ID")
    return candidate


def capture_controller_context(recorder: Any, trial: int, phase: str) -> None:
    recorder.write({"kind": "v3_controller_context_start", "trial": trial, "phase": phase, "observed_at_utc": utc_now()})
    recorder.kubectl(
        "logs", "deployment/aws-load-balancer-controller", "-n", "kube-system",
        "--all-containers=true", "--timestamps=true", "--since=30m",
    )
    recorder.kubectl("get", "events", "-A", "--sort-by=.metadata.creationTimestamp", "-o", "json")
    recorder.kubectl("describe", "ingress", APP, "-n", NAMESPACE)
    recorder.kubectl("get", "service", APP, "-n", NAMESPACE, "-o", "json")
    recorder.write({"kind": "v3_controller_context_end", "trial": trial, "phase": phase, "observed_at_utc": utc_now()})


def capture_security_group_rules(recorder: Any, group_ids: list[str], phase: str) -> dict[str, Any]:
    unique_ids = sorted(set(group_ids))
    if not unique_ids:
        raise RuntimeError("No ALB or worker security groups were found for the reachability gate")
    groups = aws_json(
        recorder, "ec2", "describe-security-groups", "--region", REGION,
        "--group-ids", *unique_ids, "--output", "json",
        label=f"{phase} ALB and worker security-group dump",
    ).get("SecurityGroups", [])
    aws_json(
        recorder, "ec2", "describe-security-group-rules", "--region", REGION,
        "--filters", f"Name=group-id,Values={','.join(unique_ids)}", "--output", "json",
        label=f"{phase} ALB and worker security-group rule dump",
    )
    return {group.get("GroupId"): group for group in groups}


def exact_backend_http_rule(group: dict[str, Any], backend_sg_id: str) -> bool:
    for permission in group.get("IpPermissions", []):
        if permission.get("IpProtocol") != "tcp" or permission.get("FromPort") != 80 or permission.get("ToPort") != 80:
            continue
        if any(pair.get("GroupId") == backend_sg_id for pair in permission.get("UserIdGroupPairs", [])):
            return True
    return False


def backend_source_has_only_tcp80(group: dict[str, Any], backend_sg_id: str) -> bool:
    for permission in group.get("IpPermissions", []):
        if any(pair.get("GroupId") == backend_sg_id for pair in permission.get("UserIdGroupPairs", [])):
            if permission.get("IpProtocol") != "tcp" or permission.get("FromPort") != 80 or permission.get("ToPort") != 80:
                return False
    return True


def has_non_sg_cidr_for_target_http(group: dict[str, Any], pod_ips: list[str]) -> bool:
    targets = []
    for address in pod_ips:
        try:
            targets.append(ipaddress.ip_address(address))
        except ValueError:
            continue
    for permission in group.get("IpPermissions", []):
        protocol = permission.get("IpProtocol")
        covers_http = protocol == "-1" or (
            protocol == "tcp" and permission.get("FromPort", 0) <= 80 <= permission.get("ToPort", 0)
        )
        if not covers_http:
            continue
        for item in permission.get("IpRanges", []):
            try:
                network = ipaddress.ip_network(item.get("CidrIp", ""), strict=False)
            except ValueError:
                continue
            if any(address.version == network.version and address in network for address in targets):
                return True
        # A prefix list may include target addresses; fail closed because the
        # preserved API response alone cannot prove it excludes these Pod IPs.
        if permission.get("PrefixListIds"):
            return True
    return False


def has_other_security_group_for_http(group: dict[str, Any], backend_sg_id: str) -> bool:
    for permission in group.get("IpPermissions", []):
        protocol = permission.get("IpProtocol")
        from_port = permission.get("FromPort")
        to_port = permission.get("ToPort")
        covers_http = protocol == "-1" or (
            protocol == "tcp" and isinstance(from_port, int) and isinstance(to_port, int) and from_port <= 80 <= to_port
        )
        if not covers_http:
            continue
        if any(pair.get("GroupId") != backend_sg_id for pair in permission.get("UserIdGroupPairs", [])):
            return True
    return False


def alb_egress_allows_target(groups: dict[str, Any], pod_ips: list[str], worker_sg_ids: list[str]) -> bool:
    target_networks = []
    for address in pod_ips:
        try:
            target_networks.append(ipaddress.ip_address(address))
        except ValueError:
            continue
    for group in groups.values():
        for permission in group.get("IpPermissionsEgress", []):
            protocol = permission.get("IpProtocol")
            from_port = permission.get("FromPort")
            to_port = permission.get("ToPort")
            if protocol == "-1" or (protocol == "tcp" and from_port == 80 and to_port == 80):
                if any(pair.get("GroupId") in worker_sg_ids for pair in permission.get("UserIdGroupPairs", [])):
                    return True
                for cidr in permission.get("IpRanges", []):
                    try:
                        network = ipaddress.ip_network(cidr.get("CidrIp", ""), strict=False)
                    except ValueError:
                        continue
                    if target_networks and all(address.version == network.version and address in network for address in target_networks):
                        return True
                for cidr in permission.get("Ipv6Ranges", []):
                    try:
                        network = ipaddress.ip_network(cidr.get("CidrIpv6", ""), strict=False)
                    except ValueError:
                        continue
                    if target_networks and all(address.version == network.version and address in network for address in target_networks):
                        return True
    return False


def ensure_backend_http_rules(
    recorder: Any,
    worker_sg_ids: list[str],
    backend_sg_id: str,
    experiment_tag: str,
    initial_groups: dict[str, Any],
) -> list[str]:
    created: list[str] = []
    tag_specifications = json.dumps([{
        "ResourceType": "security-group-rule",
        "Tags": [
            {"Key": "Project", "Value": "onebite"},
            {"Key": "Temporary", "Value": "true"},
            {"Key": "measure-experiment", "Value": experiment_tag},
        ],
    }])
    for group_id in sorted(set(worker_sg_ids)):
        group = initial_groups.get(group_id)
        if group is None:
            raise RuntimeError(f"Worker security group {group_id} was missing from the SG snapshot")
        if exact_backend_http_rule(group, backend_sg_id):
            recorder.write({
                "kind": "v3_backend_sg_rule_present",
                "observed_at_utc": utc_now(),
                "target_security_group": group_id,
                "source_security_group": backend_sg_id,
                "protocol": "tcp",
                "from_port": 80,
                "to_port": 80,
            })
            continue

        permission = [{
            "IpProtocol": "tcp",
            "FromPort": 80,
            "ToPort": 80,
            "UserIdGroupPairs": [{
                "GroupId": backend_sg_id,
                "Description": "Phase 3 v3 ALB backend HTTP only",
            }],
        }]
        response = recorder.aws(
            "ec2", "authorize-security-group-ingress", "--region", REGION,
            "--group-id", group_id,
            "--ip-permissions", json.dumps(permission),
            "--tag-specifications", tag_specifications,
            "--output", "json",
        )
        if response.returncode:
            recorder.write({
                "kind": "v3_backend_sg_rule_authorize_failed",
                "observed_at_utc": utc_now(),
                "target_security_group": group_id,
                "source_security_group": backend_sg_id,
                "stderr": response.stderr,
                "exit_code": response.returncode,
            })
        else:
            try:
                rule_ids = [item.get("SecurityGroupRuleId") for item in json.loads(response.stdout).get("SecurityGroupRules", [])]
            except (json.JSONDecodeError, AttributeError):
                rule_ids = []
            created.extend(item for item in rule_ids if item)
            recorder.write({
                "kind": "v3_backend_sg_rule_authorized",
                "observed_at_utc": utc_now(),
                "target_security_group": group_id,
                "source_security_group": backend_sg_id,
                "protocol": "tcp",
                "from_port": 80,
                "to_port": 80,
                "security_group_rule_ids": rule_ids,
                "tags": {"Project": "onebite", "Temporary": "true", "measure-experiment": experiment_tag},
            })

    recorder.write({"kind": "v3_backend_sg_rule_ids_created", "observed_at_utc": utc_now(), "security_group_rule_ids": created})
    return created


def run_vpc_http_probe(recorder: Any, pod_ips: list[str], trial: int) -> None:
    pod_name = f"phase3-v3-vpc-probe-{trial:02d}"
    overrides = json.dumps({
        "spec": {
            "nodeSelector": {"measure-pool": "system"},
            "automountServiceAccountToken": False,
        }
    })
    script = (
        'set -e; for ip in "$@"; do '
        'status="$(curl -sS --max-time 5 -o /dev/null -w "%{http_code}" "http://${ip}:80/")"; '
        'printf "pod_ip=%s http_status=%s\\n" "$ip" "$status"; '
        '[ "$status" = "200" ]; done'
    )
    created = recorder.kubectl(
        "run", pod_name, "-n", NAMESPACE,
        "--image=curlimages/curl:8.12.1", "--restart=Never",
        "--labels=app=phase3-v3-vpc-probe,measure-experiment=phase3-v3",
        "--overrides", overrides,
        "--command", "--", "sh", "-c", script, "sh", *pod_ips,
    )
    if created.returncode:
        raise RuntimeError(created.stderr.strip() or "Could not create the temporary in-VPC HTTP probe")
    try:
        waited = recorder.kubectl("wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{pod_name}", "-n", NAMESPACE, "--timeout=120s")
        output = recorder.kubectl("logs", pod_name, "-n", NAMESPACE)
        recorder.write({
            "kind": "v3_vpc_pod_ip_http_probe",
            "trial": trial,
            "observed_at_utc": utc_now(),
            "probe_node_selector": {"measure-pool": "system"},
            "pod_ips": pod_ips,
            "wait_exit_code": waited.returncode,
            "output": output.stdout,
            "stderr": output.stderr,
        })
        if waited.returncode or output.returncode:
            raise RuntimeError("VPC-internal Pod IP:80 probe did not complete successfully")
        observed = [line for line in output.stdout.splitlines() if line.startswith("pod_ip=") and "http_status=200" in line]
        if len(observed) != len(pod_ips):
            raise RuntimeError(f"Expected HTTP 200 from all {len(pod_ips)} Pod IPs; observed {observed}")
    finally:
        recorder.kubectl("delete", "pod", pod_name, "-n", NAMESPACE, "--ignore-not-found", "--wait=true")


def wait_for_healthy_targets(
    recorder: Any,
    target_group_arns: list[str],
    pod_ips: set[str],
    timeout_seconds: int = 900,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        all_healthy = True
        for target_group_arn in target_group_arns:
            payload = aws_json(
                recorder, "elbv2", "describe-target-health", "--region", REGION,
                "--target-group-arn", target_group_arn, "--output", "json",
                label="Full target-health gate snapshot",
            )
            rows = payload.get("TargetHealthDescriptions", [])
            observed = {
                (item.get("Target", {}).get("Id"), item.get("Target", {}).get("Port"), item.get("TargetHealth", {}).get("State"))
                for item in rows
            }
            healthy_targets = {
                (item.get("Target", {}).get("Id"), item.get("Target", {}).get("Port"))
                for item in rows
                if item.get("TargetHealth", {}).get("State") == "healthy"
            }
            if len(rows) != 2 or healthy_targets != {(ip, 80) for ip in pod_ips}:
                all_healthy = False
            recorder.write({
                "kind": "v3_target_health_gate_sample",
                "observed_at_utc": utc_now(),
                "target_group_arn": target_group_arn,
                "targets": sorted([list(item) for item in observed]),
                "healthy_expected_two_pod_ips_on_80": healthy_targets == {(ip, 80) for ip in pod_ips} and len(rows) == 2,
            })
        if all_healthy:
            return
        time.sleep(5)
    raise RuntimeError("Both registered Pod IP targets did not become healthy within 15 minutes")


def wait_for_consecutive_alb_2xx(recorder: Any, url: str, trial: int, consecutive: int = 5, timeout_seconds: int = 900) -> None:
    deadline = time.monotonic() + timeout_seconds
    success_count = 0
    while time.monotonic() < deadline:
        observed_at = utc_now()
        try:
            with urlopen(url, timeout=5) as response:
                response.read(1)
                status: int | None = response.status
                error = ""
        except HTTPError as exc:
            status = exc.code
            error = f"HTTPError: {exc.reason}"
        except Exception as exc:  # Preserve network failures as gate evidence.
            status = None
            error = f"{type(exc).__name__}: {exc}"
        success_count = success_count + 1 if status is not None and 200 <= status < 300 else 0
        recorder.write({
            "kind": "v3_alb_endpoint_gate_request",
            "trial": trial,
            "started_at_utc": observed_at,
            "finished_at_utc": utc_now(),
            "url_host": urlsplit(url).hostname,
            "http_status": status,
            "error": error,
            "consecutive_2xx": success_count,
            "required_consecutive_2xx": consecutive,
        })
        if success_count >= consecutive:
            return
        time.sleep(1)
    raise RuntimeError(f"ALB endpoint did not return {consecutive} consecutive HTTP 2xx responses within 15 minutes")


def run_preflight(recorder: Any, http_url: str, trial: int, experiment_tag: str) -> None:
    """Collect v3 network evidence and fail closed before any FIS call."""
    gate = {"trial": trial, "started_at_utc": utc_now(), "checks": {}}
    try:
        pods_payload = kubectl_json(recorder, "get", "pods", "-n", NAMESPACE, "-l", "app=backend-probe", "-o", "json")
        nodes_payload = kubectl_json(recorder, "get", "nodes", "-l", "measure-experiment=phase3-spot", "-o", "json")
        pods = [
            item for item in pods_payload.get("items", [])
            if any(condition.get("type") == "Ready" and condition.get("status") == "True" for condition in item.get("status", {}).get("conditions", []))
        ]
        nodes = [item for item in nodes_payload.get("items", []) if is_ready(item)]
        pod_node_names = {item.get("spec", {}).get("nodeName") for item in pods}
        if len(nodes) != 2 or any(node.get("metadata", {}).get("labels", {}).get("karpenter.sh/capacity-type") != "spot" for node in nodes):
            raise RuntimeError("Gate 1 failed: expected two Ready Spot workers")
        if len(pods) != 2 or len(pod_node_names) != 2 or pod_node_names != {item.get("metadata", {}).get("name") for item in nodes}:
            raise RuntimeError("Gate 2 failed: expected two Ready Pods on the two distinct Spot workers")
        pod_ips = [item.get("status", {}).get("podIP") for item in pods]
        if any(not ip for ip in pod_ips):
            raise RuntimeError("Gate 2 failed: a Ready Pod had no Pod IP")
        gate["checks"]["two_spot_workers_ready"] = True
        gate["checks"]["two_ready_pods_on_distinct_workers"] = True

        worker_instance_ids = [instance_id(node) for node in nodes]
        instance_payload = aws_json(
            recorder, "ec2", "describe-instances", "--region", REGION,
            "--instance-ids", *sorted(worker_instance_ids), "--output", "json",
            label="Full Phase 3 Spot worker ENI/subnet/security-group snapshot",
        )
        instances = [instance for reservation in instance_payload.get("Reservations", []) for instance in reservation.get("Instances", [])]
        if {item.get("InstanceId") for item in instances} != set(worker_instance_ids):
            raise RuntimeError("Could not map both Ready workers to their EC2 network interfaces")
        worker_sg_ids = sorted({
            group.get("GroupId")
            for instance in instances
            for interface in instance.get("NetworkInterfaces", [])
            for group in interface.get("Groups", [])
            if group.get("GroupId")
        })
        worker_subnet_ids = sorted({item.get("SubnetId") for item in instances if item.get("SubnetId")})
        if not worker_sg_ids or not worker_subnet_ids:
            raise RuntimeError("Worker ENI snapshot did not contain security groups and subnet IDs")

        hostname = urlsplit(http_url).hostname
        if not hostname:
            raise RuntimeError("ALB endpoint hostname is missing")
        query = f"LoadBalancers[?DNSName=='{hostname}']"
        lb_payload = aws_json(
            recorder, "elbv2", "describe-load-balancers", "--region", REGION,
            "--query", query, "--output", "json",
            label="Full ALB and enabled-AZ/subnet snapshot",
        )
        load_balancers = lb_payload if isinstance(lb_payload, list) else []
        if len(load_balancers) != 1:
            raise RuntimeError(f"Expected exactly one ALB with the Ingress DNS name, found {len(load_balancers)}")
        load_balancer = load_balancers[0]
        load_balancer_arn = load_balancer.get("LoadBalancerArn")
        alb_sg_ids = sorted(load_balancer.get("SecurityGroups", []))
        alb_subnet_ids = sorted({item.get("SubnetId") for item in load_balancer.get("AvailabilityZones", []) if item.get("SubnetId")})
        if not load_balancer_arn or not alb_sg_ids or not alb_subnet_ids:
            raise RuntimeError("ALB snapshot did not contain ARN, attached SGs, and enabled subnets")

        tg_payload = aws_json(
            recorder, "elbv2", "describe-target-groups", "--region", REGION,
            "--load-balancer-arn", load_balancer_arn, "--output", "json",
            label="Full Target Group configuration snapshot",
        )
        target_groups = tg_payload.get("TargetGroups", [])
        if len(target_groups) != 1:
            raise RuntimeError(f"Expected exactly one Phase 3 backend Target Group, found {len(target_groups)}")
        target_group = target_groups[0]
        target_group_arn = target_group.get("TargetGroupArn")
        if (
            target_group.get("TargetType") != "ip"
            or target_group.get("Protocol") != "HTTP"
            or target_group.get("HealthCheckPath") != "/"
            or target_group.get("HealthCheckPort") != "traffic-port"
        ):
            raise RuntimeError("Target Group did not match the preregistered IP/HTTP/traffic-port/path=/ configuration")
        if not target_group_arn:
            raise RuntimeError("Target Group ARN is missing")

        subnet_ids = sorted(set(alb_subnet_ids + worker_subnet_ids))
        initial_groups = capture_security_group_rules(recorder, alb_sg_ids + worker_sg_ids, "before-v3-reachability-rule")
        aws_json(
            recorder, "ec2", "describe-subnets", "--region", REGION,
            "--subnet-ids", *subnet_ids, "--output", "json",
            label="Full ALB and worker subnet snapshot",
        )
        vpc_ids = sorted({load_balancer.get("VpcId")} | {item.get("VpcId") for item in instances if item.get("VpcId")})
        if len(vpc_ids) != 1:
            raise RuntimeError(f"ALB and workers did not resolve to one VPC: {vpc_ids}")
        aws_json(
            recorder, "ec2", "describe-route-tables", "--region", REGION,
            "--filters", f"Name=vpc-id,Values={vpc_ids[0]}", "--output", "json",
            label="Full Phase 3 VPC route-table snapshot",
        )
        aws_json(
            recorder, "ec2", "describe-network-acls", "--region", REGION,
            "--filters", f"Name=association.subnet-id,Values={','.join(subnet_ids)}", "--output", "json",
            label="ALB and worker subnet network-ACL snapshot",
        )

        backend_sg_payload = aws_json(
            recorder, "ec2", "describe-security-groups", "--region", REGION,
            "--filters",
            "Name=tag:elbv2.k8s.aws/resource,Values=backend-sg",
            f"Name=tag:elbv2.k8s.aws/cluster,Values={CLUSTER}",
            "--output", "json",
            label="Exact LBC shared backend SG lookup by required tags",
        )
        backend_sg_groups = backend_sg_payload.get("SecurityGroups", [])
        if len(backend_sg_groups) != 1:
            found_ids = sorted(group.get("GroupId", "") for group in backend_sg_groups)
            raise RuntimeError(
                "Expected exactly one LBC shared backend SG from the required tag query; "
                f"found {len(backend_sg_groups)}: {found_ids}"
            )
        backend_sg_id = backend_sg_groups[0].get("GroupId")
        if not backend_sg_id or backend_sg_id not in alb_sg_ids:
            raise RuntimeError(
                "The unique LBC shared backend SG from the required tag query is not attached to this ALB"
            )
        alb_groups = {group_id: initial_groups[group_id] for group_id in alb_sg_ids if group_id in initial_groups}
        if not alb_egress_allows_target(alb_groups, pod_ips, worker_sg_ids):
            raise RuntimeError("ALB security groups do not show egress that permits target TCP 80")
        gate["checks"]["alb_security_group_egress_allows_target_port"] = True

        # Keep the AWS Load Balancer Controller's default SG path. This exact, tagged
        # TCP/80 rule is a fallback only when the controller-managed rule is absent.
        created_rule_ids = ensure_backend_http_rules(
            recorder, worker_sg_ids, backend_sg_id, experiment_tag, initial_groups,
        )
        final_groups = capture_security_group_rules(recorder, alb_sg_ids + worker_sg_ids, "before-fis-final")
        if any(not exact_backend_http_rule(final_groups.get(group_id, {}), backend_sg_id) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: worker SG lacks exact ALB backend SG -> TCP 80 ingress")
        if any(not backend_source_has_only_tcp80(final_groups.get(group_id, {}), backend_sg_id) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: worker SG has an overly broad rule from the ALB backend SG")
        if any(has_non_sg_cidr_for_target_http(final_groups.get(group_id, {}), pod_ips) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: a target worker SG also permits Pod-IP HTTP from a CIDR or prefix list")
        if any(has_other_security_group_for_http(final_groups.get(group_id, {}), backend_sg_id) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: a target worker SG permits HTTP from a security group other than the ALB backend SG")
        gate["checks"]["worker_sg_allows_only_exact_backend_source_tcp_80"] = True
        gate["security_groups"] = {
            "alb_group_ids": alb_sg_ids,
            "lbc_backend_group_id": backend_sg_id,
            "worker_group_ids": worker_sg_ids,
            "tagged_rule_ids_created_by_v3": created_rule_ids,
        }

        gate["checks"]["vpc_client_http_200_for_each_pod_ip"] = False
        run_vpc_http_probe(recorder, pod_ips, trial)
        gate["checks"]["vpc_client_http_200_for_each_pod_ip"] = True

        wait_for_healthy_targets(recorder, [target_group_arn], set(pod_ips))
        gate["checks"]["both_target_group_targets_healthy"] = True
        wait_for_consecutive_alb_2xx(recorder, http_url, trial)
        gate["checks"]["alb_endpoint_five_consecutive_2xx"] = True

        # Dump the final effective SG state after the HTTP checks, immediately before FIS.
        final_groups = capture_security_group_rules(recorder, alb_sg_ids + worker_sg_ids, "immediately-before-fis")
        if any(not exact_backend_http_rule(final_groups.get(group_id, {}), backend_sg_id) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: worker SG -> ALB backend SG TCP 80 rule disappeared before FIS")
        if any(not backend_source_has_only_tcp80(final_groups.get(group_id, {}), backend_sg_id) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: worker SG has an overly broad rule from the ALB backend SG")
        if any(has_non_sg_cidr_for_target_http(final_groups.get(group_id, {}), pod_ips) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: a target worker SG also permits Pod-IP HTTP from a CIDR or prefix list")
        if any(has_other_security_group_for_http(final_groups.get(group_id, {}), backend_sg_id) for group_id in worker_sg_ids):
            raise RuntimeError("Gate 6 failed: a target worker SG permits HTTP from a security group other than the ALB backend SG")
        gate["checks"]["worker_sg_allows_only_exact_backend_source_tcp_80"] = True
        gate["passed"] = True
        gate["completed_at_utc"] = utc_now()
        gate["alb_enabled_subnets"] = load_balancer.get("AvailabilityZones", [])
        gate["worker_subnets"] = worker_subnet_ids
        gate["pod_ips"] = pod_ips
        recorder.write({"kind": "v3_pre_fis_reachability_gate", **gate})
        capture_controller_context(recorder, trial, "pre-fis-gate-passed")
    except Exception as exc:
        gate["passed"] = False
        gate["failed_at_utc"] = utc_now()
        gate["failure"] = f"{type(exc).__name__}: {exc}"
        recorder.write({"kind": "v3_pre_fis_reachability_gate", **gate})
        raise
