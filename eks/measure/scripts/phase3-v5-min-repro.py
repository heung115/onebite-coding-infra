#!/usr/bin/env python3
"""Run the Phase 3 v5 system-node to Spot Pod TCP/HTTP minimal reproduction."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-northeast-2"
CLUSTER = "onebite-eks-measure"
KUBECONFIG = Path.home() / ".kube" / "onebite-eks.kubeconfig"
NAMESPACE = "measure"
BATCH = "phase3-spot-20261002-v5"
RAW = ROOT / "measure/results/20261002-phase3-spot-interruption-v5/minimal-repro/network-probe.jsonl"
OUT = ROOT / "measure/results/20261002-phase3-spot-interruption-v5/minimal-repro"
NGINX_NAME = "phase3-v5-min-nginx"
CURL_NAME = "phase3-v5-min-curl"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Recorder:
    def __init__(self) -> None:
        RAW.parent.mkdir(parents=True, exist_ok=True)

    def record(self, row: dict[str, Any]) -> None:
        with RAW.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def run(self, kind: str, argv: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["AWS_REGION"] = REGION
        if kind == "kubectl":
            env["KUBECONFIG"] = str(KUBECONFIG)
        started = utc_now()
        try:
            result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False, timeout=timeout)
            row = {
                "kind": kind,
                "started_at_utc": started,
                "finished_at_utc": utc_now(),
                "argv": argv,
                "environment": {"KUBECONFIG": str(KUBECONFIG)} if kind == "kubectl" else {"AWS_REGION": REGION},
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            self.record(row)
            return result
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            self.record({
                "kind": kind,
                "started_at_utc": started,
                "finished_at_utc": utc_now(),
                "argv": argv,
                "environment": {"KUBECONFIG": str(KUBECONFIG)} if kind == "kubectl" else {"AWS_REGION": REGION},
                "exit_code": 124,
                "stdout": stdout,
                "stderr": stderr,
                "timeout_seconds": timeout,
            })
            return subprocess.CompletedProcess(argv, 124, stdout, stderr + f"\nTimed out after {timeout}s")

    def kubectl(self, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        return self.run("kubectl", ["kubectl", "--kubeconfig", str(KUBECONFIG), "--context", CLUSTER, *args], timeout)

    def aws(self, *args: str, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
        return self.run("aws", ["aws", *args], timeout)

    def json(self, kind: str, *args: str, timeout: int | None = None) -> dict[str, Any]:
        result = self.kubectl(*args, timeout=timeout) if kind == "kubectl" else self.aws(*args, timeout=timeout)
        if result.returncode:
            raise RuntimeError(f"{kind} {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}")
        return json.loads(result.stdout)


def apply_manifest(rec: Recorder, name: str, pod: dict[str, Any]) -> Path:
    path = OUT / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pod, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = rec.kubectl("apply", "-f", str(path))
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or f"could not apply {name}")
    return path


def pod_manifest(name: str, labels: dict[str, str], spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": name, "namespace": NAMESPACE, "labels": labels},
        "spec": spec,
    }


def ready(resource: dict[str, Any]) -> bool:
    return any(
        c.get("type") == "Ready" and c.get("status") == "True"
        for c in resource.get("status", {}).get("conditions", [])
    )


def node_instance_id(node: dict[str, Any]) -> str | None:
    provider_id = node.get("spec", {}).get("providerID", "")
    value = provider_id.rsplit("/", 1)[-1]
    return value if value.startswith("i-") else None


def get_nodes(rec: Recorder) -> list[dict[str, Any]]:
    return rec.json("kubectl", "get", "nodes", "-o", "json").get("items", [])


def wait_pod_ready(rec: Recorder, name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = rec.json("kubectl", "get", "pod", name, "-n", NAMESPACE, "-o", "json")
        last = payload
        if ready(payload):
            rec.record({"kind": "v5_min_repro_pod_ready", "observed_at_utc": utc_now(), "pod": payload})
            return payload
        phase = payload.get("status", {}).get("phase")
        if phase in {"Failed", "Succeeded"}:
            break
        time.sleep(3)
    raise RuntimeError(f"Pod {name} did not become Ready: phase={last.get('status', {}).get('phase')}")


def wait_pod_terminal(rec: Recorder, name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        payload = rec.json("kubectl", "get", "pod", name, "-n", NAMESPACE, "-o", "json")
        last = payload
        if payload.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
            return payload
        time.sleep(2)
    return last


def capture_failure_diagnostics(
    rec: Recorder,
    source_node: dict[str, Any] | None,
    target_node: dict[str, Any] | None,
    pod: dict[str, Any] | None,
    reason: str,
) -> None:
    rec.record({
        "kind": "v5_network_failure_diagnostics_start",
        "observed_at_utc": utc_now(),
        "reason": reason,
        "source_system_node": source_node,
        "destination_spot_node": target_node,
        "nginx_test_pod": pod,
        "pod_ip": (pod or {}).get("status", {}).get("podIP"),
    })
    nodes = [node for node in (source_node, target_node) if node]
    instance_ids = sorted({iid for iid in (node_instance_id(n) for n in nodes) if iid})
    instance_payload: dict[str, Any] = {}
    if instance_ids:
        instance_payload = rec.json("aws", "ec2", "describe-instances", "--region", REGION, "--instance-ids", *instance_ids, "--output", "json")
    instances = [item for reservation in instance_payload.get("Reservations", []) for item in reservation.get("Instances", [])]
    rec.record({
        "kind": "v5_source_destination_ec2_enis",
        "observed_at_utc": utc_now(),
        "node_to_instance_ids": {node.get("metadata", {}).get("name"): node_instance_id(node) for node in nodes},
        "instances": instances,
    })
    group_ids = sorted({
        group.get("GroupId")
        for instance in instances
        for group in instance.get("SecurityGroups", [])
        if group.get("GroupId")
    } | {
        group.get("GroupId")
        for instance in instances
        for eni in instance.get("NetworkInterfaces", [])
        for group in eni.get("Groups", [])
        if group.get("GroupId")
    })
    if group_ids:
        rec.json("aws", "ec2", "describe-security-groups", "--region", REGION, "--group-ids", *group_ids, "--output", "json")
        rec.json("aws", "ec2", "describe-security-group-rules", "--region", REGION, "--filters", f"Name=group-id,Values={','.join(group_ids)}", "--output", "json")
    subnet_ids = sorted({item.get("SubnetId") for item in instances if item.get("SubnetId")})
    vpc_ids = sorted({item.get("VpcId") for item in instances if item.get("VpcId")})
    if subnet_ids:
        rec.json("aws", "ec2", "describe-subnets", "--region", REGION, "--subnet-ids", *subnet_ids, "--output", "json")
        rec.json("aws", "ec2", "describe-network-acls", "--region", REGION, "--filters", f"Name=association.subnet-id,Values={','.join(subnet_ids)}", "--output", "json")
    if vpc_ids:
        for vpc_id in vpc_ids:
            rec.json("aws", "ec2", "describe-route-tables", "--region", REGION, "--filters", f"Name=vpc-id,Values={vpc_id}", "--output", "json")

    rec.kubectl("get", "pod", NGINX_NAME, "-n", NAMESPACE, "-o", "yaml")
    rec.kubectl("get", "nodes", "-o", "wide")
    rec.kubectl("get", "events", "-A", "--sort-by=.metadata.creationTimestamp", "-o", "json")
    rec.kubectl("get", "daemonset", "aws-node", "-n", "kube-system", "-o", "yaml")
    aws_node_pods = rec.json("kubectl", "get", "pods", "-n", "kube-system", "-l", "k8s-app=aws-node", "-o", "json").get("items", [])
    node_names = {n.get("metadata", {}).get("name") for n in nodes}
    for aws_node_pod in aws_node_pods:
        if aws_node_pod.get("spec", {}).get("nodeName") in node_names:
            name = aws_node_pod.get("metadata", {}).get("name")
            rec.kubectl("logs", "pod/" + name, "-n", "kube-system", "--all-containers=true", "--timestamps=true", "--since=60m")
            rec.kubectl("describe", "pod", name, "-n", "kube-system")
    rec.json("aws", "eks", "describe-addon", "--region", REGION, "--cluster-name", CLUSTER, "--addon-name", "vpc-cni", "--output", "json")

    for node in nodes:
        node_name = node.get("metadata", {}).get("name")
        if not node_name:
            continue
        diag_name = ("phase3-v5-iproute-" + ("system" if node is source_node else "spot")).lower()
        diag = pod_manifest(diag_name, {"measure-experiment": BATCH, "app": "phase3-v5-iproute"}, {
            "nodeName": node_name,
            "hostNetwork": True,
            "restartPolicy": "Never",
            "automountServiceAccountToken": False,
            "containers": [{
                "name": "netshoot",
                "image": "nicolaka/netshoot:v0.13",
                "command": ["/bin/sh", "-c", "echo ===ip-route; ip route; echo ===ip-rule; ip rule; echo ===ip-address; ip -4 address"],
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True},
            }],
        })
        try:
            apply_manifest(rec, diag_name, diag)
            wait_pod_terminal(rec, diag_name, 120)
            rec.kubectl("logs", diag_name, "-n", NAMESPACE)
        except Exception as exc:
            rec.record({"kind": "v5_ip_route_capture_error", "observed_at_utc": utc_now(), "node": node_name, "error": str(exc)})
        finally:
            rec.kubectl("delete", "pod", diag_name, "-n", NAMESPACE, "--ignore-not-found", "--wait=true")
    rec.record({"kind": "v5_network_failure_diagnostics_end", "observed_at_utc": utc_now()})


def delete_pod(rec: Recorder, name: str) -> None:
    rec.kubectl("delete", "pod", name, "-n", NAMESPACE, "--ignore-not-found", "--wait=true")


def clear_test_workers(rec: Recorder, timeout_seconds: int = 900) -> dict[str, Any]:
    claims = rec.json("kubectl", "get", "nodeclaims.karpenter.sh", "-l", "karpenter.sh/nodepool=phase3-spot", "-o", "json").get("items", [])
    deleted = []
    for claim in claims:
        name = claim.get("metadata", {}).get("name")
        if name:
            result = rec.kubectl("delete", "nodeclaim", name, "--ignore-not-found", "--wait=false")
            deleted.append({"nodeclaim": name, "exit_code": result.returncode})
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        nodes = rec.json("kubectl", "get", "nodes", "-l", "measure-experiment=phase3-spot", "-o", "json").get("items", [])
        remaining_claims = rec.json("kubectl", "get", "nodeclaims.karpenter.sh", "-l", "karpenter.sh/nodepool=phase3-spot", "-o", "json").get("items", [])
        active = rec.json("aws", "ec2", "describe-instances", "--region", REGION, "--filters", "Name=tag:Project,Values=onebite", "Name=tag:Temporary,Values=true", f"Name=tag:measure-experiment,Values={BATCH}", "Name=instance-state-name,Values=pending,running,stopping,stopped", "--output", "json")
        active_instances = [i for r in active.get("Reservations", []) for i in r.get("Instances", [])]
        last = {"nodes": [n.get("metadata", {}).get("name") for n in nodes], "nodeclaims": [c.get("metadata", {}).get("name") for c in remaining_claims], "instances": [i.get("InstanceId") for i in active_instances]}
        if not any(last.values()):
            return {"complete": True, "deleted_nodeclaims": deleted, "last_state": last}
        time.sleep(5)
    return {"complete": False, "deleted_nodeclaims": deleted, "last_state": last}


def main() -> int:
    recorder = Recorder()
    os.environ["AWS_REGION"] = REGION
    outcome: dict[str, Any] = {"batch": BATCH, "started_at_utc": utc_now(), "success": False}
    nginx_applied = False
    curl_applied = False
    source_node = None
    target_node = None
    nginx_pod = None
    failure: str | None = None

    try:
        if not KUBECONFIG.is_file():
            raise RuntimeError(f"Dedicated kubeconfig is missing: {KUBECONFIG}")
        identity = recorder.json("aws", "sts", "get-caller-identity", "--region", REGION, "--output", "json")
        account = identity.get("Account", "")
        if not account.endswith("0497"):
            raise RuntimeError("AWS caller account does not end in 0497")
        recorder.record({"kind": "v5_network_probe_start", "observed_at_utc": utc_now(), "account_suffix": account[-4:], "region": REGION, "cluster": CLUSTER, "kubeconfig": str(KUBECONFIG)})

        nodes = get_nodes(recorder)
        system = [n for n in nodes if n.get("metadata", {}).get("labels", {}).get("measure-pool") == "system" and ready(n)]
        if len(system) != 1:
            raise RuntimeError(f"Expected exactly one Ready system node; found {len(system)}")
        source_node = system[0]
        nginx = pod_manifest(NGINX_NAME, {"app": "phase3-v5-min-nginx", "measure-experiment": BATCH}, {
            "restartPolicy": "Never",
            "nodeSelector": {"measure-pool": "experiment", "measure-experiment": "phase3-spot"},
            "tolerations": [{"key": "measure/only", "operator": "Equal", "value": "experiment", "effect": "NoSchedule"}],
            "containers": [{
                "name": "nginx",
                "image": "nginx:1.27.5-alpine",
                "ports": [{"containerPort": 80, "protocol": "TCP"}],
                "readinessProbe": {"httpGet": {"path": "/", "port": 80}, "initialDelaySeconds": 1, "periodSeconds": 2, "timeoutSeconds": 1, "failureThreshold": 3},
            }],
        })
        apply_manifest(recorder, NGINX_NAME, nginx)
        nginx_applied = True
        nginx_pod = wait_pod_ready(recorder, NGINX_NAME, 900)
        destination_name = nginx_pod.get("spec", {}).get("nodeName")
        nodes = get_nodes(recorder)
        target_node = next((n for n in nodes if n.get("metadata", {}).get("name") == destination_name), None)
        if not target_node or not node_instance_id(target_node):
            raise RuntimeError("Ready nginx Pod has no mappable Spot worker node / EC2 provider ID")
        pod_ip = nginx_pod.get("status", {}).get("podIP")
        if not pod_ip:
            raise RuntimeError("Ready nginx Pod has no Pod IP")
        outcome.update({
            "system_node": {"name": source_node.get("metadata", {}).get("name"), "provider_id": source_node.get("spec", {}).get("providerID")},
            "spot_node": {"name": destination_name, "provider_id": target_node.get("spec", {}).get("providerID"), "capacity_type": target_node.get("metadata", {}).get("labels", {}).get("karpenter.sh/capacity-type")},
            "nginx_pod": {"name": NGINX_NAME, "ip": pod_ip, "node": destination_name},
            "probe": "curl --connect-timeout 5 http://<spot-pod-ip>:80/",
        })
        recorder.record({"kind": "v5_nginx_test_pod_ready", "observed_at_utc": utc_now(), **outcome})

        curl_script = f"curl -sS --connect-timeout 5 --max-time 10 -o /dev/null -w 'HTTP_STATUS=%{{http_code}}\\n' http://{pod_ip}:80/"
        curl = pod_manifest(CURL_NAME, {"app": "phase3-v5-min-curl", "measure-experiment": BATCH}, {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": 45,
            "nodeSelector": {"measure-pool": "system"},
            "automountServiceAccountToken": False,
            "containers": [{"name": "curl", "image": "curlimages/curl:8.12.1", "command": ["sh", "-c", curl_script]}],
        })
        apply_manifest(recorder, CURL_NAME, curl)
        curl_applied = True
        curl_status = wait_pod_terminal(recorder, CURL_NAME, 60)
        curl_logs = recorder.kubectl("logs", CURL_NAME, "-n", NAMESPACE)
        curl_text = curl_logs.stdout
        result_phase = curl_status.get("status", {}).get("phase")
        success = result_phase == "Succeeded" and "HTTP_STATUS=200" in curl_text
        outcome.update({
            "curl_pod": {"name": CURL_NAME, "phase": result_phase, "node": curl_status.get("spec", {}).get("nodeName")},
            "curl_stdout": curl_text,
            "curl_stderr": curl_logs.stderr,
            "http_200": success,
            "finished_at_utc": utc_now(),
        })
        recorder.record({"kind": "v5_system_to_spot_pod_http_result", "observed_at_utc": utc_now(), **outcome})
        if not success:
            failure = f"System node curl to Spot Pod IP {pod_ip}:80 did not return HTTP 200"
            capture_failure_diagnostics(recorder, source_node, target_node, nginx_pod, failure)
        else:
            outcome["success"] = True

    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        outcome["error"] = failure
        recorder.record({"kind": "v5_network_probe_error", "observed_at_utc": utc_now(), "error": failure})
        if source_node or target_node:
            try:
                capture_failure_diagnostics(recorder, source_node, target_node, nginx_pod, failure)
            except Exception as diag_exc:
                recorder.record({"kind": "v5_failure_diagnostic_collection_error", "observed_at_utc": utc_now(), "error": str(diag_exc)})

    finally:
        if curl_applied:
            delete_pod(recorder, CURL_NAME)
        if nginx_applied:
            delete_pod(recorder, NGINX_NAME)
        if nginx_applied:
            outcome["worker_cleanup"] = clear_test_workers(recorder)
        outcome["completed_at_utc"] = utc_now()
        outcome["success"] = bool(outcome.get("success"))
        (OUT / "network-probe-summary.json").write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        recorder.record({"kind": "v5_network_probe_complete", "observed_at_utc": utc_now(), "summary": outcome})

    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0 if outcome.get("success") and outcome.get("worker_cleanup", {}).get("complete") else 1


if __name__ == "__main__":
    sys.exit(main())
