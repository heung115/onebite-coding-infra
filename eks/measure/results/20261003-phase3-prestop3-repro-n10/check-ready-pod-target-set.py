#!/usr/bin/env python3
"""Read-only gate: Ready Pod IPs must exactly match healthy ALB target IPs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REGION = "ap-northeast-2"
CLUSTER = "onebite-eks-measure"
CONTEXT = CLUSTER
NAMESPACE = "measure"
KUBECONFIG = Path.home() / ".kube" / "onebite-eks.kubeconfig"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def command(argv: list[str], *, kubectl: bool = False) -> dict[str, Any]:
    env = os.environ.copy()
    if kubectl:
        env["KUBECONFIG"] = str(KUBECONFIG)
    started = now()
    result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    row: dict[str, Any] = {
        "kind": "command",
        "started_at_utc": started,
        "finished_at_utc": now(),
        "argv": argv,
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    if kubectl:
        row["environment"] = {"KUBECONFIG": str(KUBECONFIG), "context": CONTEXT}
    return row


def query_json(argv: list[str], *, kubectl: bool = False) -> tuple[dict[str, Any] | list[Any], dict[str, Any]]:
    row = command(argv, kubectl=kubectl)
    if row["exit_code"]:
        raise RuntimeError(f"command failed ({row['exit_code']}): {' '.join(argv)}: {row['stderr'].strip()}")
    try:
        return json.loads(row["stdout"]), row
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command returned invalid JSON: {' '.join(argv)}") from exc


def ready(pod: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in pod.get("status", {}).get("conditions", [])
    )


def node_ready(node: dict[str, Any]) -> bool:
    return any(
        condition.get("type") == "Ready" and condition.get("status") == "True"
        for condition in node.get("status", {}).get("conditions", [])
    )


def record(stream: Any, row: dict[str, Any]) -> None:
    stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    stream.flush()


def verify_live_workload_condition(stream: Any, attempt: int) -> bool:
    """Check the fixed preStop3 workload without recording unrelated Pod env data."""
    deploy_argv = [
        "kubectl", "--context", CONTEXT, "get", "deployment", "backend-probe",
        "-n", NAMESPACE, "-o", "json",
    ]
    deploy_row = command(deploy_argv, kubectl=True)
    deployment: dict[str, Any] = {}
    if deploy_row["exit_code"] == 0:
        try:
            deployment = json.loads(deploy_row["stdout"])
        except json.JSONDecodeError:
            pass

    pdb_argv = ["kubectl", "--context", CONTEXT, "get", "pdb", "backend-probe", "-n", NAMESPACE, "-o", "json"]
    pdb_row = command(pdb_argv, kubectl=True)
    pdb: dict[str, Any] = {}
    if pdb_row["exit_code"] == 0:
        try:
            pdb = json.loads(pdb_row["stdout"])
        except json.JSONDecodeError:
            pass

    spec = deployment.get("spec", {})
    template = spec.get("template", {}).get("spec", {})
    containers = template.get("containers", [])
    container = next((item for item in containers if item.get("name") == "nginx"), {})
    pre_stop = container.get("lifecycle", {}).get("preStop", {}).get("exec", {}).get("command")
    readiness = container.get("readinessProbe", {}).get("httpGet", {})
    readiness_spec = container.get("readinessProbe", {})
    spread = template.get("topologySpreadConstraints", [])
    spread_row = spread[0] if len(spread) == 1 else {}
    pdb_min = pdb.get("spec", {}).get("minAvailable")
    container_ports = container.get("ports", [])
    resources = container.get("resources", {})
    expected_readiness = {
        "path": "/",
        "port": "http",
        "periodSeconds": 2,
        "timeoutSeconds": 1,
        "failureThreshold": 2,
    }
    checks = {
        "deployment_readable": deploy_row["exit_code"] == 0 and bool(deployment),
        "replicas_two": spec.get("replicas") == 2,
        "prestop_sleep_three_seconds": pre_stop == ["/bin/sh", "-c", "sleep 3"],
        "termination_grace_period_30_seconds": template.get("terminationGracePeriodSeconds") == 30,
        "image_unchanged": container.get("image") == "nginx:1.27.5-alpine",
        "readiness_unchanged": (
            readiness.get("path") == expected_readiness["path"]
            and readiness.get("port") == expected_readiness["port"]
            and all(readiness_spec.get(key) == value for key, value in expected_readiness.items() if key not in ("path", "port"))
        ),
        "container_port_unchanged": any(port.get("name") == "http" and port.get("containerPort") == 80 for port in container_ports),
        "resource_requests_limits_unchanged": resources == {
            "requests": {"cpu": "100m", "memory": "64Mi"},
            "limits": {"cpu": "250m", "memory": "128Mi"},
        },
        "node_selector_unchanged": template.get("nodeSelector") == {
            "measure-pool": "experiment",
            "measure-experiment": "phase3-spot",
        },
        "toleration_unchanged": template.get("tolerations") == [{
            "key": "measure/only",
            "operator": "Equal",
            "value": "experiment",
            "effect": "NoSchedule",
        }],
        "hostname_spread_unchanged": (
            len(spread) == 1
            and spread_row.get("topologyKey") == "kubernetes.io/hostname"
            and spread_row.get("maxSkew") == 1
            and spread_row.get("minDomains") == 2
            and spread_row.get("whenUnsatisfiable") == "DoNotSchedule"
            and spread_row.get("labelSelector", {}).get("matchLabels") == {"app": "backend-probe"}
        ),
        "pdb_min_available_one": pdb_row["exit_code"] == 0 and pdb_min in (1, "1"),
    }
    details = {
        "replicas": spec.get("replicas"),
        "preStop_command": pre_stop,
        "termination_grace_period_seconds": template.get("terminationGracePeriodSeconds"),
        "image": container.get("image"),
        "container_ports": container_ports,
        "resources": resources,
        "node_selector": template.get("nodeSelector"),
        "tolerations": template.get("tolerations"),
        "readiness_probe": readiness_spec,
        "topology_spread_constraints": spread,
        "pdb_min_available": pdb_min,
    }
    record(stream, {
        "kind": "fixed_live_workload_condition_check",
        "attempt": attempt,
        "observed_at_utc": now(),
        "commands": [
            {"argv": deploy_argv, "exit_code": deploy_row["exit_code"], "kubeconfig": str(KUBECONFIG)},
            {"argv": pdb_argv, "exit_code": pdb_row["exit_code"], "kubeconfig": str(KUBECONFIG)},
        ],
        "checks": checks,
        "details": details,
        "passed": all(checks.values()),
        "note": "Full Deployment JSON and possible environment values are intentionally not copied into the raw log.",
    })
    return all(checks.values())


def observe(stream: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    kubectl_prefix = ["kubectl", "--context", CONTEXT]
    pods, pods_cmd = query_json(
        kubectl_prefix + ["get", "pods", "-n", NAMESPACE, "-l", "app=backend-probe", "-o", "json"],
        kubectl=True,
    )
    nodes, nodes_cmd = query_json(
        kubectl_prefix + ["get", "nodes", "-l", "karpenter.sh/nodepool=phase3-spot", "-o", "json"],
        kubectl=True,
    )
    ingress, ingress_cmd = query_json(
        kubectl_prefix + ["get", "ingress", "backend-probe", "-n", NAMESPACE, "-o", "json"],
        kubectl=True,
    )
    for cmd in (pods_cmd, nodes_cmd, ingress_cmd):
        record(stream, cmd)

    pod_items = pods.get("items", []) if isinstance(pods, dict) else []
    node_items = nodes.get("items", []) if isinstance(nodes, dict) else []
    ready_pods = [
        pod for pod in pod_items
        if ready(pod) and not pod.get("metadata", {}).get("deletionTimestamp")
    ]
    ready_workers = [
        node for node in node_items
        if node_ready(node) and not node.get("metadata", {}).get("deletionTimestamp")
    ]
    pod_ips = sorted(pod.get("status", {}).get("podIP", "") for pod in ready_pods if pod.get("status", {}).get("podIP"))
    pod_nodes = sorted({pod.get("spec", {}).get("nodeName") for pod in ready_pods if pod.get("spec", {}).get("nodeName")})
    worker_names = {node.get("metadata", {}).get("name") for node in ready_workers}

    addresses = ingress.get("status", {}).get("loadBalancer", {}).get("ingress", []) if isinstance(ingress, dict) else []
    host = next((item.get("hostname") for item in addresses if item.get("hostname")), None)
    snapshot: dict[str, Any] = {
        "observed_at_utc": now(),
        "ready_spot_worker_count": len(ready_workers),
        "ready_spot_workers": [
            {"name": n.get("metadata", {}).get("name"), "provider_id": n.get("spec", {}).get("providerID"),
             "instance_type": n.get("metadata", {}).get("labels", {}).get("node.kubernetes.io/instance-type"),
             "zone": n.get("metadata", {}).get("labels", {}).get("topology.kubernetes.io/zone")}
            for n in ready_workers
        ],
        "ready_pod_count": len(ready_pods),
        "ready_pods": [
            {"name": p.get("metadata", {}).get("name"), "ip": p.get("status", {}).get("podIP"),
             "node_name": p.get("spec", {}).get("nodeName")}
            for p in ready_pods
        ],
        "ready_pod_ips": pod_ips,
        "ready_pod_node_names": pod_nodes,
        "ingress_hostname": host,
        "target_group_arn": None,
        "targets": [],
        "healthy_target_ips": [],
        "gate_checks": {},
    }

    if host:
        lbs, lbs_cmd = query_json(
            ["aws", "elbv2", "describe-load-balancers", "--region", REGION, "--output", "json"]
        )
        record(stream, lbs_cmd)
        matching_lbs = [lb for lb in lbs.get("LoadBalancers", []) if lb.get("DNSName") == host]
        snapshot["matching_load_balancer_arns"] = [lb.get("LoadBalancerArn") for lb in matching_lbs]
        if len(matching_lbs) == 1:
            lb_arn = matching_lbs[0]["LoadBalancerArn"]
            groups, groups_cmd = query_json(
                ["aws", "elbv2", "describe-target-groups", "--load-balancer-arn", lb_arn, "--region", REGION, "--output", "json"]
            )
            record(stream, groups_cmd)
            target_groups = groups.get("TargetGroups", [])
            snapshot["target_group_arns"] = [tg.get("TargetGroupArn") for tg in target_groups]
            if len(target_groups) == 1:
                tg_arn = target_groups[0]["TargetGroupArn"]
                snapshot["target_group_arn"] = tg_arn
                health, health_cmd = query_json(
                    ["aws", "elbv2", "describe-target-health", "--target-group-arn", tg_arn, "--region", REGION, "--output", "json"]
                )
                record(stream, health_cmd)
                targets = health.get("TargetHealthDescriptions", [])
                snapshot["targets"] = [
                    {"ip": row.get("Target", {}).get("Id"), "port": row.get("Target", {}).get("Port"),
                     "state": row.get("TargetHealth", {}).get("State"),
                     "reason": row.get("TargetHealth", {}).get("Reason"),
                     "description": row.get("TargetHealth", {}).get("Description")}
                    for row in targets
                ]
                snapshot["healthy_target_ips"] = sorted(
                    row.get("Target", {}).get("Id", "") for row in targets
                    if row.get("TargetHealth", {}).get("State") == "healthy"
                )

    target_rows = snapshot["targets"]
    checks = {
        "exactly_two_ready_spot_workers": len(ready_workers) == 2,
        "exactly_two_ready_pods": len(ready_pods) == 2,
        "pods_on_distinct_ready_spot_workers": len(pod_nodes) == 2 and set(pod_nodes) == worker_names,
        "two_pods_have_ip": len(pod_ips) == 2,
        "one_ingress_load_balancer": len(snapshot.get("matching_load_balancer_arns", [])) == 1,
        "one_backend_target_group": len(snapshot.get("target_group_arns", [])) == 1,
        "exactly_two_target_rows": len(target_rows) == 2,
        "all_target_ports_tcp_80": len(target_rows) == 2 and all(row.get("port") == 80 for row in target_rows),
        "both_targets_healthy": len(target_rows) == 2 and all(row.get("state") == "healthy" for row in target_rows),
        "healthy_target_ips_equal_ready_pod_ips": len(pod_ips) == 2 and snapshot["healthy_target_ips"] == pod_ips,
        "all_target_ips_equal_ready_pod_ips": len(target_rows) == 2 and sorted(row.get("ip", "") for row in target_rows) == pod_ips,
    }
    snapshot["gate_checks"] = checks
    snapshot["gate_passed"] = all(checks.values())
    return snapshot, {"pods": pods, "nodes": nodes, "ingress": ingress}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--attempt", type=int, required=True, choices=range(1, 11))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--interval-seconds", type=int, default=15)
    args = parser.parse_args()
    if not KUBECONFIG.is_file():
        print(f"Dedicated kubeconfig is missing: {KUBECONFIG}", file=sys.stderr)
        return 2

    deadline = time.monotonic() + args.timeout_seconds
    with args.output.open("x", encoding="utf-8") as stream:
        record(stream, {
            "kind": "pre_attempt_exact_target_set_gate_started",
            "attempt": args.attempt,
            "started_at_utc": now(),
            "timeout_seconds": args.timeout_seconds,
            "poll_interval_seconds": args.interval_seconds,
            "cluster": CLUSTER,
            "namespace": NAMESPACE,
            "kubectl_context": CONTEXT,
            "kubeconfig": str(KUBECONFIG),
            "read_only": True,
        })
        if not verify_live_workload_condition(stream, args.attempt):
            record(stream, {
                "kind": "pre_attempt_fixed_workload_condition_failed",
                "attempt": args.attempt,
                "finished_at_utc": now(),
                "fis_started": False,
                "failure_policy": "fixed attempt recorded; no FIS and no replacement attempt",
            })
            return 11
        poll = 0
        last_error = None
        while time.monotonic() < deadline:
            poll += 1
            try:
                snapshot, _ = observe(stream)
                snapshot["kind"] = "pre_attempt_exact_target_set_observation"
                snapshot["attempt"] = args.attempt
                snapshot["poll"] = poll
                record(stream, snapshot)
                if snapshot["gate_passed"]:
                    record(stream, {
                        "kind": "pre_attempt_exact_target_set_gate_passed",
                        "attempt": args.attempt,
                        "observed_at_utc": now(),
                        "polls": poll,
                        "pod_ips": snapshot["ready_pod_ips"],
                        "healthy_target_ips": snapshot["healthy_target_ips"],
                    })
                    return 0
                last_error = None
            except Exception as exc:  # preserve transient API/controller errors and keep waiting
                last_error = f"{type(exc).__name__}: {exc}"
                record(stream, {
                    "kind": "pre_attempt_exact_target_set_observation_error",
                    "attempt": args.attempt,
                    "poll": poll,
                    "observed_at_utc": now(),
                    "error": last_error,
                })
            time.sleep(args.interval_seconds)
        record(stream, {
            "kind": "pre_attempt_exact_target_set_gate_failed",
            "attempt": args.attempt,
            "finished_at_utc": now(),
            "polls": poll,
            "timeout_seconds": args.timeout_seconds,
            "last_error": last_error,
            "failure_policy": "fixed attempt recorded; no FIS and no replacement attempt",
        })
    return 10


if __name__ == "__main__":
    raise SystemExit(main())
