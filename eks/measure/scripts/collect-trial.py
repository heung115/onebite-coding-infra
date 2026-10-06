#!/usr/bin/env python3
"""Collect raw Kubernetes state and scale/readiness timestamps for one trial."""

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


KUBECONFIG = Path.home() / ".kube" / "onebite-eks.kubeconfig"
KUBE_CONTEXT = "onebite-eks-measure"
AWS_REGION = "ap-northeast-2"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Collector:
    def __init__(self, raw_path: Path) -> None:
        self.raw_path = raw_path
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, item: dict[str, Any]) -> None:
        with self.raw_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    def kubectl(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["KUBECONFIG"] = str(KUBECONFIG)
        argv = ["env", f"KUBECONFIG={KUBECONFIG}", "kubectl", "--context", KUBE_CONTEXT, *args]
        started = utc_now()
        result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
        self.record(
            {
                "kind": "command",
                "started_at_utc": started,
                "finished_at_utc": utc_now(),
                "argv": argv,
                "environment": {"KUBECONFIG": str(KUBECONFIG), "context": KUBE_CONTEXT},
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        return result

    def aws(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = ["aws", *args]
        started = utc_now()
        result = subprocess.run(argv, capture_output=True, text=True, check=False)
        self.record(
            {
                "kind": "command",
                "started_at_utc": started,
                "finished_at_utc": utc_now(),
                "argv": argv,
                "environment": {"region": AWS_REGION},
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        return result

    def get_json(self, *args: str) -> dict[str, Any]:
        result = self.kubectl(*args)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "kubectl failed")
        return json.loads(result.stdout)


def pod_summaries(payload: dict[str, Any], selector: str) -> list[dict[str, Any]]:
    result = []
    for pod in payload.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("app") != selector:
            continue
        statuses = pod.get("status", {}).get("containerStatuses", [])
        ready = bool(statuses) and all(item.get("ready", False) for item in statuses)
        conditions = pod.get("status", {}).get("conditions", [])
        ready_condition = next((item for item in conditions if item.get("type") == "Ready"), {})
        scheduled_condition = next((item for item in conditions if item.get("type") == "PodScheduled"), {})
        result.append(
            {
                "name": pod.get("metadata", {}).get("name"),
                "created_at_utc": pod.get("metadata", {}).get("creationTimestamp"),
                "phase": pod.get("status", {}).get("phase", "Unknown"),
                "ready": ready,
                "node": pod.get("spec", {}).get("nodeName"),
                "pod_ip": pod.get("status", {}).get("podIP"),
                "pod_ips": [item.get("ip") for item in pod.get("status", {}).get("podIPs", [])],
                "reason": pod.get("status", {}).get("reason"),
                "conditions": [
                    {
                        "type": item.get("type"),
                        "status": item.get("status"),
                        "reason": item.get("reason"),
                        "last_transition_at_utc": item.get("lastTransitionTime"),
                    }
                    for item in conditions
                ],
                "unschedulable_at_utc": (
                    scheduled_condition.get("lastTransitionTime")
                    if scheduled_condition.get("status") == "False"
                    and scheduled_condition.get("reason") == "Unschedulable"
                    else None
                ),
                "ready_at_utc": (
                    ready_condition.get("lastTransitionTime")
                    if ready_condition.get("status") == "True" and ready
                    else None
                ),
            }
        )
    return result


def node_summaries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for node in payload.get("items", []):
        metadata = node.get("metadata", {})
        labels = metadata.get("labels", {})
        if labels.get("measure-pool") != "experiment":
            continue
        ready = any(
            condition.get("type") == "Ready" and condition.get("status") == "True"
            for condition in node.get("status", {}).get("conditions", [])
        )
        result.append(
            {
                "name": metadata.get("name"),
                "created_at_utc": metadata.get("creationTimestamp"),
                "provider_id": node.get("spec", {}).get("providerID"),
                "ready": ready,
                "ready_at_utc": next(
                    (
                        condition.get("lastTransitionTime")
                        for condition in node.get("status", {}).get("conditions", [])
                        if condition.get("type") == "Ready" and condition.get("status") == "True"
                    ),
                    None,
                ),
                "zone": labels.get("topology.kubernetes.io/zone"),
                "instance_type": labels.get("node.kubernetes.io/instance-type"),
                "capacity_pods": node.get("status", {}).get("capacity", {}).get("pods"),
                "allocatable_pods": node.get("status", {}).get("allocatable", {}).get("pods"),
                "capacity_cpu": node.get("status", {}).get("capacity", {}).get("cpu"),
                "allocatable_cpu": node.get("status", {}).get("allocatable", {}).get("cpu"),
                "capacity_memory": node.get("status", {}).get("capacity", {}).get("memory"),
                "allocatable_memory": node.get("status", {}).get("allocatable", {}).get("memory"),
            }
        )
    return result


def experiment_instances(collector: Collector) -> list[dict[str, Any]]:
    result = collector.aws(
        "ec2",
        "describe-instances",
        "--region",
        AWS_REGION,
        "--filters",
        "Name=tag:Project,Values=onebite",
        "Name=tag:Temporary,Values=true",
        "Name=tag:measure-pool,Values=experiment",
        "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down,terminated",
        "--query",
        "Reservations[].Instances[].{instance_id:InstanceId,launch_time:LaunchTime,state:State.Name,instance_type:InstanceType,ami_id:ImageId,zone:Placement.AvailabilityZone,subnet_id:SubnetId}",
        "--output",
        "json",
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "could not read experiment EC2 instances")
    return json.loads(result.stdout or "[]")


def experiment_nodeclaims(collector: Collector) -> list[dict[str, Any]]:
    result = collector.kubectl(
        "get",
        "nodeclaims.karpenter.sh",
        "-l",
        "karpenter.sh/nodepool=experiment",
        "-o",
        "json",
    )
    if result.returncode != 0:
        missing_api = "the server doesn't have a resource type" in result.stderr.lower()
        missing_crd = "no matches for kind" in result.stderr.lower() or "notfound" in result.stderr.lower()
        if missing_api or missing_crd:
            return []
        raise RuntimeError(result.stderr.strip() or "could not read experiment NodeClaims")
    return [
        {"name": item.get("metadata", {}).get("name"), "phase": item.get("status", {}).get("phase")}
        for item in json.loads(result.stdout).get("items", [])
    ]


def controller_health(collector: Collector, condition: str) -> None:
    selected = (
        ("cluster-autoscaler", "kube-system")
        if condition == "ca"
        else ("karpenter", "karpenter")
    )
    other = ("karpenter", "karpenter") if condition == "ca" else ("cluster-autoscaler", "kube-system")
    other_result = collector.kubectl("get", "deployment", other[0], "-n", other[1], "-o", "json")
    if other_result.returncode == 0:
        raise RuntimeError(f"both autoscalers are present; unexpected deployment {other[1]}/{other[0]}")
    if "notfound" not in other_result.stderr.lower():
        raise RuntimeError(other_result.stderr.strip() or f"could not verify inactive controller {other[0]}")

    deployment = collector.get_json("get", "deployment", selected[0], "-n", selected[1], "-o", "json")
    metadata = deployment.get("metadata", {})
    status = deployment.get("status", {})
    desired = deployment.get("spec", {}).get("replicas", 1)
    ready = status.get("readyReplicas", 0)
    observed_generation = status.get("observedGeneration", 0)
    if ready < desired or observed_generation < metadata.get("generation", 0):
        raise RuntimeError(f"selected autoscaler {selected[1]}/{selected[0]} is not healthy")


def verify_aws_target(collector: Collector) -> None:
    configured_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not configured_region:
        result = subprocess.run(
            ["aws", "configure", "get", "region"], capture_output=True, text=True, check=False
        )
        configured_region = result.stdout.strip() if result.returncode == 0 else ""
    identity = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        capture_output=True,
        text=True,
        check=False,
    )
    account_suffix = identity.stdout.strip()[-4:] if identity.returncode == 0 else "unavailable"
    collector.record(
        {
            "kind": "target_check",
            "observed_at_utc": utc_now(),
            "account_suffix": account_suffix,
            "region": configured_region or "unset",
            "expected_account_suffix": "0497",
            "expected_region": AWS_REGION,
            "exit_code": identity.returncode,
            "error": identity.stderr.strip() if identity.returncode != 0 else None,
        }
    )
    if identity.returncode != 0 or account_suffix != "0497" or configured_region != AWS_REGION:
        raise RuntimeError("collector AWS target must be account ****0497 in ap-northeast-2")


def wait_for_state(
    collector: Collector,
    app: str,
    condition: str,
    target_replicas: int,
    target_workers: int,
    timeout: int,
    interval: float,
    on_sample: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    last_pods: list[dict[str, Any]] = []
    last_nodes: list[dict[str, Any]] = []
    last_instances: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        pods_payload = collector.get_json("get", "pods", "-n", "measure", "-l", f"app={app}", "-o", "json")
        nodes_payload = collector.get_json("get", "nodes", "-o", "json")
        last_pods = pod_summaries(pods_payload, app)
        last_nodes = node_summaries(nodes_payload)
        last_instances = experiment_instances(collector)
        nodeclaims = experiment_nodeclaims(collector)
        sample = {
            "kind": "snapshot",
            "observed_at_utc": utc_now(),
            "app": app,
            "pods": last_pods,
            "experiment_nodes": last_nodes,
            "experiment_instances": last_instances,
            "experiment_nodeclaims": nodeclaims,
        }
        collector.record(sample)
        if on_sample is not None:
            on_sample(sample)
        pods_empty = not last_pods
        pods_ready = len(last_pods) == target_replicas and all(pod["ready"] for pod in last_pods)
        nodes_ready = len(last_nodes) == target_workers and all(node["ready"] for node in last_nodes)
        instances_terminated = all(item.get("state") == "terminated" for item in last_instances)
        if target_replicas == 0 and pods_empty and nodes_ready:
            if target_workers > 0 or (not nodeclaims and instances_terminated):
                return last_pods, last_nodes, last_instances
        if target_replicas > 0 and pods_ready and nodes_ready:
            return last_pods, last_nodes, last_instances
        time.sleep(interval)
    return last_pods, last_nodes, last_instances


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", choices=["scale-probe", "bulk-probe"], required=True)
    parser.add_argument("--replicas", type=int, required=True)
    parser.add_argument("--expected-workers", type=int, required=True)
    parser.add_argument("--pretrial-workers", type=int)
    parser.add_argument("--condition", required=True)
    parser.add_argument("--trial", type=int, required=True)
    parser.add_argument("--pair", type=int)
    parser.add_argument("--pair-first", choices=["ca", "karpenter"])
    parser.add_argument("--output", type=Path, required=True, help="Trial JSON path; raw JSONL is written beside it.")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()

    raw_path = args.output.with_suffix(".jsonl")
    if raw_path.exists() or args.output.exists():
        print("Refusing to overwrite a previous trial result.", file=sys.stderr)
        return 2

    collector = Collector(raw_path)
    started_at = utc_now()
    started_mono = time.monotonic()
    deployment = f"deployment/{args.app}"
    pretrial_workers = args.expected_workers if args.pretrial_workers is None else args.pretrial_workers
    scale_requested_at: str | None = None

    try:
        verify_aws_target(collector)
        if not KUBECONFIG.is_file():
            raise RuntimeError(f"dedicated kubeconfig is missing: {KUBECONFIG}")
        if args.condition in {"ca", "karpenter"} and (
            args.app != "scale-probe"
            or args.replicas != 2
            or args.expected_workers != 2
            or pretrial_workers != 0
        ):
            raise ValueError("paired A trials require scale-probe 0→2 Pods and 0→2 experiment workers")
        current = collector.kubectl("scale", deployment, "-n", "measure", "--replicas=0")
        if current.returncode != 0:
            raise RuntimeError(current.stderr.strip() or "could not scale down before trial")
        empty_pods, drained_nodes, drained_instances = wait_for_state(
            collector, args.app, args.condition, 0, pretrial_workers, args.timeout, args.interval
        )
        if (
            empty_pods
            or len(drained_nodes) != pretrial_workers
            or any(item.get("state") != "terminated" for item in drained_instances)
            or experiment_nodeclaims(collector)
        ):
            raise TimeoutError("pre-trial Pods, Nodes, NodeClaims, or EC2 termination did not settle")
        if args.condition in {"ca", "karpenter"}:
            controller_health(collector, args.condition)

        before_payload = collector.get_json("get", "nodes", "-o", "json")
        before_nodes = {node["name"] for node in node_summaries(before_payload)}
        before_instances = {item["instance_id"] for item in experiment_instances(collector)}
        scale_requested_at = utc_now()
        scale_result = collector.kubectl(
            "scale", deployment, "-n", "measure", f"--replicas={args.replicas}"
        )
        if scale_result.returncode != 0:
            raise RuntimeError(scale_result.stderr.strip() or "could not scale workload")

        markers: dict[str, str | None] = {
            "first_pod_observed_at_utc": None,
            "first_pending_observed_at_utc": None,
            "first_new_worker_observed_at_utc": None,
            "first_new_worker_ready_at_utc": None,
            "first_ec2_launch_at_utc": None,
            "first_node_registered_at_utc": None,
            "first_unschedulable_at_utc": None,
            "node_timestamps": {},
            "pod_ready_timestamps": {},
            "all_pods_ready_at_utc": None,
        }

        def mark(sample: dict[str, Any]) -> None:
            observed = sample["observed_at_utc"]
            pods = sample["pods"]
            nodes = sample["experiment_nodes"]
            if pods and markers["first_pod_observed_at_utc"] is None:
                markers["first_pod_observed_at_utc"] = observed
            if any(pod["phase"] == "Pending" for pod in pods) and markers["first_pending_observed_at_utc"] is None:
                markers["first_pending_observed_at_utc"] = observed
            unschedulable = [pod["unschedulable_at_utc"] for pod in pods if pod["unschedulable_at_utc"]]
            if unschedulable and markers["first_unschedulable_at_utc"] is None:
                markers["first_unschedulable_at_utc"] = min(unschedulable)
            new_nodes = [node for node in nodes if node["name"] not in before_nodes]
            if new_nodes and markers["first_new_worker_observed_at_utc"] is None:
                markers["first_new_worker_observed_at_utc"] = observed
            for node in new_nodes:
                node_times = markers["node_timestamps"].setdefault(
                    node["name"],
                    {
                        "registered_at_utc": node["created_at_utc"],
                        "ready_at_utc": node["ready_at_utc"],
                        "provider_id": node["provider_id"],
                    },
                )
                node_times["ready_at_utc"] = node["ready_at_utc"] or node_times["ready_at_utc"]
                if node_times["registered_at_utc"] and (
                    markers["first_node_registered_at_utc"] is None
                    or node_times["registered_at_utc"] < markers["first_node_registered_at_utc"]
                ):
                    markers["first_node_registered_at_utc"] = node_times["registered_at_utc"]
            ready_nodes = [node["ready_at_utc"] for node in new_nodes if node["ready_at_utc"]]
            if ready_nodes and markers["first_new_worker_ready_at_utc"] is None:
                markers["first_new_worker_ready_at_utc"] = min(ready_nodes)
            new_instances = [
                item for item in sample["experiment_instances"]
                if item.get("instance_id") not in before_instances and item.get("launch_time")
            ]
            if new_instances:
                first_launch = min(item["launch_time"] for item in new_instances)
                if markers["first_ec2_launch_at_utc"] is None or first_launch < markers["first_ec2_launch_at_utc"]:
                    markers["first_ec2_launch_at_utc"] = first_launch
            for pod in pods:
                if pod["ready"] and pod["ready_at_utc"]:
                    markers["pod_ready_timestamps"][pod["name"]] = pod["ready_at_utc"]
            if len(pods) == args.replicas and pods and all(pod["ready"] for pod in pods):
                if markers["all_pods_ready_at_utc"] is None:
                    markers["all_pods_ready_at_utc"] = observed

        final_pods, final_nodes, final_instances = wait_for_state(
            collector,
            args.app,
            args.condition,
            args.replicas,
            args.expected_workers,
            args.timeout,
            args.interval,
            mark,
        )
        succeeded = (
            len(final_pods) == args.replicas
            and all(pod["ready"] for pod in final_pods)
            and len(final_nodes) == args.expected_workers
            and all(node["ready"] for node in final_nodes)
        )
        finished_at = utc_now()
        scale_to_ready_seconds = None
        if markers["all_pods_ready_at_utc"]:
            scale_to_ready_seconds = round(
                (
                    datetime.fromisoformat(markers["all_pods_ready_at_utc"])
                    - datetime.fromisoformat(scale_requested_at)
                ).total_seconds(),
                3,
            )
        summary = {
            "condition": args.condition,
            "trial": args.trial,
            "pair": args.pair,
            "pair_first": args.pair_first,
            "app": args.app,
            "replicas": args.replicas,
            "expected_experiment_workers": args.expected_workers,
            "pretrial_experiment_workers": pretrial_workers,
            "kubeconfig": str(KUBECONFIG),
            "started_at_utc": started_at,
            "scale_requested_at_utc": scale_requested_at,
            "finished_at_utc": finished_at,
            "elapsed_seconds": round(time.monotonic() - started_mono, 3),
            "scale_request_to_all_pods_ready_seconds": scale_to_ready_seconds,
            "succeeded": succeeded,
            "markers": markers,
            "final_pods": final_pods,
            "final_experiment_nodes": final_nodes,
            "final_experiment_instances": final_instances,
            "limitations": [] if succeeded else ["desired Pods and Nodes did not all become Ready before timeout"],
            "raw_file": str(raw_path),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if succeeded else 1
    except Exception as exc:  # Preserve the failure with all raw samples collected so far.
        failure = {
            "condition": args.condition,
            "trial": args.trial,
            "pair": args.pair,
            "pair_first": args.pair_first,
            "app": args.app,
            "replicas": args.replicas,
            "expected_experiment_workers": args.expected_workers,
            "started_at_utc": started_at,
            "failed_at_utc": utc_now(),
            "scale_requested_at_utc": scale_requested_at,
            "error": f"{type(exc).__name__}: {exc}",
            "raw_file": str(raw_path),
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
