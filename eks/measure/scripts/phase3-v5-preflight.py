"""ALB reachability diagnostics and fail-closed pre-FIS gates for Phase 3 v5."""

from __future__ import annotations

import json
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
    recorder.write({"kind": "v5_controller_context_start", "trial": trial, "phase": phase, "observed_at_utc": utc_now()})
    recorder.kubectl(
        "logs", "deployment/aws-load-balancer-controller", "-n", "kube-system",
        "--all-containers=true", "--timestamps=true", "--since=30m",
    )
    recorder.kubectl("get", "events", "-A", "--sort-by=.metadata.creationTimestamp", "-o", "json")
    recorder.kubectl("describe", "ingress", APP, "-n", NAMESPACE)
    recorder.kubectl("get", "service", APP, "-n", NAMESPACE, "-o", "json")
    recorder.write({"kind": "v5_controller_context_end", "trial": trial, "phase": phase, "observed_at_utc": utc_now()})


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
                "kind": "v5_backend_sg_rule_present",
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
                "Description": "Phase 3 v5 ALB backend HTTP only",
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
                "kind": "v5_backend_sg_rule_authorize_failed",
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
                "kind": "v5_backend_sg_rule_authorized",
                "observed_at_utc": utc_now(),
                "target_security_group": group_id,
                "source_security_group": backend_sg_id,
                "protocol": "tcp",
                "from_port": 80,
                "to_port": 80,
                "security_group_rule_ids": rule_ids,
                "tags": {"Project": "onebite", "Temporary": "true", "measure-experiment": experiment_tag},
            })

    recorder.write({"kind": "v5_backend_sg_rule_ids_created", "observed_at_utc": utc_now(), "security_group_rule_ids": created})
    return created


