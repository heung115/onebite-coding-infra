#!/usr/bin/env python3
"""Run and record one Phase 2 instance-selection trial."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-northeast-2"
CONTEXT = "onebite-eks-measure"
KUBECONFIG = Path.home() / ".kube" / "onebite-eks.kubeconfig"
POOLS = {"phase2-fixed", "phase2-flexible", "experiment"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_module(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper module: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Trial:
    def __init__(self, raw_path: Path) -> None:
        self.raw_path = raw_path
        raw_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, item: dict[str, Any]) -> None:
        with self.raw_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    def kubectl(self, *args: str, stdin: str | None = None, omit_output: bool = False) -> subprocess.CompletedProcess[str]:
        argv = ["env", f"KUBECONFIG={KUBECONFIG}", "kubectl", "--context", CONTEXT, *args]
        started = utc_now()
        result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
        event: dict[str, Any] = {
            "kind": "command",
            "started_at_utc": started,
            "finished_at_utc": utc_now(),
            "argv": argv,
            "environment": {"KUBECONFIG": str(KUBECONFIG), "context": CONTEXT},
            "exit_code": result.returncode,
            "stderr": result.stderr,
        }
        if omit_output:
            event["stdout_omitted"] = True
            event["stdout_bytes"] = len(result.stdout.encode("utf-8"))
        else:
            event["stdout"] = result.stdout
        self.record(event)
        return result

    def kubectl_json(self, *args: str, omit_output: bool = False) -> dict[str, Any]:
        result = self.kubectl(*args, omit_output=omit_output)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "kubectl failed")
        return json.loads(result.stdout)

    def aws(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = ["aws", *args]
        started = utc_now()
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.record({
            "kind": "command", "started_at_utc": started, "finished_at_utc": utc_now(),
            "argv": argv, "environment": {"region": REGION}, "exit_code": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr,
        })
        return result


def nodeclaims(trial: Trial) -> list[dict[str, Any]]:
    payload = trial.kubectl_json("get", "nodeclaims.karpenter.sh", "-o", "json")
    return [
        {
            "name": item.get("metadata", {}).get("name"),
            "pool": item.get("metadata", {}).get("labels", {}).get("karpenter.sh/nodepool"),
            "phase": item.get("status", {}).get("phase"),
            "provider_id": item.get("status", {}).get("providerID"),
            "conditions": [
                {
                    "type": condition.get("type"), "status": condition.get("status"),
                    "reason": condition.get("reason"), "message": condition.get("message"),
                    "last_transition_at_utc": condition.get("lastTransitionTime"),
                }
                for condition in item.get("status", {}).get("conditions", [])
            ],
        }
        for item in payload.get("items", [])
        if item.get("metadata", {}).get("labels", {}).get("karpenter.sh/nodepool") in POOLS
    ]


def benchmark_pods(trial: Trial) -> list[dict[str, Any]]:
    payload = trial.kubectl_json("get", "pods", "-A", "-o", "json", omit_output=True)
    labels = {"scale-probe", "bulk-probe", "phase2-probe", "phase2-calibration"}
    return [
        {
            "namespace": pod.get("metadata", {}).get("namespace"),
            "name": pod.get("metadata", {}).get("name"),
            "app": pod.get("metadata", {}).get("labels", {}).get("app"),
            "phase": pod.get("status", {}).get("phase"),
        }
        for pod in payload.get("items", [])
        if pod.get("metadata", {}).get("labels", {}).get("app") in labels
    ]


def record_zero_state(trial: Trial, module: Any) -> dict[str, Any]:
    pods = benchmark_pods(trial)
    nodes = module.node_summaries(trial.kubectl_json("get", "nodes", "-o", "json"))
    claims = nodeclaims(trial)
    instances = module.experiment_instances(module.Collector(trial.raw_path))
    active_instances = [item for item in instances if item.get("state") != "terminated"]
    result = {"pods": pods, "experiment_nodes": nodes, "nodeclaims": claims, "active_instances": active_instances}
    trial.record({"kind": "zero_precondition", "observed_at_utc": utc_now(), **result})
    return result


def workload_manifest(condition: str, profile: dict[str, Any]) -> str:
    resources = profile["requests_per_pod"]
    payload = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": "phase2-probe", "namespace": "measure", "labels": {"app": "phase2-probe"}},
        "spec": {
            "replicas": 0,
            "selector": {"matchLabels": {"app": "phase2-probe"}},
            "template": {
                "metadata": {"labels": {"app": "phase2-probe"}},
                "spec": {
                    "nodeSelector": {"measure-pool": "experiment", "measure-condition": condition},
                    "tolerations": [{"key": "measure/only", "operator": "Equal", "value": "experiment", "effect": "NoSchedule"}],
                    "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.10", "resources": {"requests": resources}}],
                },
            },
        },
    }
    return json.dumps(payload, indent=2) + "\n"


def snapshot(trial: Trial, module: Any, condition: str, profile_name: str) -> dict[str, Any]:
    pods_payload = trial.kubectl_json("get", "pods", "-n", "measure", "-l", "app=phase2-probe", "-o", "json")
    all_pods_payload = trial.kubectl_json("get", "pods", "-A", "-o", "json", omit_output=True)
    nodes_payload = trial.kubectl_json("get", "nodes", "-o", "json")
    claims = nodeclaims(trial)
    instances = module.experiment_instances(module.Collector(trial.raw_path))
    pods = module.pod_summaries(pods_payload, "phase2-probe")
    nodes = module.node_summaries(nodes_payload)
    node_names = {node["name"] for node in nodes}
    scheduled = [
        pod for pod in all_pods_payload.get("items", [])
        if pod.get("spec", {}).get("nodeName") in node_names
        and pod.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
    ]
    return {
        "kind": "snapshot",
        "observed_at_utc": utc_now(),
        "condition": condition,
        "profile": profile_name,
        "pods": pods,
        "experiment_nodes": nodes,
        "experiment_instances": instances,
        "experiment_nodeclaims": claims,
        "scheduled_pod_requests": [
            {
                "namespace": pod.get("metadata", {}).get("namespace"),
                "name": pod.get("metadata", {}).get("name"),
                "app": pod.get("metadata", {}).get("labels", {}).get("app"),
                "node": pod.get("spec", {}).get("nodeName"),
                "phase": pod.get("status", {}).get("phase"),
                "owner_kinds": [owner.get("kind") for owner in pod.get("metadata", {}).get("ownerReferences", [])],
                "requests": module.phase2.effective_pod_requests(pod),
            }
            for pod in scheduled
        ],
    }


def quantity_text(cpu_m: int, memory_bytes: int) -> dict[str, str]:
    memory_mib = Decimal(memory_bytes) / Decimal(2**20)
    return {"cpu": f"{cpu_m}m", "memory_mib": str(memory_mib.quantize(Decimal("0.01")))}


def final_resource_metrics(
    final: dict[str, Any],
    profile: dict[str, Any],
    prices: dict[str, Any],
    module: Any,
) -> dict[str, Any]:
    nodes = final["experiment_nodes"]
    pods = final["scheduled_pod_requests"]
    workload = [item for item in pods if item.get("app") == "phase2-probe"]
    daemon = [item for item in pods if "DaemonSet" in item.get("owner_kinds", [])]
    benchmark_cpu, benchmark_mem = module.phase2.sum_requests([
        {
            "spec": {"containers": [{"resources": {"requests": profile["requests_per_pod"]}}]},
        }
        for _ in range(2)
    ])
    total_cpu = total_mem = alloc_cpu = alloc_mem = 0
    by_type: dict[str, int] = {}
    by_family: dict[str, int] = {}
    by_size: dict[str, int] = {}
    per_node = []
    for node in nodes:
        node_cpu = module.phase2.parse_cpu(node.get("allocatable_cpu"))
        node_mem = module.phase2.parse_memory(node.get("allocatable_memory"))
        scheduled_on_node = [item for item in pods if item.get("node") == node["name"]]
        requested_cpu = sum(item["requests"][0] for item in scheduled_on_node)
        requested_mem = sum(item["requests"][1] for item in scheduled_on_node)
        alloc_cpu += node_cpu
        alloc_mem += node_mem
        total_cpu += requested_cpu
        total_mem += requested_mem
        instance_type = node.get("instance_type") or "unknown"
        by_type[instance_type] = by_type.get(instance_type, 0) + 1
        family, separator, size = instance_type.rpartition(".")
        if separator:
            by_family[family] = by_family.get(family, 0) + 1
            by_size[size] = by_size.get(size, 0) + 1
        benchmark_on_node = [item for item in workload if item.get("node") == node["name"]]
        benchmark_cpu_on_node = sum(item["requests"][0] for item in benchmark_on_node)
        benchmark_mem_on_node = sum(item["requests"][1] for item in benchmark_on_node)
        per_node.append({
            "name": node["name"], "instance_type": instance_type,
            "allocatable_cpu": node.get("allocatable_cpu"), "allocatable_memory": node.get("allocatable_memory"),
            "requested_cpu": f"{requested_cpu}m", "requested_memory_bytes": requested_mem,
            "unused_cpu": f"{node_cpu - requested_cpu}m", "unused_memory_bytes": node_mem - requested_mem,
            "benchmark_pod_count": len(benchmark_on_node),
            "benchmark_pod_names": [item.get("name") for item in benchmark_on_node],
            "benchmark_requested_cpu": f"{benchmark_cpu_on_node}m",
            "benchmark_requested_memory_bytes": benchmark_mem_on_node,
        })
    rates = prices.get("on_demand_linux_shared_hourly_prices", {})
    capacity_rates: dict[str, Any] = {}
    total_capacity_rate = Decimal("0")
    pricing_complete = True
    for instance_type, count in sorted(by_type.items()):
        rows = rates.get(instance_type, [])
        if not rows:
            capacity_rates[instance_type] = {"count": count, "list_rate_usd_per_instance_hour": None}
            pricing_complete = False
            continue
        rate = Decimal(rows[0]["usd_per_hour"])
        total_capacity_rate += rate * count
        capacity_rates[instance_type] = {
            "count": count,
            "list_rate_usd_per_instance_hour": str(rate),
            "provisioned_capacity_rate_usd_per_hour": str(rate * count),
        }
    return {
        "instance_type_counts": by_type,
        "instance_family_counts": by_family,
        "instance_size_counts": by_size,
        "node_count": len(nodes),
        "allocatable": quantity_text(alloc_cpu, alloc_mem),
        "benchmark_requested": quantity_text(benchmark_cpu, benchmark_mem),
        "all_scheduled_pod_requested": quantity_text(total_cpu, total_mem),
        "daemonset_requested": quantity_text(sum(item["requests"][0] for item in daemon), sum(item["requests"][1] for item in daemon)),
        "unused": quantity_text(alloc_cpu - total_cpu, alloc_mem - total_mem),
        "request_to_allocatable_ratio": {
            "cpu": round(total_cpu / alloc_cpu, 6) if alloc_cpu else None,
            "memory": round(total_mem / alloc_mem, 6) if alloc_mem else None,
        },
        "provisioned_ec2_capacity_rate_by_type": capacity_rates,
        "provisioned_ec2_capacity_rate_usd_per_hour": str(total_capacity_rate) if pricing_complete else None,
        "provisioned_ec2_capacity_rate_basis": "sum of Seoul Linux Shared On-Demand list rates for the provisioned experiment EC2 instance counts; a capacity rate, not actual billed spend or savings",
        "instance_pricing_complete": pricing_complete,
        "per_node": per_node,
    }


def cleanup(trial: Trial, timeout: int) -> None:
    trial.kubectl("delete", "deployment", "phase2-probe", "-n", "measure", "--ignore-not-found", "--wait=true")
    for item in nodeclaims(trial):
        trial.kubectl("delete", "nodeclaim", item["name"], "--ignore-not-found", "--wait=false")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        podlist = benchmark_pods(trial)
        nodes = trial.kubectl_json("get", "nodes", "-l", "measure-pool=experiment", "-o", "json").get("items", [])
        claims = nodeclaims(trial)
        ec2 = trial.aws(
            "ec2", "describe-instances", "--region", REGION,
            "--filters", "Name=tag:Project,Values=onebite", "Name=tag:Temporary,Values=true",
            "Name=tag:measure-pool,Values=experiment",
            "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down",
            "--query", "Reservations[].Instances[].{id:InstanceId,state:State.Name}", "--output", "json",
        )
        if ec2.returncode:
            raise RuntimeError(ec2.stderr.strip() or "could not check EC2 termination")
        instances = json.loads(ec2.stdout or "[]")
        trial.record({"kind": "cleanup_snapshot", "observed_at_utc": utc_now(), "pods": podlist, "nodes": len(nodes), "nodeclaims": claims, "active_instances": instances})
        if not podlist and not nodes and not claims and not instances:
            return
        time.sleep(2)
    raise TimeoutError("benchmark Pods, experiment Nodes, NodeClaims, or EC2 instances did not terminate")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, required=True)
    parser.add_argument("--condition", choices=["fixed", "flexible"], required=True)
    parser.add_argument("--profile", choices=["general", "cpu-heavy", "memory-heavy"], required=True)
    parser.add_argument("--trial", type=int, required=True, help="1–3 within this condition and profile")
    parser.add_argument("--pair", required=True)
    parser.add_argument("--order-index", type=int, required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--cleanup-timeout", type=int, default=900)
    parser.add_argument("--execute", action="store_true", help="Required acknowledgement to create Pods and workers.")
    args = parser.parse_args()
    if not args.execute:
        parser.error("trial creates Pods and EC2 workers; pass --execute only after separate approval")
    if os.environ.get("ALLOW_PHASE2_EXECUTION") != "1":
        parser.error("set ALLOW_PHASE2_EXECUTION=1 only after the separate Phase 2 approval")
    if not KUBECONFIG.is_file():
        parser.error(f"dedicated kubeconfig is missing: {KUBECONFIG}")
    if args.trial not in {1, 2, 3}:
        parser.error("each profile/condition is preregistered for exactly three trials")

    session = args.session_dir.resolve()
    output = session / f"phase2-{args.profile}-{args.condition}-{args.trial:02d}.json"
    raw_path = output.with_suffix(".jsonl")
    if output.exists() or raw_path.exists():
        parser.error("refusing to overwrite an existing Phase 2 trial")
    profiles_path = session / "profile-requests.json"
    prices_path = session / "candidate-inventory-and-pricing.json"
    if not profiles_path.is_file() or not prices_path.is_file():
        parser.error("frozen profile-requests.json and current pricing inventory are required")

    profile_bundle = json.loads(profiles_path.read_text(encoding="utf-8"))
    profile = profile_bundle["profiles"][args.profile]
    price_snapshot = json.loads(prices_path.read_text(encoding="utf-8"))
    trial = Trial(raw_path)
    module = load_module("onebite_collect_trial", ROOT / "measure/scripts/collect-trial.py")
    module.phase2 = load_module("onebite_phase2_calibration", ROOT / "measure/scripts/calibrate-phase2.py")
    started = utc_now()
    started_mono = time.monotonic()
    requested_at: str | None = None
    markers: dict[str, Any] = {
        "first_pending_observed_at_utc": None,
        "first_unschedulable_at_utc": None,
        "first_ec2_launch_at_utc": None,
        "nodes": {},
        "pods": {},
        "all_pods_ready_at_utc": None,
    }
    summary: dict[str, Any] = {
        "condition": args.condition, "profile": args.profile, "trial": args.trial,
        "pair": args.pair, "order_index": args.order_index, "replicas": 2,
        "requests_per_pod": profile["requests_per_pod"],
        "request_derivation": profile.get("derived_from"),
        "kubeconfig": str(KUBECONFIG), "started_at_utc": started,
        "raw_file": str(raw_path), "calibration_file": str(profiles_path),
        "pricing_file": str(prices_path),
    }
    cleanup_ok = False
    execution_started = False
    try:
        module.verify_aws_target(module.Collector(raw_path))
        module.controller_health(module.Collector(raw_path), "karpenter")
        zero = record_zero_state(trial, module)
        if zero["pods"] or zero["experiment_nodes"] or zero["nodeclaims"] or zero["active_instances"]:
            raise RuntimeError("pre-run zero check failed: benchmark Pods/workers/NodeClaims are not all zero or prior EC2 termination is incomplete")
        group = trial.aws(
            "eks", "describe-nodegroup", "--region", REGION, "--cluster-name", "onebite-eks-measure",
            "--nodegroup-name", "onebite-eks-measure-experiment",
            "--query", "nodegroup.scalingConfig.desiredSize", "--output", "text",
        )
        if group.returncode or group.stdout.strip() != "0":
            raise RuntimeError("managed experiment node group is not at zero desired workers")

        manifest = workload_manifest(args.condition, profile)
        trial.record({"kind": "workload_manifest", "observed_at_utc": utc_now(), "condition": args.condition, "profile": args.profile, "manifest": json.loads(manifest)})
        execution_started = True
        applied = trial.kubectl("apply", "-f", "-", stdin=manifest)
        if applied.returncode:
            raise RuntimeError(applied.stderr.strip() or "failed to apply phase2 workload")
        requested_at = utc_now()
        scale = trial.kubectl("scale", "deployment/phase2-probe", "-n", "measure", "--replicas=2")
        if scale.returncode:
            raise RuntimeError(scale.stderr.strip() or "failed to scale Phase 2 workload")
        summary["scale_requested_at_utc"] = requested_at

        deadline = time.monotonic() + args.timeout
        final: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            current = snapshot(trial, module, args.condition, args.profile)
            trial.record(current)
            pods = current["pods"]
            nodes = current["experiment_nodes"]
            if pods and markers["first_pending_observed_at_utc"] is None and any(item["phase"] == "Pending" for item in pods):
                markers["first_pending_observed_at_utc"] = current["observed_at_utc"]
            unsched = [item["unschedulable_at_utc"] for item in pods if item.get("unschedulable_at_utc")]
            if unsched and markers["first_unschedulable_at_utc"] is None:
                markers["first_unschedulable_at_utc"] = min(unsched)
            for node in nodes:
                markers["nodes"].setdefault(node["name"], {
                    "registered_at_utc": node.get("created_at_utc"),
                    "ready_at_utc": node.get("ready_at_utc"),
                    "instance_type": node.get("instance_type"),
                    "provider_id": node.get("provider_id"),
                })
                if node.get("ready_at_utc"):
                    markers["nodes"][node["name"]]["ready_at_utc"] = node["ready_at_utc"]
            for pod in pods:
                ready_at = pod.get("ready_at_utc")
                markers["pods"].setdefault(pod["name"], {"created_at_utc": pod.get("created_at_utc"), "ready_at_utc": ready_at, "node": pod.get("node")})
                if ready_at:
                    markers["pods"][pod["name"]]["ready_at_utc"] = ready_at
                    markers["pods"][pod["name"]]["node"] = pod.get("node")
            newly_launched = [item for item in current["experiment_instances"] if item.get("launch_time") and item.get("state") not in {"terminated", "shutting-down"}]
            if newly_launched:
                first_launch = min(item["launch_time"] for item in newly_launched)
                if markers["first_ec2_launch_at_utc"] is None or first_launch < markers["first_ec2_launch_at_utc"]:
                    markers["first_ec2_launch_at_utc"] = first_launch
            all_ready = len(pods) == 2 and all(item["ready"] for item in pods) and bool(nodes) and all(item["ready"] for item in nodes)
            if all_ready:
                markers["all_pods_ready_at_utc"] = current["observed_at_utc"]
                final = current
                break
            time.sleep(1)

        final = final or snapshot(trial, module, args.condition, args.profile)
        succeeded = bool(
            len(final["pods"]) == 2 and all(item["ready"] for item in final["pods"])
            and final["experiment_nodes"] and all(item["ready"] for item in final["experiment_nodes"])
        )
        summary["resource_metrics"] = final_resource_metrics(final, profile, price_snapshot, module)
        if not succeeded:
            summary["failure_reason"] = "all two benchmark Pods and their worker Nodes did not become Ready before timeout"
            summary["failure_details"] = {
                "pods": [
                    {"name": pod.get("name"), "phase": pod.get("phase"), "reason": pod.get("reason"), "conditions": pod.get("conditions")}
                    for pod in final["pods"] if not pod.get("ready")
                ],
                "nodeclaims": final["experiment_nodeclaims"],
                "nodes_not_ready": [node for node in final["experiment_nodes"] if not node.get("ready")],
            }
        summary.update({
            "finished_at_utc": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started_mono, 3),
            "scale_request_to_all_pods_ready_seconds": round((datetime.fromisoformat(markers["all_pods_ready_at_utc"]) - datetime.fromisoformat(requested_at)).total_seconds(), 3) if markers["all_pods_ready_at_utc"] and requested_at else None,
            "succeeded": succeeded,
            "markers": markers,
            "final_pods": final["pods"],
            "final_experiment_nodes": final["experiment_nodes"],
            "final_experiment_instances": final["experiment_instances"],
            "final_experiment_nodeclaims": final["experiment_nodeclaims"],
        })
    except Exception as exc:
        summary.update({"succeeded": False, "failed_at_utc": utc_now(), "failure_reason": f"{type(exc).__name__}: {exc}", "scale_requested_at_utc": requested_at, "markers": markers, "execution_started": execution_started})
    finally:
        if execution_started:
            try:
                cleanup(trial, args.cleanup_timeout)
                cleanup_ok = True
            except Exception as exc:
                summary["cleanup_failure"] = f"{type(exc).__name__}: {exc}"
        else:
            cleanup_ok = False
            summary["cleanup_failure"] = "execution did not start because pre-run validation failed; no cleanup mutations were issued"
        summary["execution_started"] = execution_started
        summary["cleanup_succeeded"] = cleanup_ok
        summary.setdefault("finished_at_utc", utc_now())
        summary.setdefault("elapsed_seconds", round(time.monotonic() - started_mono, 3))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"trial": output.name, "succeeded": summary.get("succeeded", False), "cleanup_succeeded": cleanup_ok, "failure_reason": summary.get("failure_reason"), "resource_metrics": summary.get("resource_metrics")}, indent=2, ensure_ascii=False))
    return 0 if summary.get("succeeded") and cleanup_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
