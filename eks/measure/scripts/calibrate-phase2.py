#!/usr/bin/env python3
"""Calibrate Phase 2 workload requests from real Node allocatable and DaemonSet requests."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SESSION_DEFAULT = ROOT / "measure/results/20260930-phase2-instance-selection"
REGION = "ap-northeast-2"
CONTEXT = "onebite-eks-measure"
KUBECONFIG = Path.home() / ".kube" / "onebite-eks.kubeconfig"
INVENTORY_DEFAULT = SESSION_DEFAULT / "candidate-inventory-and-pricing.json"
FLEXIBLE_FAMILIES = {"c7i", "c8i", "m7i", "m8i", "r7i", "r8i"}
FLEXIBLE_SIZES = {"large", "xlarge"}
FIXED_TYPE = "m7i-flex.large"
FIXED_REFERENCE_DEFAULT = ROOT / "measure/results/20260930-phase2-instance-selection-paid-retry/calibration.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_a_collector() -> Any:
    path = ROOT / "measure/scripts/collect-trial.py"
    spec = importlib.util.spec_from_file_location("onebite_collect_trial", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load existing collector helpers at {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Recorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, item: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + "\n")


def kubectl(recorder: Recorder, *args: str, stdin: str | None = None, save_output: bool = True) -> subprocess.CompletedProcess[str]:
    argv = ["env", f"KUBECONFIG={KUBECONFIG}", "kubectl", "--context", CONTEXT, *args]
    start = utc_now()
    result = subprocess.run(argv, input=stdin, capture_output=True, text=True, check=False)
    entry: dict[str, Any] = {
        "kind": "command",
        "started_at_utc": start,
        "finished_at_utc": utc_now(),
        "argv": argv,
        "environment": {"KUBECONFIG": str(KUBECONFIG), "context": CONTEXT},
        "exit_code": result.returncode,
        "stderr": result.stderr,
    }
    if save_output:
        entry["stdout"] = result.stdout
    else:
        entry["stdout_omitted"] = True
        entry["stdout_bytes"] = len(result.stdout.encode("utf-8"))
    recorder.write(entry)
    return result


def aws(recorder: Recorder, *args: str) -> subprocess.CompletedProcess[str]:
    argv = ["aws", *args]
    start = utc_now()
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    recorder.write({
        "kind": "command",
        "started_at_utc": start,
        "finished_at_utc": utc_now(),
        "argv": argv,
        "environment": {"region": REGION},
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    })
    return result


def json_kubectl(recorder: Recorder, *args: str, redact: bool = False) -> dict[str, Any]:
    result = kubectl(recorder, *args, save_output=not redact)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "kubectl failed")
    return json.loads(result.stdout)


def parse_cpu(value: str | int | float | None) -> int:
    if value is None:
        return 0
    raw = str(value)
    if raw.endswith("n"):
        amount = Decimal(raw[:-1]) / Decimal(1_000_000)
    elif raw.endswith("u"):
        amount = Decimal(raw[:-1]) / Decimal(1_000)
    elif raw.endswith("m"):
        amount = Decimal(raw[:-1])
    else:
        amount = Decimal(raw) * Decimal(1000)
    return int(amount.to_integral_value(rounding="ROUND_CEILING"))


def parse_memory(value: str | int | float | None) -> int:
    if value is None:
        return 0
    raw = str(value)
    binary = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "Pi": 2**50, "Ei": 2**60}
    decimal_units = {"k": 10**3, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12, "P": 10**15, "E": 10**18}
    for suffix, multiplier in binary.items():
        if raw.endswith(suffix):
            return int((Decimal(raw[: -len(suffix)]) * multiplier).to_integral_value(rounding="ROUND_CEILING"))
    for suffix, multiplier in decimal_units.items():
        if raw.endswith(suffix):
            return int((Decimal(raw[:-len(suffix)]) * multiplier).to_integral_value(rounding="ROUND_CEILING"))
    return int(Decimal(raw).to_integral_value(rounding="ROUND_CEILING"))


def resource_pair(container: dict[str, Any]) -> tuple[int, int]:
    request = container.get("resources", {}).get("requests", {})
    return parse_cpu(request.get("cpu")), parse_memory(request.get("memory"))


def effective_pod_requests(pod: dict[str, Any]) -> tuple[int, int]:
    def sum_containers(items: list[dict[str, Any]]) -> tuple[int, int]:
        return (
            sum(resource_pair(item)[0] for item in items),
            sum(resource_pair(item)[1] for item in items),
        )

    app = sum_containers(pod.get("spec", {}).get("containers", []))
    sidecars_cpu = sidecars_mem = peak_init_cpu = peak_init_mem = 0
    for item in pod.get("spec", {}).get("initContainers", []):
        cpu, memory = resource_pair(item)
        if item.get("restartPolicy") == "Always":
            sidecars_cpu += cpu
            sidecars_mem += memory
            peak_init_cpu = max(peak_init_cpu, sidecars_cpu)
            peak_init_mem = max(peak_init_mem, sidecars_mem)
        else:
            peak_init_cpu = max(peak_init_cpu, sidecars_cpu + cpu)
            peak_init_mem = max(peak_init_mem, sidecars_mem + memory)
    cpu = max(app[0] + sidecars_cpu, peak_init_cpu)
    memory = max(app[1] + sidecars_mem, peak_init_mem)
    overhead = pod.get("spec", {}).get("overhead", {})
    return cpu + parse_cpu(overhead.get("cpu")), memory + parse_memory(overhead.get("memory"))


def sum_requests(pods: list[dict[str, Any]]) -> tuple[int, int]:
    totals = [effective_pod_requests(pod) for pod in pods]
    return sum(item[0] for item in totals), sum(item[1] for item in totals)


def fmt_memory(mib: int) -> str:
    return f"{mib}Mi"


def ceil_to(value: Decimal, quantum: int) -> int:
    return int((value / quantum).to_integral_value(rounding="ROUND_CEILING")) * quantum


def inventory_candidate_specs(inventory: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    entries = inventory.get("inventory", [])
    rates = inventory.get("on_demand_linux_shared_hourly_prices", {})
    specs: list[dict[str, Any]] = []
    fixed: dict[str, Any] | None = None
    for item in entries:
        instance_type = item.get("instance_type", "")
        family, dot, size = instance_type.rpartition(".")
        if (
            not dot or item.get("offered_in_zone") is not True
            or "x86_64" not in item.get("architectures", [])
        ):
            continue
        price_rows = rates.get(instance_type, [])
        if not price_rows:
            raise RuntimeError(f"candidate inventory has no On-Demand price for {instance_type}")
        spec = {
            "instance_type": instance_type,
            "family": family,
            "size": size,
            "vcpu": int(item["vcpu"]),
            "memory_mib": int(item["memory_mib"]),
            "architectures": item.get("architectures", []),
            "offered_in_zone": True,
            "usd_per_hour": str(price_rows[0]["usd_per_hour"]),
        }
        if instance_type == FIXED_TYPE:
            fixed = spec
        elif family in FLEXIBLE_FAMILIES and size in FLEXIBLE_SIZES:
            specs.append(spec)
    if fixed is None:
        raise RuntimeError(f"candidate inventory does not confirm {FIXED_TYPE} in the target zone")
    if len(specs) < 4:
        raise RuntimeError(f"candidate inventory has too few offered Flexible types: {[item['instance_type'] for item in specs]}")
    return fixed, sorted(specs, key=lambda item: item["instance_type"])


def calibration_targets(inventory: dict[str, Any]) -> list[dict[str, Any]]:
    fixed, flex = inventory_candidate_specs(inventory)
    cpu_edge = [item for item in flex if item["vcpu"] > fixed["vcpu"]]
    memory_edge = [
        item for item in flex
        if item["memory_mib"] > fixed["memory_mib"] and item["vcpu"] > fixed["vcpu"]
    ]
    if not cpu_edge or not memory_edge:
        raise RuntimeError("candidate inventory does not contain CPU and Memory boundary candidates above Fixed")

    cpu_rep = min(cpu_edge, key=lambda item: (item["vcpu"], item["memory_mib"], Decimal(item["usd_per_hour"]), item["instance_type"]))
    memory_rep = min(memory_edge, key=lambda item: (item["memory_mib"], item["vcpu"], Decimal(item["usd_per_hour"]), item["instance_type"]))
    upper_memory = max(item["memory_mib"] for item in memory_edge)
    upper_memory_rep = min(
        (item for item in memory_edge if item["memory_mib"] == upper_memory),
        key=lambda item: (Decimal(item["usd_per_hour"]), item["instance_type"]),
    )
    representatives: dict[str, tuple[dict[str, Any], str]] = {}
    for item, reason in (
        (cpu_rep, "lowest hardware CPU boundary above Fixed"),
        (memory_rep, "lowest hardware Memory boundary above Fixed with higher CPU tier"),
        (upper_memory_rep, "highest hardware Memory boundary above Fixed with higher CPU tier"),
    ):
        representatives.setdefault(item["instance_type"], (item, reason))

    if len(representatives) < 2 or len(representatives) > 3:
        raise RuntimeError("boundary selection did not produce two or three distinct Flexible representatives")
    fixed_target = {**fixed, "condition": "fixed", "calibration_role": "Fixed baseline"}
    flex_targets = [
        {**item, "condition": "flexible", "calibration_role": reason}
        for item, reason in representatives.values()
    ]
    return [*flex_targets, fixed_target]


def phase2_nodeclaims(recorder: Recorder) -> list[dict[str, Any]]:
    payload = json_kubectl(recorder, "get", "nodeclaims.karpenter.sh", "-o", "json")
    wanted = {"phase2-fixed", "phase2-flexible", "experiment"}
    return [
        {"name": item.get("metadata", {}).get("name"), "pool": item.get("metadata", {}).get("labels", {}).get("karpenter.sh/nodepool"), "phase": item.get("status", {}).get("phase")}
        for item in payload.get("items", [])
        if item.get("metadata", {}).get("labels", {}).get("karpenter.sh/nodepool") in wanted
    ]


def active_pods(recorder: Recorder) -> list[dict[str, Any]]:
    payload = json_kubectl(recorder, "get", "pods", "-A", "-o", "json", redact=True)
    wanted = {"scale-probe", "bulk-probe", "phase2-probe", "phase2-calibration"}
    result = []
    for pod in payload.get("items", []):
        labels = pod.get("metadata", {}).get("labels", {})
        if labels.get("app") not in wanted:
            continue
        result.append({"namespace": pod.get("metadata", {}).get("namespace"), "name": pod.get("metadata", {}).get("name"), "phase": pod.get("status", {}).get("phase")})
    return result


def check_zero(recorder: Recorder, collector_module: Any, collector: Any) -> None:
    pods = active_pods(recorder)
    nodes = collector_module.node_summaries(collector.get_json("get", "nodes", "-o", "json"))
    claims = phase2_nodeclaims(recorder)
    instances = collector_module.experiment_instances(collector)
    active_instances = [item for item in instances if item.get("state") != "terminated"]
    state = {"pods": pods, "experiment_nodes": nodes, "nodeclaims": claims, "active_instances": active_instances}
    recorder.write({"kind": "zero_precondition", "observed_at_utc": utc_now(), **state})
    if pods or nodes or claims or active_instances:
        raise RuntimeError("zero precondition failed: benchmark Pods, worker Nodes, NodeClaims, and prior EC2 termination must all be clear")


def render_pod(condition: str, instance_type: str | None) -> str:
    node_selector = {"measure-pool": "experiment", "measure-condition": condition}
    if instance_type:
        node_selector["node.kubernetes.io/instance-type"] = instance_type
    payload = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "phase2-calibration", "namespace": "measure", "labels": {"app": "phase2-calibration"}},
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": node_selector,
            "tolerations": [{"key": "measure/only", "operator": "Equal", "value": "experiment", "effect": "NoSchedule"}],
            "containers": [{"name": "pause", "image": "registry.k8s.io/pause:3.10", "resources": {"requests": {"cpu": "5m", "memory": "16Mi"}}}],
        },
    }
    return json.dumps(payload, indent=2) + "\n"


def read_target(
    recorder: Recorder,
    collector_module: Any,
    collector: Any,
    target: dict[str, str],
    timeout: int,
) -> dict[str, Any]:
    condition = target["condition"]
    instance_type = target["instance_type"]
    manifest = render_pod(condition, None if condition == "fixed" else instance_type)
    recorder.write({"kind": "calibration_start", "observed_at_utc": utc_now(), "instance_type_requested": instance_type, "condition": condition})
    applied = kubectl(recorder, "apply", "-f", "-", stdin=manifest)
    if applied.returncode:
        raise RuntimeError(applied.stderr.strip() or f"failed to create calibration Pod for {instance_type}")

    deadline = time.monotonic() + timeout
    found: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        pod = json_kubectl(recorder, "get", "pod", "phase2-calibration", "-n", "measure", "-o", "json")
        pod_status = pod.get("status", {})
        node_name = pod.get("spec", {}).get("nodeName")
        ready = any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod_status.get("conditions", []))
        if node_name and ready:
            node = json_kubectl(recorder, "get", "node", node_name, "-o", "json")
            nodes = collector_module.node_summaries({"items": [node]})
            actual_type = nodes[0]["instance_type"] if nodes else None
            if actual_type != instance_type:
                raise RuntimeError(f"calibration requested {instance_type}, received {actual_type}")
            all_pods = json_kubectl(recorder, "get", "pods", "-A", "-o", "json", redact=True).get("items", [])
            daemon_pods = [
                item for item in all_pods
                if item.get("spec", {}).get("nodeName") == node_name
                and item.get("status", {}).get("phase") not in {"Succeeded", "Failed"}
                and any(owner.get("kind") == "DaemonSet" for owner in item.get("metadata", {}).get("ownerReferences", []))
            ]
            ds_cpu, ds_memory = sum_requests(daemon_pods)
            allocatable = node.get("status", {}).get("allocatable", {})
            found = {
                "instance_type": actual_type,
                "condition": condition,
                "node_name": node_name,
                "provider_id": node.get("spec", {}).get("providerID"),
                "capacity": node.get("status", {}).get("capacity", {}),
                "allocatable": allocatable,
                "allocatable_cpu_millicores": parse_cpu(allocatable.get("cpu")),
                "allocatable_memory_bytes": parse_memory(allocatable.get("memory")),
                "daemonset_pod_count": len(daemon_pods),
                "daemonset_requests_cpu_millicores": ds_cpu,
                "daemonset_requests_memory_bytes": ds_memory,
                "usable_cpu_millicores": parse_cpu(allocatable.get("cpu")) - ds_cpu,
                "usable_memory_bytes": parse_memory(allocatable.get("memory")) - ds_memory,
                "daemonset_pods": [
                    {
                        "namespace": item.get("metadata", {}).get("namespace"),
                        "name": item.get("metadata", {}).get("name"),
                        "requests": [
                            {
                                "container": container.get("name"),
                                "resources": container.get("resources", {}).get("requests", {}),
                            }
                            for container in item.get("spec", {}).get("containers", [])
                        ],
                    }
                    for item in daemon_pods
                ],
                "captured_at_utc": utc_now(),
            }
            recorder.write({"kind": "calibration_observation", **found})
            break
        time.sleep(2)
    if found is None:
        raise TimeoutError(f"calibration Pod did not become Ready on {instance_type} within {timeout}s")
    return found


def daemonset_request_signature(observation: dict[str, Any]) -> list[dict[str, Any]]:
    signature = []
    for pod in observation.get("daemonset_pods", []):
        pod_name = pod.get("name", "")
        workload = pod_name.rsplit("-", 1)[0] if "-" in pod_name else pod_name
        containers = [
            {
                "name": container.get("container"),
                "requests": container.get("resources", {}),
            }
            for container in pod.get("requests", [])
        ]
        signature.append({
            "namespace": pod.get("namespace"),
            "workload": workload,
            "containers": sorted(containers, key=lambda item: item["name"] or ""),
        })
    return sorted(signature, key=lambda item: (item["namespace"] or "", item["workload"]))


def compare_fixed_reference(
    observed: dict[str, Any],
    reference: dict[str, Any],
) -> dict[str, Any]:
    fields = (
        "instance_type",
        "allocatable_cpu_millicores",
        "allocatable_memory_bytes",
        "daemonset_pod_count",
        "daemonset_requests_cpu_millicores",
        "daemonset_requests_memory_bytes",
        "usable_cpu_millicores",
        "usable_memory_bytes",
    )
    differences = {
        field: {"reference": reference.get(field), "observed": observed.get(field)}
        for field in fields
        if reference.get(field) != observed.get(field)
    }
    reference_daemonsets = daemonset_request_signature(reference)
    observed_daemonsets = daemonset_request_signature(observed)
    if reference_daemonsets != observed_daemonsets:
        differences["daemonset_request_signature"] = {
            "reference": reference_daemonsets,
            "observed": observed_daemonsets,
        }
    return {
        "matches": not differences,
        "comparison": "exact numeric equality and normalized DaemonSet workload/container/request equality",
        "differences": differences,
    }


def cleanup(recorder: Recorder, timeout: int) -> None:
    kubectl(recorder, "delete", "pod", "phase2-calibration", "-n", "measure", "--ignore-not-found", "--wait=true")
    for item in phase2_nodeclaims(recorder):
        kubectl(recorder, "delete", "nodeclaim", item["name"], "--ignore-not-found", "--wait=false")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        nodes = json_kubectl(recorder, "get", "nodes", "-l", "measure-pool=experiment", "-o", "json").get("items", [])
        claims = phase2_nodeclaims(recorder)
        argv = [
            "aws", "ec2", "describe-instances", "--region", REGION,
            "--filters", "Name=tag:Project,Values=onebite", "Name=tag:Temporary,Values=true",
            "Name=tag:measure-pool,Values=experiment",
            "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down",
            "--query", "Reservations[].Instances[].{state:State.Name,id:InstanceId}", "--output", "json",
        ]
        ec2 = subprocess.run(argv, capture_output=True, text=True, check=False)
        recorder.write({"kind": "command", "started_at_utc": utc_now(), "finished_at_utc": utc_now(), "argv": argv, "environment": {"region": REGION}, "exit_code": ec2.returncode, "stdout": ec2.stdout, "stderr": ec2.stderr})
        if ec2.returncode:
            raise RuntimeError(ec2.stderr.strip() or "could not check calibration EC2 termination")
        active = json.loads(ec2.stdout or "[]")
        if not nodes and not claims and not active:
            return
        time.sleep(2)
    raise TimeoutError("calibration worker Nodes, NodeClaims, or EC2 instances did not terminate")


def derive_dimension_request(
    fixed_capacity: int,
    candidates: list[dict[str, Any]],
    quantum: int,
    secondary_key: str,
    secondary_demand: int,
) -> tuple[int, dict[str, Any]]:
    eligible = [
        item for item in candidates
        if item.get("usable_capacity", 0) > fixed_capacity
        and item.get(secondary_key, 0) >= secondary_demand
    ]
    if len({item["instance_type"] for item in eligible}) < 2:
        raise RuntimeError("fewer than two Flexible candidate types exceed the Fixed usable capacity")
    if len({item["family"] for item in eligible}) < 2:
        raise RuntimeError("fewer than two Flexible instance families exceed the Fixed usable capacity")

    median_capacity = statistics.median(item["usable_capacity"] for item in eligible)
    midpoint = (Decimal(fixed_capacity) + Decimal(str(median_capacity))) / Decimal(2)
    per_pod = ceil_to(midpoint / Decimal(2), quantum)
    while per_pod * 2 > fixed_capacity:
        fitting = [
            item for item in eligible
            if item["usable_capacity"] >= 2 * per_pod
            and item.get(secondary_key, 0) >= secondary_demand
        ]
        fitting_types = sorted({item["instance_type"] for item in fitting})
        fitting_families = sorted({item["family"] for item in fitting})
        if len(fitting_types) >= 2 and len(fitting_families) >= 2:
            return per_pod, {
                "fixed_usable_capacity": fixed_capacity,
                "flexible_candidate_count": len(candidates),
                "flexible_candidates_above_fixed": len(eligible),
                "eligible_capacity_median": median_capacity,
                "target_two_pod_capacity": 2 * per_pod,
                "candidate_fit_types": fitting_types,
                "candidate_fit_families": fitting_families,
            }
        per_pod -= quantum
    raise RuntimeError("could not derive a per-Pod request above Fixed one-node capacity")


def candidate_capacity_model(
    fixed_spec: dict[str, Any],
    flex_specs: list[dict[str, Any]],
    observations: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    specs = [fixed_spec, *flex_specs]
    specs_by_type = {item["instance_type"]: item for item in specs}
    references = []
    for instance_type, observation in observations.items():
        spec = specs_by_type[instance_type]
        references.append({
            "instance_type": instance_type,
            "vcpu": spec["vcpu"],
            "memory_mib": spec["memory_mib"],
            "cpu_overhead_millicores": spec["vcpu"] * 1000 - observation["usable_cpu_millicores"],
            "memory_overhead_bytes": spec["memory_mib"] * 2**20 - observation["usable_memory_bytes"],
        })

    def overhead_for(spec: dict[str, Any], dimension: str) -> tuple[int, list[str]]:
        spec_key = "vcpu" if dimension == "cpu" else "memory_mib"
        overhead_key = "cpu_overhead_millicores" if dimension == "cpu" else "memory_overhead_bytes"
        distance = min(abs(item[spec_key] - spec[spec_key]) for item in references)
        closest = [item for item in references if abs(item[spec_key] - spec[spec_key]) == distance]
        return int(statistics.median(item[overhead_key] for item in closest)), [item["instance_type"] for item in closest]

    result = []
    for spec in specs:
        observation = observations.get(spec["instance_type"])
        if observation:
            result.append({
                **spec,
                "usable_cpu_millicores": observation["usable_cpu_millicores"],
                "usable_memory_bytes": observation["usable_memory_bytes"],
                "capacity_basis": "measured Node allocatable minus measured DaemonSet Pod requests",
                "measurement": observation,
            })
            continue
        cpu_overhead, cpu_refs = overhead_for(spec, "cpu")
        memory_overhead, memory_refs = overhead_for(spec, "memory")
        result.append({
            **spec,
            "usable_cpu_millicores": max(0, spec["vcpu"] * 1000 - cpu_overhead),
            "usable_memory_bytes": max(0, spec["memory_mib"] * 2**20 - memory_overhead),
            "capacity_basis": "inventory hardware capacity minus nearest measured CPU-tier and memory-tier overhead",
            "cpu_overhead_reference_types": cpu_refs,
            "memory_overhead_reference_types": memory_refs,
        })
    return result


def verified_profile_demand(
    initial_per_pod: int,
    fixed_capacity: int,
    quantum: int,
    candidates: list[dict[str, Any]],
    measured_flex: list[dict[str, Any]],
    focus_dimension: str,
    secondary_per_pod: int,
) -> tuple[int, dict[str, Any]]:
    per_pod = initial_per_pod
    while 2 * per_pod > fixed_capacity:
        if focus_dimension == "cpu":
            total_cpu = 2 * per_pod
            total_memory = 2 * secondary_per_pod
        elif focus_dimension == "memory":
            total_cpu = 2 * secondary_per_pod
            total_memory = 2 * per_pod
        else:
            raise ValueError(f"unsupported focus dimension: {focus_dimension}")
        estimated_fit = [
            item for item in candidates
            if item["usable_cpu_millicores"] >= total_cpu
            and item["usable_memory_bytes"] >= total_memory
        ]
        actual_fit = [
            item for item in measured_flex
            if item["usable_cpu_millicores"] >= total_cpu
            and item["usable_memory_bytes"] >= total_memory
        ]
        estimated_types = sorted({item["instance_type"] for item in estimated_fit})
        estimated_families = sorted({item["family"] for item in estimated_fit})
        actual_types = sorted({item["instance_type"] for item in actual_fit})
        actual_families = sorted({item["family"] for item in actual_fit})
        if (
            len(estimated_types) >= 2 and len(estimated_families) >= 2
            and len(actual_types) >= 2 and len(actual_families) >= 2
        ):
            return per_pod, {
                "target_two_pod_cpu_millicores": total_cpu,
                "target_two_pod_memory_bytes": total_memory,
                "inventory_estimated_fit_types": estimated_types,
                "inventory_estimated_fit_families": estimated_families,
                "measured_representative_fit_types": actual_types,
                "measured_representative_fit_families": actual_families,
            }
        per_pod -= quantum
    raise RuntimeError("derived request failed Fixed exclusion or multi-family Flexible representative fit")


def derive_profiles(
    observations: dict[str, dict[str, Any]],
    fixed_spec: dict[str, Any],
    flex_specs: list[dict[str, Any]],
) -> dict[str, Any]:
    fixed = observations["m7i-flex.large"]
    fixed_cpu = fixed["usable_cpu_millicores"]
    fixed_memory = fixed["usable_memory_bytes"]
    capacity_model = candidate_capacity_model(fixed_spec, flex_specs, observations)
    candidates = [item for item in capacity_model if item["instance_type"] != FIXED_TYPE]
    cpu_candidates = [
        {
            "instance_type": item["instance_type"], "family": item["family"],
            "usable_capacity": item["usable_cpu_millicores"],
            "usable_memory_bytes": item["usable_memory_bytes"],
        }
        for item in candidates
    ]
    memory_candidates = [
        {
            "instance_type": item["instance_type"], "family": item["family"],
            "usable_capacity": item["usable_memory_bytes"],
            "usable_cpu_millicores": item["usable_cpu_millicores"],
        }
        for item in candidates
    ]
    cpu_per_pod, cpu_basis = derive_dimension_request(
        fixed_cpu, cpu_candidates, 50, "usable_memory_bytes", parse_memory("512Mi")
    )
    memory_bytes_per_pod, memory_basis = derive_dimension_request(
        fixed_memory, memory_candidates, 64 * 2**20, "usable_cpu_millicores", parse_cpu("2000m")
    )
    measured_flex = [
        item for item in capacity_model
        if item["instance_type"] in observations and item["instance_type"] != FIXED_TYPE
    ]
    cpu_per_pod, cpu_verification = verified_profile_demand(
        cpu_per_pod, fixed_cpu, 50, candidates, measured_flex,
        focus_dimension="cpu", secondary_per_pod=parse_memory("256Mi"),
    )
    memory_bytes_per_pod, memory_verification = verified_profile_demand(
        memory_bytes_per_pod, fixed_memory, 64 * 2**20, candidates, measured_flex,
        focus_dimension="memory", secondary_per_pod=parse_cpu("1000m"),
    )
    cpu_basis.update(cpu_verification)
    cpu_basis["candidate_fit_types"] = cpu_verification["inventory_estimated_fit_types"]
    cpu_basis["candidate_fit_families"] = cpu_verification["inventory_estimated_fit_families"]
    memory_basis.update(memory_verification)
    memory_basis["candidate_fit_types"] = memory_verification["inventory_estimated_fit_types"]
    memory_basis["candidate_fit_families"] = memory_verification["inventory_estimated_fit_families"]
    memory_mib_per_pod = math.ceil(memory_bytes_per_pod / 2**20)
    profiles = {
        "general": {
            "requests_per_pod": {"cpu": "1000m", "memory": "256Mi"},
            "derived_from": "Existing A scale-probe requests, unchanged.",
        },
        "cpu-heavy": {
            "requests_per_pod": {"cpu": f"{cpu_per_pod}m", "memory": "256Mi"},
            "derived_from": "Use metadata CPU/memory specs and price inventory for all offered candidates; subtract measured overhead from nearest calibrated CPU/memory tiers; target the midpoint of median eligible inventory CPU and Fixed; round up to 50m; verify fit against actual representatives.",
            "capacity_basis": cpu_basis,
        },
        "memory-heavy": {
            "requests_per_pod": {"cpu": "1000m", "memory": fmt_memory(memory_mib_per_pod)},
            "derived_from": "Use metadata CPU/memory specs and price inventory for all offered candidates; subtract measured overhead from nearest calibrated CPU/memory tiers; target the midpoint of median eligible inventory Memory and Fixed; round up to 64Mi; verify fit against actual representatives.",
            "capacity_basis": memory_basis,
        },
    }
    cpu_profile = profiles["cpu-heavy"]["requests_per_pod"]["cpu"]
    cpu_value = parse_cpu(cpu_profile)
    mem_profile = profiles["memory-heavy"]["requests_per_pod"]["memory"]
    mem_value = parse_memory(mem_profile)
    checks = {
        "general_one_pod_fits_fixed": parse_cpu("1000m") <= fixed_cpu and parse_memory("256Mi") <= fixed_memory,
        "general_two_cpu_requests_exceed_fixed_one_node": 2 * parse_cpu("1000m") > fixed_cpu,
        "cpu_heavy_one_pod_fits_fixed": cpu_value <= fixed_cpu and parse_memory("256Mi") <= fixed_memory,
        "cpu_heavy_two_pods_exceed_fixed_one_node": 2 * cpu_value > fixed_cpu,
        "cpu_heavy_two_pods_fit_multiple_flexible_types": len(cpu_basis["candidate_fit_types"]) >= 2,
        "cpu_heavy_two_pods_fit_multiple_flexible_families": len(cpu_basis["candidate_fit_families"]) >= 2,
        "cpu_heavy_two_pods_fit_multiple_measured_families": len(cpu_basis["measured_representative_fit_families"]) >= 2,
        "memory_heavy_one_pod_fits_fixed": parse_cpu("1000m") <= fixed_cpu and mem_value <= fixed_memory,
        "memory_heavy_two_pods_exceed_fixed_one_node": 2 * mem_value > fixed_memory,
        "memory_heavy_two_pods_fit_multiple_flexible_types": len(memory_basis["candidate_fit_types"]) >= 2,
        "memory_heavy_two_pods_fit_multiple_flexible_families": len(memory_basis["candidate_fit_families"]) >= 2,
        "memory_heavy_two_pods_fit_multiple_measured_families": len(memory_basis["measured_representative_fit_families"]) >= 2,
    }
    if not all(checks.values()):
        raise RuntimeError(f"calibration did not support the preregistered request design: {checks}")
    return {"replicas": 2, "profiles": profiles, "fit_checks": checks, "candidate_capacity_model": capacity_model}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-dir", type=Path, default=SESSION_DEFAULT)
    parser.add_argument("--inventory", type=Path, default=INVENTORY_DEFAULT)
    parser.add_argument("--fixed-only", action="store_true", help="Calibrate exactly one Fixed worker and compare it to the paid calibration reference; do not launch Flexible representatives.")
    parser.add_argument("--fixed-reference", type=Path, default=FIXED_REFERENCE_DEFAULT)
    parser.add_argument("--timeout", type=int, default=240)
    parser.add_argument("--execute", action="store_true", help="Required acknowledgement to launch calibration workers.")
    args = parser.parse_args()
    if not args.execute:
        parser.error("calibration launches EC2 workers; pass --execute only after the separate approval")
    if os.environ.get("ALLOW_PHASE2_EXECUTION") != "1":
        parser.error("set ALLOW_PHASE2_EXECUTION=1 only after the separate Phase 2 approval")
    if args.fixed_only and not args.fixed_reference.is_file():
        parser.error(f"Fixed-only mode requires an existing reference calibration: {args.fixed_reference}")

    session = args.session_dir.resolve()
    session.mkdir(parents=True, exist_ok=True)
    output_path = session / "calibration.json"
    raw_path = session / "calibration.jsonl"
    profile_path = session / "profile-requests.json"
    if output_path.exists() or raw_path.exists() or profile_path.exists():
        parser.error("refusing to overwrite existing Phase 2 calibration artifacts")
    if not KUBECONFIG.is_file():
        parser.error(f"dedicated kubeconfig is missing: {KUBECONFIG}")
    if not args.inventory.is_file():
        parser.error(f"candidate inventory is missing: {args.inventory}")

    recorder = Recorder(raw_path)
    module = load_a_collector()
    collector = module.Collector(raw_path)
    inventory = json.loads(args.inventory.read_text(encoding="utf-8"))
    fixed_spec, flex_specs = inventory_candidate_specs(inventory)
    fixed_target = {**fixed_spec, "condition": "fixed", "calibration_role": "single Fixed reuse verification"}
    targets = [fixed_target] if args.fixed_only else calibration_targets(inventory)
    summary: dict[str, Any] = {
        "started_at_utc": utc_now(), "region": REGION, "account_suffix": "0497",
        "inventory_file": str(args.inventory.resolve()),
        "candidate_inventory_specs": flex_specs,
        "targets": targets,
        "calibration_scope": (
            "exactly one Fixed m7i-flex.large sample compared with the paid calibration reference; Flexible calibration is skipped."
            if args.fixed_only
            else "one Fixed baseline plus three metadata/price-selected Flexible boundary representatives; all 12 offered Flexible candidates receive inventory-based capacity estimates using measured tier overhead; benchmark Pods are not pinned to calibration types."
        ),
        "observations": {}, "calibration_failures": [],
    }
    try:
        module.verify_aws_target(collector)
        observations: dict[str, dict[str, Any]] = {}
        for target in targets:
            instance_type = target["instance_type"]
            check_zero(recorder, module, collector)
            module.controller_health(collector, "karpenter")
            failure: str | None = None
            try:
                observations[instance_type] = read_target(recorder, module, collector, target, args.timeout)
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                summary["calibration_failures"].append({"instance_type": instance_type, "condition": target["condition"], "reason": failure})
                recorder.write({"kind": "calibration_failure", "observed_at_utc": utc_now(), "instance_type": instance_type, "condition": target["condition"], "reason": failure})
            cleanup(recorder, args.timeout)
            check_zero(recorder, module, collector)
            if failure and target["condition"] == "fixed":
                raise RuntimeError(f"Fixed baseline calibration failed: {failure}")
            if failure:
                continue
        if args.fixed_only:
            reference_data = json.loads(args.fixed_reference.read_text(encoding="utf-8"))
            reference = reference_data.get("observations", {}).get(FIXED_TYPE)
            if reference is None:
                raise RuntimeError(f"reference calibration has no Fixed observation for {FIXED_TYPE}")
            comparison = compare_fixed_reference(observations[FIXED_TYPE], reference)
            summary.update({
                "succeeded": comparison["matches"],
                "finished_at_utc": utc_now(),
                "observations": observations,
                "reference_calibration_file": str(args.fixed_reference.resolve()),
                "fixed_reference_comparison": comparison,
                "flexible_calibration_repeated": False,
                "benchmark_authorized_or_started": False,
            })
            if not comparison["matches"]:
                summary["error"] = "Fixed sample differed from the paid calibration reference; stop before benchmark and retain existing workload requests for review."
            output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(json.dumps({
                "succeeded": comparison["matches"],
                "fixed_reference_comparison": comparison,
                "flexible_calibration_repeated": False,
                "benchmark_authorized_or_started": False,
                "output": str(output_path),
            }, indent=2, ensure_ascii=False))
            return 0 if comparison["matches"] else 1

        derived = derive_profiles(observations, fixed_spec, flex_specs)
        summary.update({"succeeded": True, "finished_at_utc": utc_now(), "observations": observations, **derived})
        output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        profile_path.write_text(json.dumps({"created_at_utc": utc_now(), "source": "calibration.json", "inventory_file": str(args.inventory.resolve()), **derived}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps({"succeeded": True, "calibrated_flexible_representatives": len(observations) - 1, "inventory_flexible_candidates": len(flex_specs), "calibration_failures": summary["calibration_failures"], "profiles": derived["profiles"], "fit_checks": derived["fit_checks"], "output": str(output_path)}, indent=2, ensure_ascii=False))
        return 0
    except Exception as exc:
        try:
            cleanup(recorder, args.timeout)
        except Exception as cleanup_error:
            recorder.write({"kind": "cleanup_error", "observed_at_utc": utc_now(), "error": f"{type(cleanup_error).__name__}: {cleanup_error}"})
        summary.update({"succeeded": False, "failed_at_utc": utc_now(), "observations": locals().get("observations", {}), "error": f"{type(exc).__name__}: {exc}"})
        output_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