def run_vpc_http_probe(recorder: Any, pod_ips: list[str], trial: int) -> None:
    pod_name = f"phase3-v5-vpc-probe-{trial:02d}"
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
        "--labels=app=phase3-v5-vpc-probe,measure-experiment=phase3-v5",
        "--overrides", overrides,
        "--command", "--", "sh", "-c", script, "sh", *pod_ips,
    )
    if created.returncode:
        raise RuntimeError(created.stderr.strip() or "Could not create the temporary in-VPC HTTP probe")
    try:
        waited = recorder.kubectl("wait", "--for=jsonpath={.status.phase}=Succeeded", f"pod/{pod_name}", "-n", NAMESPACE, "--timeout=120s")
        output = recorder.kubectl("logs", pod_name, "-n", NAMESPACE)
        recorder.write({
            "kind": "v5_vpc_pod_ip_http_probe",
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
                "kind": "v5_target_health_gate_sample",
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
            "kind": "v5_alb_endpoint_gate_request",
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


LBC_TARGET_ENI_SG_TAG_KEY = "onebite.io/lbc-target-sg"


def resolve_lbc_target_worker_sg(
    groups: dict[str, Any], worker_sg_ids: list[str],
) -> str:
    cluster_tag_key = f"kubernetes.io/cluster/{CLUSTER}"
    matches = []
    for group_id in worker_sg_ids:
        group = groups.get(group_id, {})
        tags = {item.get("Key"): item.get("Value") for item in group.get("Tags", [])}
        if cluster_tag_key in tags and tags.get(LBC_TARGET_ENI_SG_TAG_KEY) == CLUSTER:
            matches.append(group_id)
    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one worker ENI SG tagged with both the EKS cluster tag "
            f"and {LBC_TARGET_ENI_SG_TAG_KEY}={CLUSTER}; found {sorted(matches)}"
        )
    return matches[0]


def v5_created_broad_http_rules(
    recorder: Any, worker_sg_ids: list[str], experiment_tag: str,
) -> list[dict[str, Any]]:
    if not worker_sg_ids:
        raise RuntimeError("No target worker SG IDs available for the v5-created ingress scan")
    payload = aws_json(
        recorder, "ec2", "describe-security-group-rules", "--region", REGION,
        "--filters", f"Name=group-id,Values={','.join(sorted(set(worker_sg_ids)))}",
        "--output", "json", label="V5-tagged broad HTTP ingress scan on worker SGs",
    )
    matches: list[dict[str, Any]] = []
    for rule in payload.get("SecurityGroupRules", []):
        tags = {item.get("Key"): item.get("Value") for item in rule.get("Tags", [])}
        if (
            tags.get("Project") != "onebite"
            or tags.get("Temporary") != "true"
            or tags.get("measure-experiment") != experiment_tag
            or rule.get("IsEgress")
        ):
            continue
        protocol = rule.get("IpProtocol")
        covers_http = protocol == "-1" or (
            protocol == "tcp"
            and isinstance(rule.get("FromPort"), int)
            and isinstance(rule.get("ToPort"), int)
            and rule["FromPort"] <= 80 <= rule["ToPort"]
        )
        if not covers_http:
            continue
        if rule.get("CidrIpv5") == "0.0.0.0/0" or rule.get("CidrIpv6") == "::/0":
            matches.append({
                key: rule.get(key)
                for key in (
                    "SecurityGroupRuleId", "GroupId", "IsEgress", "IpProtocol",
                    "FromPort", "ToPort", "CidrIpv5", "CidrIpv6", "Tags",
                )
            })
    recorder.write({
        "kind": "v5_batch_broad_http_ingress_scan",
        "observed_at_utc": utc_now(),
        "worker_security_group_ids": sorted(set(worker_sg_ids)),
        "experiment_tag": experiment_tag,
        "broad_http_ingress_rules": matches,
    })
    return matches


def run_preflight(
    recorder: Any,
    http_url: str,
    trial: int,
    experiment_tag: str,
    verify_node_peer_http_rule: bool = False,
) -> None:
    """Run the existing Phase 3 gates, with an optional read-only final v6 SG gate."""
    ordered_gates = [
        "spot_workers_2_ready",
        "pods_2_ready_on_distinct_workers",
        "pod_ip_80_http_200",
        "target_group_2_healthy",
        "alb_http_2xx_five_consecutive",
    ]
    if verify_node_peer_http_rule:
        ordered_gates.append("node_sg_self_reference_tcp_80_present")
    gate = {
        "trial": trial,
        "started_at_utc": utc_now(),
        "ordered_gates": ordered_gates,
        "completed_gates": [],
        "checks": {},
    }
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
            raise RuntimeError("Gate 1 failed: expected exactly two Ready Spot workers")
        gate["checks"]["two_spot_workers_ready"] = True
        gate["completed_gates"].append(gate["ordered_gates"][0])
        recorder.write({"kind": "v5_pre_fis_gate_step", "trial": trial, "step": 1, "name": gate["ordered_gates"][0], "passed": True, "observed_at_utc": utc_now()})

        if len(pods) != 2 or len(pod_node_names) != 2 or pod_node_names != {item.get("metadata", {}).get("name") for item in nodes}:
            raise RuntimeError("Gate 2 failed: expected two Ready Pods on the two distinct Spot workers")
        pod_ips = [item.get("status", {}).get("podIP") for item in pods]
        if any(not ip for ip in pod_ips):
            raise RuntimeError("Gate 2 failed: a Ready Pod had no Pod IP")
        gate["checks"]["two_ready_pods_on_distinct_workers"] = True
        gate["completed_gates"].append(gate["ordered_gates"][1])
        recorder.write({"kind": "v5_pre_fis_gate_step", "trial": trial, "step": 2, "name": gate["ordered_gates"][1], "passed": True, "observed_at_utc": utc_now()})

        worker_instance_ids = [instance_id(node) for node in nodes]
        instance_payload = aws_json(
            recorder, "ec2", "describe-instances", "--region", REGION,
            "--instance-ids", *sorted(worker_instance_ids), "--output", "json",
            label="V5 Spot worker ENI/subnet/security-group snapshot",
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
        capture_security_group_rules(recorder, worker_sg_ids, "v5-worker-before-reachability-gates")
        subnet_payload = aws_json(
            recorder, "ec2", "describe-subnets", "--region", REGION,
            "--subnet-ids", *worker_subnet_ids, "--output", "json",
            label="V5 Spot worker subnet snapshot",
        )
        vpcs = sorted({item.get("VpcId") for item in instances if item.get("VpcId")})
        if len(vpcs) != 1:
            raise RuntimeError(f"V5 Spot workers did not resolve to one VPC: {vpcs}")
        aws_json(
            recorder, "ec2", "describe-route-tables", "--region", REGION,
            "--filters", f"Name=vpc-id,Values={vpcs[0]}", "--output", "json",
            label="V5 Spot worker VPC route-table snapshot",
        )
        aws_json(
            recorder, "ec2", "describe-network-acls", "--region", REGION,
            "--filters", f"Name=association.subnet-id,Values={','.join(worker_subnet_ids)}", "--output", "json",
            label="V5 Spot worker subnet network-ACL snapshot",
        )
        gate["worker_instance_ids"] = sorted(worker_instance_ids)
        gate["worker_security_group_ids"] = worker_sg_ids
        gate["worker_subnets"] = worker_subnet_ids
        gate["pod_ips"] = pod_ips
        gate["worker_subnet_snapshot_count"] = len(subnet_payload.get("Subnets", []))

        # Gate 3 is intentionally before Target Group and ALB health checks.
        run_vpc_http_probe(recorder, pod_ips, trial)
        gate["checks"]["vpc_client_http_200_for_each_pod_ip"] = True
        gate["completed_gates"].append(gate["ordered_gates"][2])
        recorder.write({"kind": "v5_pre_fis_gate_step", "trial": trial, "step": 3, "name": gate["ordered_gates"][2], "passed": True, "observed_at_utc": utc_now()})

        hostname = urlsplit(http_url).hostname
        if not hostname:
            raise RuntimeError("ALB endpoint hostname is missing")
        query = f"LoadBalancers[?DNSName=='{hostname}']"
        lb_payload = aws_json(
            recorder, "elbv2", "describe-load-balancers", "--region", REGION,
            "--query", query, "--output", "json",
            label="Full V5 ALB and enabled-AZ/subnet snapshot",
        )
        load_balancers = lb_payload if isinstance(lb_payload, list) else []
        if len(load_balancers) != 1:
            raise RuntimeError(f"Gate 4 preparation failed: expected exactly one ALB, found {len(load_balancers)}")
        load_balancer = load_balancers[0]
        load_balancer_arn = load_balancer.get("LoadBalancerArn")
        alb_sg_ids = sorted(load_balancer.get("SecurityGroups", []))
        alb_subnet_ids = sorted({item.get("SubnetId") for item in load_balancer.get("AvailabilityZones", []) if item.get("SubnetId")})
        if not load_balancer_arn or not alb_sg_ids or not alb_subnet_ids:
            raise RuntimeError("ALB snapshot did not contain ARN, attached SGs, and enabled subnets")

        tg_payload = aws_json(
            recorder, "elbv2", "describe-target-groups", "--region", REGION,
            "--load-balancer-arn", load_balancer_arn, "--output", "json",
            label="Full V5 Target Group configuration snapshot",
        )
        target_groups = tg_payload.get("TargetGroups", [])
        if len(target_groups) != 1:
            raise RuntimeError(f"Gate 4 failed: expected exactly one backend Target Group, found {len(target_groups)}")
        target_group = target_groups[0]
        target_group_arn = target_group.get("TargetGroupArn")

        all_subnet_ids = sorted(set(alb_subnet_ids + worker_subnet_ids))
        initial_groups = capture_security_group_rules(recorder, alb_sg_ids + worker_sg_ids, "v5-before-reachability-rule-setup")
        aws_json(
            recorder, "ec2", "describe-subnets", "--region", REGION,
            "--subnet-ids", *all_subnet_ids, "--output", "json",
            label="Full V5 ALB and worker subnet snapshot",
        )
        vpc_ids = sorted({load_balancer.get("VpcId")} | {item.get("VpcId") for item in instances if item.get("VpcId")})
        if len(vpc_ids) != 1:
            raise RuntimeError(f"ALB and workers did not resolve to one VPC: {vpc_ids}")
        aws_json(
            recorder, "ec2", "describe-route-tables", "--region", REGION,
            "--filters", f"Name=vpc-id,Values={vpc_ids[0]}", "--output", "json",
            label="Full V5 VPC route-table snapshot",
        )
        aws_json(
            recorder, "ec2", "describe-network-acls", "--region", REGION,
            "--filters", f"Name=association.subnet-id,Values={','.join(all_subnet_ids)}", "--output", "json",
            label="Full V5 ALB and worker subnet network-ACL snapshot",
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
            raise RuntimeError("The unique LBC shared backend SG is not attached to this ALB")

        worker_group_payload = aws_json(
            recorder, "ec2", "describe-security-groups", "--region", REGION,
            "--group-ids", *worker_sg_ids, "--output", "json",
            label="V5 target worker SG role and selector-tag snapshot",
        )
        worker_groups = {item.get("GroupId"): item for item in worker_group_payload.get("SecurityGroups", [])}
        lbc_target_sg_id = resolve_lbc_target_worker_sg(worker_groups, worker_sg_ids)
        gate["security_group_selection"] = {
            "cluster_tag_key": f"kubernetes.io/cluster/{CLUSTER}",
            "additional_selector_tag": {LBC_TARGET_ENI_SG_TAG_KEY: CLUSTER},
            "selected_target_worker_sg_id": lbc_target_sg_id,
            "attached_worker_sg_ids": worker_sg_ids,
            "match_count": 1,
        }

        # This is setup only; the sixth gate verifies the effective rules last.
        created_rule_ids = ensure_backend_http_rules(
            recorder, [lbc_target_sg_id], backend_sg_id, experiment_tag, initial_groups,
        )
        gate["security_groups"] = {
            "alb_group_ids": alb_sg_ids,
            "lbc_backend_group_id": backend_sg_id,
            "selected_target_worker_group_id": lbc_target_sg_id,
            "worker_group_ids": worker_sg_ids,
            "tagged_rule_ids_created_by_v5": created_rule_ids,
        }

        # Gate 4: require the unchanged target configuration and two healthy Pod IP targets.
        if (
            target_group.get("TargetType") != "ip"
            or target_group.get("Protocol") != "HTTP"
            or target_group.get("HealthCheckPath") != "/"
            or target_group.get("HealthCheckPort") != "traffic-port"
            or not target_group_arn
        ):
            raise RuntimeError("Gate 4 failed: Target Group must remain IP/HTTP, traffic-port, health path=/")
        wait_for_healthy_targets(recorder, [target_group_arn], set(pod_ips))
        gate["checks"]["both_target_group_targets_healthy"] = True
        gate["completed_gates"].append(gate["ordered_gates"][3])
        recorder.write({"kind": "v5_pre_fis_gate_step", "trial": trial, "step": 4, "name": gate["ordered_gates"][3], "passed": True, "observed_at_utc": utc_now()})

        # Gate 5: five consecutive public ALB 2xx responses.
        wait_for_consecutive_alb_2xx(recorder, http_url, trial)
        gate["checks"]["alb_endpoint_five_consecutive_2xx"] = True
        gate["completed_gates"].append(gate["ordered_gates"][4])
        recorder.write({"kind": "v5_pre_fis_gate_step", "trial": trial, "step": 5, "name": gate["ordered_gates"][4], "passed": True, "observed_at_utc": utc_now()})

        if verify_node_peer_http_rule:
            selected_sg_id = gate["security_group_selection"]["selected_target_worker_sg_id"]
            final_peer_payload = aws_json(
                recorder,
                "ec2", "describe-security-groups", "--region", REGION,
                "--group-ids", selected_sg_id, "--output", "json",
                label="Final v6 worker node-SG self-reference TCP 80 gate",
            )
            final_peer_groups = final_peer_payload.get("SecurityGroups", [])
            if len(final_peer_groups) != 1 or final_peer_groups[0].get("GroupId") != selected_sg_id:
                raise RuntimeError("Final gate failed: selected worker SG could not be uniquely re-read")
            peer_group = final_peer_groups[0]
            peer_rules = [
                {
                    "protocol": permission.get("IpProtocol"),
                    "from_port": permission.get("FromPort"),
                    "to_port": permission.get("ToPort"),
                    "source_group_ids": sorted({
                        pair.get("GroupId")
                        for pair in permission.get("UserIdGroupPairs", [])
                        if pair.get("GroupId")
                    }),
                }
                for permission in peer_group.get("IpPermissions", [])
                if permission.get("IpProtocol") == "tcp"
                and permission.get("FromPort") == 80
                and permission.get("ToPort") == 80
            ]
            self_reference_present = any(
                selected_sg_id in rule["source_group_ids"]
                for rule in peer_rules
            )
            recorder.write({
                "kind": "phase3_v6_node_peer_sg_gate",
                "trial": trial,
                "observed_at_utc": utc_now(),
                "security_group_id": selected_sg_id,
                "protocol": "tcp",
                "port": 80,
                "self_reference_rule_present": self_reference_present,
                "matching_ingress_rules": peer_rules,
            })
            if not self_reference_present:
                raise RuntimeError(
                    "Final gate failed: the selected worker SG lacks its TCP 80 self-reference rule"
                )
            gate["checks"]["node_sg_self_reference_tcp_80_present"] = True
            gate["completed_gates"].append(gate["ordered_gates"][5])
            recorder.write({
                "kind": "phase3_v6_pre_fis_gate_step",
                "trial": trial,
                "step": 6,
                "name": gate["ordered_gates"][5],
                "passed": True,
                "observed_at_utc": utc_now(),
            })

        gate["passed"] = len(gate["completed_gates"]) == len(gate["ordered_gates"])
        gate["completed_at_utc"] = utc_now()
        gate["alb_enabled_subnets"] = load_balancer.get("AvailabilityZones", [])
        recorder.write({"kind": "v5_pre_fis_reachability_gate", **gate})
        capture_controller_context(recorder, trial, "pre-fis-gate-passed")
    except Exception as exc:
        gate["passed"] = False
        gate["failed_at_utc"] = utc_now()
        gate["failure"] = f"{type(exc).__name__}: {exc}"
        recorder.write({"kind": "v5_pre_fis_reachability_gate", **gate})
        raise
