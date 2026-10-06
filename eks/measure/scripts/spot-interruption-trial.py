#!/usr/bin/env python3
"""Run one gated Karpenter Spot interruption trial and preserve its timeline."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-northeast-2"
ACCOUNT_SUFFIX = "0497"
CLUSTER = "onebite-eks-measure"
KUBECONFIG = Path.home() / ".kube" / "onebite-eks.kubeconfig"
CONTEXT = CLUSTER
NAMESPACE = "measure"
APP = "backend-probe"
NODEPOOL = "phase3-spot"
EXPERIMENT_TAG = {
    "Project": "onebite",
    "Temporary": "true",
    "measure-experiment": os.environ.get("PHASE3_EXPERIMENT_TAG", "phase3-spot-20260930"),
}
FIS_TARGET_NAME = "phase3-spot-worker"
FIS_ACTION_ID = "aws:ec2:send-spot-instance-interruptions"
SAMPLE_SEED = 3092026
WORKSPACE = os.environ.get("PHASE3_TF_WORKSPACE", "phase3-spot-interruption")

_FAILOVER_PATH = ROOT / "measure" / "scripts" / "failover-trial.py"
_SPEC = importlib.util.spec_from_file_location("onebite_failover_trial", _FAILOVER_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Could not load reusable failover collector helpers: {_FAILOVER_PATH}")
_FAILOVER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FAILOVER)
Recorder = _FAILOVER.Recorder
fetch = _FAILOVER.fetch

REACHABILITY_GATE_VERSION = os.environ.get("PHASE3_REACHABILITY_GATE_VERSION")
if REACHABILITY_GATE_VERSION is None:
    REACHABILITY_GATE_VERSION = "3" if os.environ.get("PHASE3_V3_REACHABILITY_GATE") == "1" else "0"
_PREFLIGHT_FILENAME = {
    "3": "phase3-v3-preflight.py",
    "4": "phase3-v4-preflight.py",
    "5": "phase3-v5-preflight.py",
    "6": "phase3-v5-preflight.py",
    "7": "phase3-v7-preflight.py",
}.get(REACHABILITY_GATE_VERSION, "phase3-v3-preflight.py")
_PREFLIGHT_PATH = ROOT / "measure" / "scripts" / _PREFLIGHT_FILENAME
_PREFLIGHT_SPEC = importlib.util.spec_from_file_location("onebite_phase3_preflight", _PREFLIGHT_PATH)
if _PREFLIGHT_SPEC is None or _PREFLIGHT_SPEC.loader is None:
    raise RuntimeError(f"Could not load Phase 3 reachability gates: {_PREFLIGHT_PATH}")
_PHASE3_PREFLIGHT = importlib.util.module_from_spec(_PREFLIGHT_SPEC)
_PREFLIGHT_SPEC.loader.exec_module(_PHASE3_PREFLIGHT)


def reachability_gate_enabled() -> bool:
    return REACHABILITY_GATE_VERSION in {"3", "4", "5", "6", "7"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_json(recorder: Any, *argv: str, label: str) -> dict[str, Any]:
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


def condition_timestamp(obj: dict[str, Any], condition_type: str, expected: str = "True") -> str | None:
    for condition in obj.get("status", {}).get("conditions", []):
        if condition.get("type") == condition_type and condition.get("status") == expected:
            return condition.get("lastTransitionTime")
    return None


def ready_pods(pods: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        pod for pod in pods.get("items", [])
        if condition_timestamp(pod, "Ready") is not None
    ]


def instance_id_from_node(node: dict[str, Any]) -> str | None:
    provider_id = node.get("spec", {}).get("providerID", "")
    candidate = provider_id.rsplit("/", 1)[-1]
    return candidate if candidate.startswith("i-") else None


def verify_account(recorder: Any) -> str:
    identity = run_json(
        recorder,
        "sts", "get-caller-identity", "--region", REGION, "--output", "json",
        label="AWS caller identity",
    )
    account_id = identity.get("Account", "")
    configured_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if account_id[-4:] != ACCOUNT_SUFFIX or configured_region != REGION:
        raise RuntimeError("AWS target must be account ****0497 in ap-northeast-2")
    return account_id


def verify_fis_template(recorder: Any, template_id: str) -> dict[str, Any]:
    payload = run_json(
        recorder,
        "fis", "get-experiment-template", "--id", template_id,
        "--region", REGION, "--output", "json",
        label="FIS template preflight",
    )
    template = payload.get("experimentTemplate", {})
    target = template.get("targets", {}).get(FIS_TARGET_NAME, {})
    tags = target.get("resourceTags", {})
    action = next(iter(template.get("actions", {}).values()), {})
    checks = {
        "target_type_is_spot_instance": target.get("resourceType") == "aws:ec2:spot-instance",
        "target_selects_exactly_one": target.get("selectionMode") == "COUNT(1)",
        "target_has_project_tag": tags.get("Project") == "onebite",
        "target_has_temporary_tag": tags.get("Temporary") == "true",
        "target_has_unique_phase3_tag": tags.get("measure-experiment") == EXPERIMENT_TAG["measure-experiment"],
        "action_is_spot_interruption": action.get("actionId") == FIS_ACTION_ID,
        "action_targets_named_spot_pool": action.get("targets", {}).get("SpotInstances") == FIS_TARGET_NAME,
        "interruption_notice_is_two_minutes": action.get("parameters", {}).get("durationBeforeInterruption") == "PT2M",
    }
    recorder.write({"kind": "fis_template_safety_check", "observed_at_utc": utc_now(), "checks": checks})
    if not all(checks.values()):
        raise RuntimeError(f"FIS template failed safety check: {checks}")
    return template


def wait_rollout(recorder: Any, timeout: str = "10m") -> None:
    result = recorder.kubectl(
        "rollout", "status", f"deployment/{APP}", "-n", NAMESPACE, f"--timeout={timeout}"
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "backend-probe did not become Ready")


def wait_http(url: str, timeout_seconds: int = 900) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = fetch(url, 3.0)
        if str(response.get("status", "")).startswith("2"):
            return
        time.sleep(3)
    raise RuntimeError("ALB endpoint did not return HTTP 2xx before timeout")


def fetch_and_record(recorder: Any, url: str, timeout: float, request_id: int) -> dict[str, Any]:
    result = fetch(url, timeout)
    result["finished_at_utc"] = utc_now()
    recorder.write({"kind": "http_request", "request_id": request_id, **result})
    return result


def elapsed_seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    try:
        start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (end_dt - start_dt).total_seconds())


def ordered_timestamp(stamps: list[str], latest: bool = False) -> str | None:
    parsed: list[tuple[datetime, str]] = []
    for stamp in stamps:
        try:
            parsed.append((datetime.fromisoformat(stamp.replace("Z", "+00:00")), stamp))
        except ValueError:
            continue
    if not parsed:
        return None
    return (max if latest else min)(parsed, key=lambda item: item[0])[1]


def event_instance_id(event: dict[str, Any]) -> str | None:
    detail = event.get("detail", {})
    if not isinstance(detail, dict):
        return None
    return detail.get("instance-id") or detail.get("instanceId") or detail.get("instance_id")


def ingress_url(recorder: Any, override: str | None) -> str:
    if override:
        return override
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        ingress = kubectl_json(recorder, "get", "ingress", APP, "-n", NAMESPACE, "-o", "json")
        addresses = ingress.get("status", {}).get("loadBalancer", {}).get("ingress", [])
        host = next((item.get("hostname") or item.get("ip") for item in addresses if item.get("hostname") or item.get("ip")), None)
        if host:
            return f"http://{host}/"
        time.sleep(3)
    raise RuntimeError("ALB Ingress did not publish an endpoint")


def get_workload_state(recorder: Any) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    pods = kubectl_json(recorder, "get", "pods", "-n", NAMESPACE, "-l", "app=backend-probe", "-o", "json")
    nodes = kubectl_json(recorder, "get", "nodes", "-l", "measure-experiment=phase3-spot", "-o", "json")
    claims = kubectl_json(recorder, "get", "nodeclaims.karpenter.sh", "-l", f"karpenter.sh/nodepool={NODEPOOL}", "-o", "json")
    return pods, nodes, claims


def ready_condition(obj: dict[str, Any]) -> bool:
    return any(
        item.get("type") == "Ready" and item.get("status") == "True"
        for item in obj.get("status", {}).get("conditions", [])
    )


def verify_clean_trial_state(recorder: Any) -> dict[str, Any]:
    pods, nodes, claims = get_workload_state(recorder)
    counts = {
        "benchmark_pods": len(pods.get("items", [])),
        "spot_worker_nodes": len(nodes.get("items", [])),
        "spot_nodeclaims": len(claims.get("items", [])),
    }
    if any(counts.values()):
        raise RuntimeError(f"trial requires zero benchmark Pods, Spot workers, and NodeClaims: {counts}")

    inventory = run_json(
        recorder,
        "ec2", "describe-instances", "--region", REGION,
        "--filters",
        "Name=tag:Project,Values=onebite",
        "Name=tag:Temporary,Values=true",
        f"Name=tag:measure-experiment,Values={EXPERIMENT_TAG['measure-experiment']}",
        "Name=instance-state-name,Values=pending,running,stopping,stopped",
        "--output", "json",
        label="Pretrial unique-tagged Spot worker inventory",
    )
    active_instances = [
        instance
        for reservation in inventory.get("Reservations", [])
        for instance in reservation.get("Instances", [])
    ]
    if active_instances:
        raise RuntimeError(f"trial requires zero prior tagged Spot EC2 instances: {[i.get('InstanceId') for i in active_instances]}")

    controller = kubectl_json(recorder, "get", "deployment", "karpenter", "-n", "karpenter", "-o", "json")
    nodepool = kubectl_json(recorder, "get", "nodepool", NODEPOOL, "-o", "json")
    nodeclass = kubectl_json(recorder, "get", "ec2nodeclass", NODEPOOL, "-o", "json")
    if int(controller.get("status", {}).get("availableReplicas", 0)) < 1:
        raise RuntimeError("Karpenter controller is not healthy before the trial")
    if not ready_condition(nodepool) or not ready_condition(nodeclass):
        raise RuntimeError("Phase 3 NodePool or EC2NodeClass is not Ready before the trial")

    system_nodegroup = run_json(
        recorder,
        "eks", "describe-nodegroup", "--region", REGION,
        "--cluster-name", CLUSTER,
        "--nodegroup-name", f"{CLUSTER}-system",
        "--output", "json",
        label="On-Demand system nodegroup preflight",
    ).get("nodegroup", {})
    if system_nodegroup.get("capacityType") != "ON_DEMAND":
        raise RuntimeError("Karpenter/system workloads must remain on the On-Demand system nodegroup")

    result = {
        "kind": "pretrial_zero_and_health_gate",
        "observed_at_utc": utc_now(),
        "counts": counts,
        "active_tagged_spot_instances": len(active_instances),
        "karpenter_available_replicas": controller.get("status", {}).get("availableReplicas"),
        "nodepool_ready": ready_condition(nodepool),
        "ec2nodeclass_ready": ready_condition(nodeclass),
        "system_nodegroup_capacity_type": system_nodegroup.get("capacityType"),
        "system_nodegroup_status": system_nodegroup.get("status"),
    }
    recorder.write(result)
    return result


def verify_persistent_trial_environment(recorder: Any) -> dict[str, Any]:
    controller = kubectl_json(recorder, "get", "deployment", "karpenter", "-n", "karpenter", "-o", "json")
    nodepool = kubectl_json(recorder, "get", "nodepool", NODEPOOL, "-o", "json")
    nodeclass = kubectl_json(recorder, "get", "ec2nodeclass", NODEPOOL, "-o", "json")
    available = int(controller.get("status", {}).get("availableReplicas", 0))
    observed_generation = int(controller.get("status", {}).get("observedGeneration", 0))
    generation = int(controller.get("metadata", {}).get("generation", 0))
    if available < 1 or observed_generation < generation:
        raise RuntimeError("Karpenter controller is not healthy before the persistent trial")
    if not ready_condition(nodepool) or not ready_condition(nodeclass):
        raise RuntimeError("Phase 3 NodePool or EC2NodeClass is not Ready before the persistent trial")

    system_nodegroup = run_json(
        recorder,
        "eks", "describe-nodegroup", "--region", REGION,
        "--cluster-name", CLUSTER,
        "--nodegroup-name", f"{CLUSTER}-system",
        "--output", "json",
        label="Persistent On-Demand system nodegroup preflight",
    ).get("nodegroup", {})
    if system_nodegroup.get("capacityType") != "ON_DEMAND":
        raise RuntimeError("Karpenter/system workloads must remain on the On-Demand system nodegroup")

    result = {
        "kind": "persistent_trial_environment_health_gate",
        "observed_at_utc": utc_now(),
        "karpenter_available_replicas": available,
        "karpenter_observed_generation": observed_generation,
        "karpenter_generation": generation,
        "nodepool_ready": ready_condition(nodepool),
        "ec2nodeclass_ready": ready_condition(nodeclass),
        "system_nodegroup_capacity_type": system_nodegroup.get("capacityType"),
        "system_nodegroup_status": system_nodegroup.get("status"),
        "workload_resources_preserved": True,
    }
    recorder.write(result)
    return result


def spot_instance_record(recorder: Any, instance_id: str, account_id: str) -> dict[str, Any]:
    payload = run_json(
        recorder,
        "ec2", "describe-instances", "--region", REGION,
        "--instance-ids", instance_id, "--output", "json",
        label=f"Spot target verification {instance_id}",
    )
    instances = [item for reservation in payload.get("Reservations", []) for item in reservation.get("Instances", [])]
    if len(instances) != 1:
        raise RuntimeError(f"expected one EC2 record for {instance_id}, got {len(instances)}")
    instance = instances[0]
    tags = {item.get("Key"): item.get("Value") for item in instance.get("Tags", [])}
    checks = {
        "state_running": instance.get("State", {}).get("Name") == "running",
        "market_spot": instance.get("InstanceLifecycle") == "spot",
        "required_tags": all(tags.get(key) == value for key, value in EXPERIMENT_TAG.items()),
        "account_matches": instance.get("OwnerId") in (None, account_id),
        "region_is_seoul": instance.get("Placement", {}).get("AvailabilityZone", "").startswith(REGION),
    }
    if not all(checks.values()):
        raise RuntimeError(f"EC2 target {instance_id} failed FIS safety checks: {checks}")
    return {
        "instance_id": instance_id,
        "instance_type": instance.get("InstanceType"),
        "state": instance.get("State", {}).get("Name"),
        "instance_lifecycle": instance.get("InstanceLifecycle"),
        "availability_zone": instance.get("Placement", {}).get("AvailabilityZone"),
        "launch_time": instance.get("LaunchTime"),
        "tags": {key: tags.get(key) for key in EXPERIMENT_TAG},
        "safety_checks": checks,
    }


def list_resolved_targets(recorder: Any, experiment_id: str) -> dict[str, Any]:
    return run_json(
        recorder,
        "fis", "list-experiment-resolved-targets",
        "--experiment-id", experiment_id,
        "--target-name", FIS_TARGET_NAME,
        "--region", REGION, "--output", "json",
        label="FIS resolved target lookup",
    )


def resource_ids(payload: Any) -> list[str]:
    found: set[str] = set()
    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"resourceId", "resourceArn", "resourceArnId", "arn"} and isinstance(item, str):
                    match = re.search(r"(i-[0-9a-f]+)$", item)
                    if match:
                        found.add(match.group(1))
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(payload)
    return sorted(found)


def receive_audit_events(recorder: Any, queue_url: str, max_messages: int = 10) -> list[dict[str, Any]]:
    result = recorder.aws(
        "sqs", "receive-message", "--region", REGION,
        "--queue-url", queue_url,
        "--max-number-of-messages", str(max_messages),
        "--wait-time-seconds", "0",
        "--attribute-names", "All",
        "--message-attribute-names", "All",
        "--output", "json",
    )
    if result.returncode:
        raise RuntimeError(f"EventBridge audit queue poll failed with exit {result.returncode}: {result.stderr.strip()}")
    # AWS CLI returns successful empty stdout when an SQS queue has no messages.
    try:
        response = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("EventBridge audit queue poll returned invalid JSON") from exc
    messages = response.get("Messages", [])
    events: list[dict[str, Any]] = []
    for message in messages:
        try:
            body = json.loads(message.get("Body", "{}"))
        except json.JSONDecodeError:
            body = {"unparsed_body": message.get("Body", "")}
        event = {
            "kind": "eventbridge_event",
            "observed_at_utc": utc_now(),
            "event_id": body.get("id"),
            "event_time_utc": body.get("time"),
            "source": body.get("source"),
            "detail_type": body.get("detail-type"),
            "resources": body.get("resources", []),
            "detail": body.get("detail", body),
            "sqs_attributes": message.get("Attributes", {}),
        }
        recorder.write(event)
        events.append(event)
        receipt = message.get("ReceiptHandle")
        if receipt:
            recorder.aws(
                "sqs", "delete-message", "--region", REGION,
                "--queue-url", queue_url, "--receipt-handle", receipt,
            )
    return events


def drain_audit_queue(recorder: Any, queue_url: str, phase: str) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    for _ in range(5):
        events = receive_audit_events(recorder, queue_url)
        if not events:
            break
        for event in events:
            event["phase"] = phase
        collected.extend(events)
    return collected


def queue_attributes(recorder: Any, queue_url: str, label: str) -> dict[str, Any]:
    payload = run_json(
        recorder,
        "sqs", "get-queue-attributes", "--region", REGION,
        "--queue-url", queue_url,
        "--attribute-names", "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
        "--output", "json",
        label=f"{label} queue attributes",
    )
    return payload.get("Attributes", {})


def summarize_node(node: dict[str, Any]) -> dict[str, Any]:
    labels = node.get("metadata", {}).get("labels", {})
    conditions = node.get("status", {}).get("conditions", [])
    return {
        "name": node.get("metadata", {}).get("name"),
        "created_at_utc": node.get("metadata", {}).get("creationTimestamp"),
        "provider_id": node.get("spec", {}).get("providerID"),
        "instance_id": instance_id_from_node(node),
        "instance_type": labels.get("node.kubernetes.io/instance-type"),
        "nodepool": labels.get("karpenter.sh/nodepool"),
        "zone": labels.get("topology.kubernetes.io/zone"),
        "ready_at_utc": condition_timestamp(node, "Ready"),
        "conditions": [
            {"type": item.get("type"), "status": item.get("status"), "transition_at_utc": item.get("lastTransitionTime"), "reason": item.get("reason")}
            for item in conditions
        ],
        "taints": node.get("spec", {}).get("taints", []),
    }


def summarize_claim(claim: dict[str, Any]) -> dict[str, Any]:
    metadata = claim.get("metadata", {})
    status = claim.get("status", {})
    return {
        "name": metadata.get("name"),
        "created_at_utc": metadata.get("creationTimestamp"),
        "deletion_timestamp_utc": metadata.get("deletionTimestamp"),
        "nodepool": metadata.get("labels", {}).get("karpenter.sh/nodepool"),
        "instance_type": status.get("instanceType"),
        "provider_id": status.get("providerID"),
        "instance_id": status.get("providerID", "").rsplit("/", 1)[-1] or None,
        "conditions": [
            {"type": item.get("type"), "status": item.get("status"), "transition_at_utc": item.get("lastTransitionTime"), "reason": item.get("reason"), "message": item.get("message")}
            for item in status.get("conditions", [])
        ],
    }


def summarize_pod(pod: dict[str, Any]) -> dict[str, Any]:
    status = pod.get("status", {})
    return {
        "name": pod.get("metadata", {}).get("name"),
        "created_at_utc": pod.get("metadata", {}).get("creationTimestamp"),
        "deletion_timestamp_utc": pod.get("metadata", {}).get("deletionTimestamp"),
        "node_name": pod.get("spec", {}).get("nodeName"),
        "phase": status.get("phase"),
        "ready_at_utc": condition_timestamp(pod, "Ready"),
        "conditions": [
            {"type": item.get("type"), "status": item.get("status"), "transition_at_utc": item.get("lastTransitionTime"), "reason": item.get("reason")}
            for item in status.get("conditions", [])
        ],
        "container_terminated_at_utc": [
            container.get("state", {}).get("terminated", {}).get("finishedAt")
            for container in status.get("containerStatuses", [])
            if container.get("state", {}).get("terminated", {}).get("finishedAt")
        ],
        "container_statuses": [
            {
                "name": container.get("name"),
                "restart_count": container.get("restartCount"),
                "started_at_utc": container.get("state", {}).get("running", {}).get("startedAt"),
                "terminated_at_utc": container.get("state", {}).get("terminated", {}).get("finishedAt"),
                "last_terminated_at_utc": container.get("lastState", {}).get("terminated", {}).get("finishedAt"),
                "state": next((key for key in ("waiting", "running", "terminated") if key in container.get("state", {})), None),
            }
            for container in status.get("containerStatuses", [])
        ],
    }


def take_snapshot(recorder: Any, known_instance_ids: set[str], trial: int) -> tuple[dict[str, Any], set[str]]:
    pods, nodes, claims = get_workload_state(recorder)
    all_ids = {item for item in (instance_id_from_node(node) for node in nodes.get("items", [])) if item}
    all_ids.update(
        claim.get("status", {}).get("providerID", "").rsplit("/", 1)[-1]
        for claim in claims.get("items", [])
        if claim.get("status", {}).get("providerID", "").rsplit("/", 1)[-1].startswith("i-")
    )
    new_ids = all_ids - known_instance_ids
    known_instance_ids.update(all_ids)
    ec2_rows: list[dict[str, Any]] = []
    if all_ids:
        payload = run_json(
            recorder,
            "ec2", "describe-instances", "--region", REGION,
            "--instance-ids", *sorted(all_ids), "--output", "json",
            label="Experiment EC2 instance snapshot",
        )
        for reservation in payload.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                tags = {item.get("Key"): item.get("Value") for item in instance.get("Tags", [])}
                ec2_rows.append({
                    "instance_id": instance.get("InstanceId"),
                    "instance_type": instance.get("InstanceType"),
                    "state": instance.get("State", {}).get("Name"),
                    "instance_lifecycle": instance.get("InstanceLifecycle", "on-demand"),
                    "launch_time_utc": instance.get("LaunchTime"),
                    "availability_zone": instance.get("Placement", {}).get("AvailabilityZone"),
                    "tags": {key: tags.get(key) for key in EXPERIMENT_TAG},
                })

    timestamp = utc_now()
    snapshot = {
        "kind": "recovery_snapshot",
        "trial": trial,
        "observed_at_utc": timestamp,
        "pods": [summarize_pod(item) for item in pods.get("items", [])],
        "ready_replica_count": len(ready_pods(pods)),
        "nodes": [summarize_node(item) for item in nodes.get("items", [])],
        "nodeclaims": [summarize_claim(item) for item in claims.get("items", [])],
        "experiment_instances": ec2_rows,
        "new_instance_ids": sorted(new_ids),
    }
    recorder.write(snapshot)
    return snapshot, new_ids


def poll_kubernetes_events(recorder: Any, original_names: set[str]) -> list[dict[str, Any]]:
    payload = kubectl_json(recorder, "get", "events", "-A", "-o", "json")
    observed: list[dict[str, Any]] = []
    for event in payload.get("items", []):
        ref = event.get("regarding", event.get("involvedObject", {}))
        reason = event.get("reason", "")
        message = event.get("note", event.get("message", ""))
        is_related = ref.get("name") in original_names or any(word in f"{reason} {message}".lower() for word in ("interrupt", "spot", "disrupt"))
        if not is_related:
            continue
        observed.append({
            "reason": reason,
            "message": message,
            "event_time_utc": event.get("eventTime") or event.get("lastTimestamp") or event.get("metadata", {}).get("creationTimestamp"),
            "first_timestamp_utc": event.get("firstTimestamp"),
            "last_timestamp_utc": event.get("lastTimestamp"),
            "regarding": ref,
            "reporting_controller": event.get("reportingController", event.get("source", {}).get("component")),
        })
    recorder.write({"kind": "kubernetes_events", "observed_at_utc": utc_now(), "events": observed})
    return observed


def poll_controller_logs(recorder: Any) -> list[str]:
    result = recorder.kubectl(
        "logs", "deployment/karpenter", "-n", "karpenter",
        "--all-containers=true", "--timestamps=true", "--since=15s",
    )
    if result.returncode:
        recorder.write({"kind": "controller_log_poll_error", "observed_at_utc": utc_now(), "stderr": result.stderr})
        return []
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    for line in lines:
        recorder.write({"kind": "karpenter_log_line", "observed_at_utc": utc_now(), "line": line})
    return lines


def original_pod_drain_timeline(
    snapshots: list[dict[str, Any]], original_names: set[str], events: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for pod_name in sorted(original_names):
        pod_rows = [
            (snapshot.get("observed_at_utc"), pod)
            for snapshot in snapshots
            for pod in snapshot.get("pods", [])
            if pod.get("name") == pod_name
        ]
        deletion_stamps = [pod.get("deletion_timestamp_utc") for _, pod in pod_rows if pod.get("deletion_timestamp_utc")]
        terminated_stamps = [
            stamp
            for _, pod in pod_rows
            for stamp in pod.get("container_terminated_at_utc", [])
            if stamp
        ]
        unique_events: dict[tuple[Any, ...], dict[str, Any]] = {}
        for event in events:
            if event.get("regarding", {}).get("name") != pod_name:
                continue
            if event.get("reason") not in {"Killing", "PreStopHook", "FailedPreStopHook"}:
                continue
            event_key = (
                event.get("reason"), event.get("event_time_utc"), pod_name,
                event.get("message"),
            )
            unique_events[event_key] = event
        related_events = list(unique_events.values())
        killing_at = ordered_timestamp([
            event.get("event_time_utc") for event in related_events
            if event.get("reason") == "Killing" and event.get("event_time_utc")
        ])
        deletion_at = ordered_timestamp(deletion_stamps)
        terminated_at = ordered_timestamp(terminated_stamps)
        rows.append({
            "pod_name": pod_name,
            "deletion_timestamp_utc": deletion_at,
            "deletion_first_observed_at_utc": next(
                (observed for observed, pod in pod_rows if pod.get("deletion_timestamp_utc")), None
            ),
            "container_terminated_at_utc": terminated_at,
            "deletion_to_container_termination_seconds": elapsed_seconds(deletion_at, terminated_at),
            "killing_event_at_utc": killing_at,
            "killing_event_to_container_termination_seconds": elapsed_seconds(killing_at, terminated_at),
            "termination_events": related_events,
            "sample_count": len(pod_rows),
        })
    return rows


def price_ec2_instance(recorder: Any, instance_type: str, zone: str) -> dict[str, Any]:
    history = run_json(
        recorder,
        "ec2", "describe-spot-price-history", "--region", REGION,
        "--availability-zone", zone,
        "--product-descriptions", "Linux/UNIX",
        "--instance-types", instance_type,
        "--max-results", "20",
        "--query", "SpotPriceHistory[0].{price:SpotPrice,timestamp:Timestamp,type:InstanceType,zone:AvailabilityZone}",
        "--output", "json",
        label=f"Spot price {instance_type}",
    )
    history = history if isinstance(history, dict) else {}
    spot_price = history.get("price")
    filters = [
        "Type=TERM_MATCH,Field=instanceType,Value=" + instance_type,
        "Type=TERM_MATCH,Field=location,Value=Asia Pacific (Seoul)",
        "Type=TERM_MATCH,Field=operatingSystem,Value=Linux",
        "Type=TERM_MATCH,Field=tenancy,Value=Shared",
        "Type=TERM_MATCH,Field=preInstalledSw,Value=NA",
        "Type=TERM_MATCH,Field=capacitystatus,Value=Used",
    ]
    products = run_json(
        recorder,
        "pricing", "get-products", "--region", "us-east-1",
        "--service-code", "AmazonEC2", "--filters", *filters,
        "--max-results", "100", "--output", "json",
        label=f"On-Demand public rate {instance_type}",
    )
    on_demand_prices: list[float] = []
    for row in products.get("PriceList", []):
        try:
            product = json.loads(row) if isinstance(row, str) else row
            if product.get("product", {}).get("attributes", {}).get("marketoption") not in (None, "OnDemand"):
                continue
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for dimension in term.get("priceDimensions", {}).values():
                    unit = dimension.get("unit")
                    value = dimension.get("pricePerUnit", {}).get("USD")
                    if unit == "Hrs" and value not in (None, ""):
                        on_demand_prices.append(float(value))
        except (ValueError, TypeError):
            continue
    return {
        "instance_type": instance_type,
        "availability_zone": zone,
        "spot_usd_per_hour": spot_price,
        "spot_price_timestamp_utc": history.get("timestamp") if history else None,
        "same_type_on_demand_usd_per_hour": min(on_demand_prices) if on_demand_prices else None,
        "on_demand_price_product_count": len(products.get("PriceList", [])),
        "basis": "Spot price history and Linux Shared On-Demand Price List API; capacity rate only, not billed spend.",
    }


def analyze_requests(rows: list[tuple[int, dict[str, Any]]], rps: float, spot_event_time: str | None) -> dict[str, Any]:
    rows.sort(key=lambda row: row[0])
    failures = [not str(result.get("status", "")).startswith("2") for _, result in rows]
    failure_count = sum(failures)
    failure_runs: list[dict[str, Any]] = []
    i = 0
    while i < len(rows):
        if not failures[i]:
            i += 1
            continue
        begin = i
        while i < len(rows) and failures[i]:
            i += 1
        end = i
        start_time = rows[begin][1].get("started_at_utc")
        if end < len(rows):
            end_time = rows[end][1].get("finished_at_utc") or rows[end][1].get("started_at_utc")
            recovery_at = end_time
        else:
            last_result = rows[end - 1][1]
            end_time = last_result.get("finished_at_utc")
            recovery_at = None
        try:
            start_dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(str(end_time).replace("Z", "+00:00"))
            duration = max(0.0, (end_dt - start_dt).total_seconds())
        except (ValueError, TypeError):
            duration = None
        failure_runs.append({"from_utc": start_time, "to_utc": recovery_at or end_time, "recovered_at_utc": recovery_at, "failed_requests": end - begin, "seconds": duration})

    steady_after_event: str | None = None
    event_dt = None
    if spot_event_time:
        try:
            event_dt = datetime.fromisoformat(spot_event_time.replace("Z", "+00:00"))
        except ValueError:
            pass
    good_streak = 0
    for index, (_, result) in enumerate(rows):
        started = result.get("started_at_utc")
        if event_dt and started:
            try:
                started_dt = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                if started_dt < event_dt:
                    continue
            except ValueError:
                pass
        if not failures[index]:
            good_streak += 1
            if good_streak == 3:
                steady_after_event = result.get("finished_at_utc") or started
                break
        else:
            good_streak = 0

    unavailable = [item["seconds"] for item in failure_runs if item.get("seconds") is not None]
    rate = failure_count / len(rows) if rows else None
    return {
        "http_total_requests": len(rows),
        "http_failed_requests": failure_count,
        "http_failure_rate": rate,
        "expected_request_rate_per_second": rps,
        "max_contiguous_failure_window_seconds": max(unavailable) if unavailable else 0.0,
        "failure_windows": failure_runs,
        "three_consecutive_successes_confirmed_after_interruption_at_utc": steady_after_event,
        "max_failure_window_definition": "from the first failed request start to completion of the next successful response; final unrecovered block ends at the last failed response completion",
    }


def timestamp_from_controller_log(line: str) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s", line)
    if match:
        return match.group(1)
    return None


def cleanup_workers(recorder: Any, timeout_seconds: int = 900, pre_fis_gate_failed: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    deleted_app = recorder.kubectl("delete", "deployment", APP, "-n", NAMESPACE, "--ignore-not-found", "--wait=true")
    if deleted_app.returncode:
        errors.append(deleted_app.stderr.strip() or "could not delete benchmark deployment")
    if pre_fis_gate_failed and reachability_gate_enabled():
        for resource in ("service", "pdb", "ingress"):
            deleted = recorder.kubectl("delete", resource, APP, "-n", NAMESPACE, "--ignore-not-found", "--wait=true")
            if deleted.returncode:
                errors.append(deleted.stderr.strip() or f"could not delete benchmark {resource}")
    claims = kubectl_json(recorder, "get", "nodeclaims.karpenter.sh", "-l", f"karpenter.sh/nodepool={NODEPOOL}", "-o", "json")
    claim_names = [item.get("metadata", {}).get("name") for item in claims.get("items", []) if item.get("metadata", {}).get("name")]
    for name in claim_names:
        response = recorder.kubectl("delete", "nodeclaim", name, "--ignore-not-found", "--wait=false")
        if response.returncode:
            errors.append(response.stderr.strip() or f"could not delete NodeClaim {name}")
    deadline = time.monotonic() + timeout_seconds
    last = {"pods": None, "nodes": None, "nodeclaims": None, "instances": None}
    while time.monotonic() < deadline:
        pods, nodes, remaining_claims = get_workload_state(recorder)
        active_ids = {item for item in (instance_id_from_node(node) for node in nodes.get("items", [])) if item}
        ec2_payload = run_json(
            recorder,
            "ec2", "describe-instances", "--region", REGION,
            "--filters", "Name=tag:Project,Values=onebite", "Name=tag:Temporary,Values=true", f"Name=tag:measure-experiment,Values={EXPERIMENT_TAG['measure-experiment']}",
            "--output", "json",
            label="Cleanup tagged Phase 3 EC2 inventory",
        )
        instances = [instance for reservation in ec2_payload.get("Reservations", []) for instance in reservation.get("Instances", [])]
        active_instances = [instance for instance in instances if instance.get("State", {}).get("Name") != "terminated"]
        last = {
            "pods": [item.get("metadata", {}).get("name") for item in pods.get("items", [])],
            "nodes": [item.get("metadata", {}).get("name") for item in nodes.get("items", [])],
            "nodeclaims": [item.get("metadata", {}).get("name") for item in remaining_claims.get("items", [])],
            "instances": [{"id": item.get("InstanceId"), "state": item.get("State", {}).get("Name")} for item in active_instances],
        }
        if not any(last.values()):
            return {"complete": True, "errors": errors, "last_state": last, "targeted_nodeclaim_names": claim_names}
        time.sleep(3)
    return {"complete": False, "errors": errors, "last_state": last, "targeted_nodeclaim_names": claim_names}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trial", type=int, choices=range(1, 11), required=True)
    parser.add_argument("--fis-template-id", required=True)
    parser.add_argument("--interruption-queue-url", required=True)
    parser.add_argument("--audit-queue-url", required=True)
    parser.add_argument("--url", help="Optional ALB endpoint; otherwise discovered from the backend-probe Ingress.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workload-manifest", type=Path, default=Path(os.environ.get(
        "PHASE3_WORKLOAD_MANIFEST",
        str(ROOT / "measure" / "manifests" / "spot-recovery-app.yaml"),
    )).expanduser())
    parser.add_argument("--execute-fis", action="store_true", help="Required to start the approved Spot interruption experiment.")
    parser.add_argument(
        "--preserve-environment",
        action="store_true",
        help="Keep the workload, Ingress, workers, and cluster resources for a fixed multi-trial batch.",
    )
    parser.add_argument("--rps", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=600.0)
    parser.add_argument("--interrupt-after", type=float, default=30.0)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    args = parser.parse_args()

    if not args.execute_fis:
        parser.error("pass --execute-fis only after the separate Phase 3 benchmark approval")
    if args.trial > 5 and not args.preserve_environment:
        parser.error("trials 6-10 require --preserve-environment")
    if os.environ.get("TF_WORKSPACE") != WORKSPACE:
        parser.error(f"TF_WORKSPACE must be {WORKSPACE}")
    if not KUBECONFIG.is_file():
        parser.error(f"Dedicated kubeconfig is missing: {KUBECONFIG}")
    if args.output.exists() or args.output.with_suffix(".jsonl").exists() or args.output.with_suffix(".csv").exists():
        parser.error("Refusing to overwrite previous Phase 3 trial output")
    phase3_manifest_path = args.output.parent / f"spot-recovery-app-{args.trial:02d}-v{REACHABILITY_GATE_VERSION}-rendered.yaml"
    if reachability_gate_enabled() and phase3_manifest_path.exists():
        parser.error(f"Refusing to overwrite previous Phase 3 v{REACHABILITY_GATE_VERSION} rendered manifest: {phase3_manifest_path}")
    if args.rps <= 0 or args.duration <= args.interrupt_after or args.poll_interval <= 0:
        parser.error("rps/poll interval must be positive and duration must exceed interrupt-after")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    raw_path = args.output.with_suffix(".jsonl")
    recorder = Recorder(raw_path)
    trial_started = utc_now()
    http_rows: list[tuple[int, Any]] = []
    selected_target: dict[str, Any] | None = None
    experiment_id: str | None = None
    fis_start_requested_at: str | None = None
    fis_start_response_at: str | None = None
    fis_experiment_start_time: str | None = None
    failure: str | None = None
    cleanup_result: dict[str, Any] | None = None
    snapshots: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    audit_rows: list[dict[str, Any]] = []
    logs: list[str] = []
    known_instance_ids: set[str] = set()
    original_node_names: set[str] = set()
    original_pod_names: set[str] = set()
    before_ready = 0
    http_url = args.url
    template: dict[str, Any] = {}
    account_id = ""

    try:
        account_id = verify_account(recorder)
        template = verify_fis_template(recorder, args.fis_template_id)
        old_depth = queue_attributes(recorder, args.interruption_queue_url, "Karpenter interruption")
        if int(old_depth.get("ApproximateNumberOfMessages", "0")) or int(old_depth.get("ApproximateNumberOfMessagesNotVisible", "0")):
            raise RuntimeError(f"Karpenter interruption queue is not empty before trial: {old_depth}")
        drain_audit_queue(recorder, args.audit_queue_url, "pretrial-drain")
        if args.preserve_environment:
            verify_persistent_trial_environment(recorder)
        else:
            verify_clean_trial_state(recorder)

        workload_manifest = args.workload_manifest.resolve()
        if not workload_manifest.is_file():
            raise RuntimeError(f"workload manifest does not exist: {workload_manifest}")
        workload_manifest_sha256 = __import__("hashlib").sha256(workload_manifest.read_bytes()).hexdigest()
        if reachability_gate_enabled() and not args.preserve_environment:
            manifest_text = workload_manifest.read_text(encoding="utf-8")
            old_tag = "measure-experiment=phase3-spot-20260930"
            if manifest_text.count(old_tag) != 1:
                raise RuntimeError(f"Could not uniquely render the Phase 3 v{REACHABILITY_GATE_VERSION} ALB resource tag")
            phase3_manifest_path.write_text(
                manifest_text.replace(old_tag, f"measure-experiment={EXPERIMENT_TAG['measure-experiment']}"),
                encoding="utf-8",
            )
            workload_manifest = phase3_manifest_path
        if not args.preserve_environment:
            workload_apply = recorder.kubectl("apply", "-f", str(workload_manifest))
            if workload_apply.returncode:
                raise RuntimeError(workload_apply.stderr.strip() or "could not apply the Phase 3 workload")
        wait_rollout(recorder)
        http_url = ingress_url(recorder, args.url)
        if reachability_gate_enabled():
            _PHASE3_PREFLIGHT.run_preflight(
                recorder,
                http_url,
                args.trial,
                EXPERIMENT_TAG["measure-experiment"],
                verify_node_peer_http_rule=REACHABILITY_GATE_VERSION in {"6", "7"},
            )
        else:
            wait_http(http_url)

        pods_payload, nodes_payload, claims_payload = get_workload_state(recorder)
        ready = ready_pods(pods_payload)
        workers = nodes_payload.get("items", [])
        if len(ready) != 2 or len({pod.get("spec", {}).get("nodeName") for pod in ready}) != 2:
            raise RuntimeError("Phase 3 requires exactly two Ready replicas on distinct Spot worker nodes")
        if len(workers) != 2:
            raise RuntimeError(f"expected exactly two phase3-spot nodes, found {len(workers)}")
        node_by_name = {node.get("metadata", {}).get("name"): node for node in workers}
        pod_host_names = {pod.get("spec", {}).get("nodeName") for pod in ready}
        if pod_host_names != set(node_by_name):
            raise RuntimeError("both Ready replicas must occupy the two experiment Spot nodes")
        before_ready = len(ready)
        original_node_names = set(node_by_name)
        original_pod_names = {pod.get("metadata", {}).get("name") for pod in ready}
        if None in original_pod_names:
            raise RuntimeError("Ready workload Pods are missing names")
        workers_by_id = {}
        for node in workers:
            instance_id = instance_id_from_node(node)
            if not instance_id:
                raise RuntimeError("phase3-spot Node has no EC2 provider ID")
            verified = spot_instance_record(recorder, instance_id, account_id)
            workers_by_id[instance_id] = verified
            if node.get("metadata", {}).get("labels", {}).get("karpenter.sh/nodepool") != NODEPOOL:
                raise RuntimeError("experiment worker is not owned by the Phase 3 Karpenter NodePool")
            known_instance_ids.add(instance_id)
        if args.preserve_environment:
            claim_instance_ids = {
                item.get("status", {}).get("providerID", "").rsplit("/", 1)[-1]
                for item in claims_payload.get("items", [])
                if item.get("status", {}).get("providerID", "").rsplit("/", 1)[-1].startswith("i-")
            }
            all_worker_node_ids = {
                instance_id_from_node(item)
                for item in nodes_payload.get("items", [])
                if instance_id_from_node(item)
            }
            terminating_nodes = [
                item.get("metadata", {}).get("name")
                for item in nodes_payload.get("items", [])
                if item.get("metadata", {}).get("deletionTimestamp")
            ]
            terminating_claims = [
                item.get("metadata", {}).get("name")
                for item in claims_payload.get("items", [])
                if item.get("metadata", {}).get("deletionTimestamp")
            ]
            if (
                len(nodes_payload.get("items", [])) != 2
                or all_worker_node_ids != set(workers_by_id)
                or len(claims_payload.get("items", [])) != 2
                or claim_instance_ids != set(workers_by_id)
                or terminating_nodes
                or terminating_claims
            ):
                raise RuntimeError(
                    "persistent trial requires exactly two non-terminating Spot Nodes and two current NodeClaims "
                    "matching the two Ready Spot workers; "
                    f"nodes={len(nodes_payload.get('items', []))} node_instances={sorted(all_worker_node_ids)} "
                    f"claims={len(claims_payload.get('items', []))} claim_instances={sorted(claim_instance_ids)} "
                    f"terminating_nodes={terminating_nodes} terminating_claims={terminating_claims} "
                    f"worker_instances={sorted(workers_by_id)}"
                )
        active_tagged_result = recorder.aws(
            "ec2", "describe-instances", "--region", REGION,
            "--filters", "Name=tag:Project,Values=onebite", "Name=tag:Temporary,Values=true", f"Name=tag:measure-experiment,Values={EXPERIMENT_TAG['measure-experiment']}", "Name=instance-state-name,Values=pending,running,stopping,stopped",
            "--query", "Reservations[].Instances[].{id:InstanceId,state:State.Name,lifecycle:InstanceLifecycle}",
            "--output", "json",
        )
        if active_tagged_result.returncode:
            raise RuntimeError(f"FIS target inventory query failed: {active_tagged_result.stderr.strip()}")
        active_tagged = json.loads(active_tagged_result.stdout)
        if {item.get("id") for item in active_tagged} != set(workers_by_id):
            raise RuntimeError("tag-scoped FIS target inventory does not exactly match the two Ready Spot worker nodes")

        baseline = {
            "pods": [summarize_pod(pod) for pod in ready],
            "nodes": [summarize_node(node) for node in workers],
            "nodeclaims": [summarize_claim(item) for item in claims_payload.get("items", [])],
            "spot_instances": list(workers_by_id.values()),
            "fis_target_count": 1,
            "fis_candidate_count": len(active_tagged),
            "ingress_url": http_url,
        }
        pdb = kubectl_json(recorder, "get", "pdb", APP, "-n", NAMESPACE, "-o", "json")
        disruptions_allowed = int(pdb.get("status", {}).get("disruptionsAllowed", 0))
        if disruptions_allowed < 1:
            raise RuntimeError(f"PDB must allow at least one voluntary disruption before FIS: {disruptions_allowed}")
        if any(pod.get("metadata", {}).get("annotations", {}).get("karpenter.sh/do-not-disrupt") == "true" for pod in ready):
            raise RuntimeError("workload Pod has karpenter.sh/do-not-disrupt=true and could block draining")
        baseline["pdb_disruptions_allowed"] = disruptions_allowed
        recorder.write({"kind": "pretrial_baseline", "trial": args.trial, "observed_at_utc": utc_now(), **baseline})

        request_interval = 1.0 / args.rps
        started_mono = time.monotonic()
        ends_mono = started_mono + args.duration
        next_snapshot_mono = started_mono
        next_log_mono = started_mono
        next_fis_poll_mono = started_mono
        fis_started = False
        fis_resolved = False
        fis_status = None
        fis_template_targets = template.get("targets", {}).get(FIS_TARGET_NAME, {})
        recorder.write({"kind": "load_start", "trial": args.trial, "started_at_utc": utc_now(), "rps": args.rps, "duration_seconds": args.duration, "url": http_url})

        request_futures: list[tuple[int, Any]] = []
        request_stop = threading.Event()

        def produce_http_requests(pool: concurrent.futures.ThreadPoolExecutor) -> None:
            request_id = 0
            next_request_mono = started_mono
            while not request_stop.is_set() and next_request_mono < ends_mono:
                future = pool.submit(fetch_and_record, recorder, http_url, args.request_timeout, request_id)
                request_futures.append((request_id, future))
                request_id += 1
                next_request_mono += request_interval
                request_stop.wait(max(0.0, next_request_mono - time.monotonic()))

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=64)
        producer = threading.Thread(target=produce_http_requests, args=(pool,), name="phase3-http-load", daemon=True)
        producer.start()
        try:
            while time.monotonic() < ends_mono:
                now = time.monotonic()
                elapsed = now - started_mono
                if not fis_started and elapsed >= args.interrupt_after:
                    fis_start_requested_at = utc_now()
                    request = recorder.aws(
                        "fis", "start-experiment",
                        "--experiment-template-id", args.fis_template_id,
                        "--region", REGION,
                        "--client-token", f"phase3-spot-{args.trial}-{int(time.time())}",
                        "--tags", f"Project=onebite,Temporary=true,measure-experiment={EXPERIMENT_TAG['measure-experiment']}",
                        "--output", "json",
                    )
                    if request.returncode:
                        raise RuntimeError(f"FIS StartExperiment failed: {request.stderr.strip()}")
                    fis_start_response_at = utc_now()
                    response = json.loads(request.stdout)
                    experiment = response.get("experiment", {})
                    experiment_id = experiment.get("id")
                    fis_experiment_start_time = experiment.get("startTime") or experiment.get("start_time")
                    if not experiment_id:
                        raise RuntimeError("FIS StartExperiment returned no experiment ID")
                    fis_started = True
                    next_fis_poll_mono = now
                    recorder.write({"kind": "fis_experiment_started", "trial": args.trial, "start_requested_at_utc": fis_start_requested_at, "start_response_at_utc": fis_start_response_at, "experiment_start_time_utc": fis_experiment_start_time, "experiment_id": experiment_id, "template_id": args.fis_template_id, "target_name": FIS_TARGET_NAME, "target_type": fis_template_targets.get("resourceType"), "selection_mode": fis_template_targets.get("selectionMode")})

                if now >= next_snapshot_mono:
                    snapshot, new_ids = take_snapshot(recorder, known_instance_ids, args.trial)
                    snapshots.append(snapshot)
                    if new_ids:
                        recorder.write({"kind": "new_ec2_instance_ids", "observed_at_utc": utc_now(), "instance_ids": sorted(new_ids)})
                    event_rows = poll_kubernetes_events(recorder, original_node_names | original_pod_names)
                    events.extend(event_rows)
                    audit_rows.extend(receive_audit_events(recorder, args.audit_queue_url))
                    queue_depth = queue_attributes(recorder, args.interruption_queue_url, "Karpenter interruption")
                    recorder.write({"kind": "karpenter_queue_depth", "observed_at_utc": utc_now(), "attributes": queue_depth})
                    next_snapshot_mono += args.poll_interval

                if fis_started and experiment_id and now >= next_fis_poll_mono:
                    resolved = list_resolved_targets(recorder, experiment_id)
                    resolved_ids = resource_ids(resolved)
                    if resolved_ids:
                        if len(resolved_ids) != 1 or resolved_ids[0] not in workers_by_id:
                            raise RuntimeError(f"FIS resolved outside the two verified Spot workers: {resolved_ids}")
                        selected_target = workers_by_id[resolved_ids[0]]
                        fis_resolved = True
                        recorder.write({"kind": "fis_target_resolved", "trial": args.trial, "observed_at_utc": utc_now(), "selected_instance": selected_target, "resolved_targets": resolved})
                    experiment_payload = run_json(
                        recorder,
                        "fis", "get-experiment", "--id", experiment_id,
                        "--region", REGION, "--output", "json",
                        label="FIS experiment status",
                    )
                    fis_status = experiment_payload.get("experiment", {}).get("state", {}).get("status")
                    if fis_status in {"failed", "stopped", "completed", "cancelled"}:
                        recorder.write({"kind": "fis_experiment_terminal_state", "trial": args.trial, "observed_at_utc": utc_now(), "status": fis_status, "experiment": experiment_payload.get("experiment", {})})
                    next_fis_poll_mono = now + 2.0

                if now >= next_log_mono:
                    logs.extend(poll_controller_logs(recorder))
                    next_log_mono += 10.0
                time.sleep(min(0.1, max(0.0, ends_mono - time.monotonic())))
        finally:
            request_stop.set()
            producer.join(timeout=args.request_timeout + 2.0)
            pool.shutdown(wait=True)
        http_rows = [(request_id, future.result()) for request_id, future in request_futures]
        if not fis_started or not experiment_id:
            raise RuntimeError("FIS interruption was not started during the load window")

        # Wait briefly for the action state and final post-load recovery snapshot.
        action_deadline = time.monotonic() + 120
        while time.monotonic() < action_deadline and fis_status not in {"completed", "failed", "stopped", "cancelled"}:
            experiment_payload = run_json(recorder, "fis", "get-experiment", "--id", experiment_id, "--region", REGION, "--output", "json", label="FIS completion check")
            fis_status = experiment_payload.get("experiment", {}).get("state", {}).get("status")
            time.sleep(3)
        final_snapshot, _ = take_snapshot(recorder, known_instance_ids, args.trial)
        snapshots.append(final_snapshot)
        events.extend(poll_kubernetes_events(recorder, original_node_names | original_pod_names))
        audit_rows.extend(receive_audit_events(recorder, args.audit_queue_url))
        if not selected_target and experiment_id:
            resolved = list_resolved_targets(recorder, experiment_id)
            ids = resource_ids(resolved)
            if len(ids) == 1 and ids[0] in workers_by_id:
                selected_target = workers_by_id[ids[0]]
                recorder.write({"kind": "fis_target_resolved", "trial": args.trial, "observed_at_utc": utc_now(), "selected_instance": selected_target, "resolved_targets": resolved})

        target_id = selected_target.get("instance_id") if selected_target else None
        all_interruption_events = [item for item in audit_rows if item.get("detail_type") == "EC2 Spot Instance Interruption Warning"]
        all_rebalance_events = [item for item in audit_rows if item.get("detail_type") == "EC2 Instance Rebalance Recommendation"]
        interruption_events = [item for item in all_interruption_events if event_instance_id(item) == target_id]
        rebalance_events = [item for item in all_rebalance_events if event_instance_id(item) == target_id]
        spot_event_time = next((item.get("event_time_utc") for item in interruption_events if item.get("event_time_utc")), None)
        request_metrics = analyze_requests(http_rows, args.rps, spot_event_time)

        csv_path = args.output.with_suffix(".csv")
        with csv_path.open("w", encoding="utf-8", newline="") as stream:
            import csv
            writer = csv.DictWriter(stream, fieldnames=["request_id", "started_at_utc", "finished_at_utc", "status", "latency_ms", "error"])
            writer.writeheader()
            for request_id, result in sorted(http_rows, key=lambda row: row[0]):
                writer.writerow({"request_id": request_id, **result})

        if selected_target is None:
            raise RuntimeError("FIS did not resolve exactly one verified Spot worker target")
        target_node_id = selected_target["instance_id"]
        target_node_name = next((name for name, node in node_by_name.items() if instance_id_from_node(node) == target_node_id), None)
        recovery_snapshots = [item for item in snapshots if item.get("observed_at_utc")]
        baseline_claim_names = {row.get("name") for row in baseline["nodeclaims"]}
        later_claims = [
            claim for snapshot in recovery_snapshots for claim in snapshot.get("nodeclaims", [])
            if claim.get("nodepool") == NODEPOOL and claim.get("name") not in baseline_claim_names
        ]
        claim_names = sorted({claim.get("name") for claim in later_claims if claim.get("name")})
        replacement_claim_name = next((name for name in claim_names if any(row.get("name") == name and row.get("created_at_utc") for row in later_claims)), None)
        replacement_claim_rows = [row for row in later_claims if row.get("name") == replacement_claim_name]
        replacement_claim = replacement_claim_rows[-1] if replacement_claim_rows else None
        replacement_instance_id = next((row.get("instance_id") for row in reversed(replacement_claim_rows) if row.get("instance_id")), None)
        instance_zone_pairs = sorted({
            (row.get("instance_type"), row.get("availability_zone"))
            for snap in recovery_snapshots for row in snap.get("experiment_instances", [])
            if row.get("instance_type") and row.get("availability_zone")
        })
        instance_types = sorted({instance_type for instance_type, _ in instance_zone_pairs})
        capacity_rates: dict[str, Any] = {}
        zone = selected_target.get("availability_zone") or REGION + "a"
        for instance_type, instance_zone in instance_zone_pairs:
            rate_key = f"{instance_type}@{instance_zone}"
            capacity_rates[rate_key] = price_ec2_instance(recorder, instance_type, instance_zone)

        karpenter_detection_events = [
            item for item in events
            if "interrupt" in f"{item.get('reason','')} {item.get('message','')}".lower()
            and "rebalance" not in f"{item.get('reason','')} {item.get('message','')}".lower()
        ]
        karpenter_detection_logs = [
            line for line in logs
            if re.search(r"interrupt", line, re.IGNORECASE)
            and not re.search(r"rebalance", line, re.IGNORECASE)
        ]
        log_detection_times = [timestamp_from_controller_log(line) for line in karpenter_detection_logs]
        log_detection_times = [stamp for stamp in log_detection_times if stamp]
        kubernetes_detection_times = [
            item.get("event_time_utc") or item.get("last_timestamp_utc")
            for item in karpenter_detection_events
            if item.get("event_time_utc") or item.get("last_timestamp_utc")
        ]
        karpenter_detected_at = ordered_timestamp(log_detection_times or kubernetes_detection_times)
        replacement_node_ready = next((node.get("ready_at_utc") for snap in recovery_snapshots for node in snap.get("nodes", []) if node.get("instance_id") == replacement_instance_id and node.get("ready_at_utc")), None)
        replacement_pod_ready = next((pod.get("ready_at_utc") for snap in recovery_snapshots for pod in snap.get("pods", []) if pod.get("node_name") != target_node_name and pod.get("name") not in original_pod_names and pod.get("ready_at_utc")), None)
        replacement_ec2_launch_time = next((instance.get("launch_time_utc") for snap in recovery_snapshots for instance in snap.get("experiment_instances", []) if instance.get("instance_id") == replacement_instance_id), None)
        recovered_ready_counts = [snap.get("ready_replica_count", 0) for snap in recovery_snapshots]
        first_eventbridge_spot = spot_event_time
        stable_http_at = request_metrics.get("three_consecutive_successes_confirmed_after_interruption_at_utc")
        full_recovery_at = ordered_timestamp(
            [stamp for stamp in (replacement_pod_ready, stable_http_at) if stamp], latest=True
        )
        recovery_timeline_seconds = {
            "notice_to_karpenter_detection": elapsed_seconds(first_eventbridge_spot, karpenter_detected_at),
            "notice_to_replacement_nodeclaim": elapsed_seconds(first_eventbridge_spot, replacement_claim.get("created_at_utc") if replacement_claim else None),
            "notice_to_replacement_ec2_launch": elapsed_seconds(first_eventbridge_spot, replacement_ec2_launch_time),
            "notice_to_replacement_node_ready": elapsed_seconds(first_eventbridge_spot, replacement_node_ready),
            "notice_to_replacement_pod_ready": elapsed_seconds(first_eventbridge_spot, replacement_pod_ready),
            "notice_to_http_steady": elapsed_seconds(first_eventbridge_spot, stable_http_at),
            "notice_to_full_recovery": elapsed_seconds(first_eventbridge_spot, full_recovery_at),
        }
        final_instances = [
            item for item in final_snapshot.get("experiment_instances", [])
            if item.get("state") == "running" and item.get("instance_lifecycle") == "spot"
        ]
        final_counts: dict[str, int] = {}
        for instance in final_instances:
            instance_type = instance.get("instance_type")
            if instance_type:
                final_counts[instance_type] = final_counts.get(instance_type, 0) + 1
        spot_hourly_total = 0.0
        od_hourly_total = 0.0
        prices_complete = True
        for instance in final_instances:
            instance_type = instance.get("instance_type")
            instance_zone = instance.get("availability_zone")
            price_info = capacity_rates.get(f"{instance_type}@{instance_zone}", {})
            if price_info.get("spot_usd_per_hour") is None or price_info.get("same_type_on_demand_usd_per_hour") is None:
                prices_complete = False
                continue
            spot_hourly_total += float(price_info["spot_usd_per_hour"])
            od_hourly_total += float(price_info["same_type_on_demand_usd_per_hour"])
        summary = {
            "trial": args.trial,
            "started_at_utc": trial_started,
            "finished_at_utc": utc_now(),
            "account_suffix": ACCOUNT_SUFFIX,
            "region": REGION,
            "availability_zone": zone,
            "availability_zones_observed": sorted({instance_zone for _, instance_zone in instance_zone_pairs}),
            "cluster": CLUSTER,
            "karpenter_version": "1.14.1",
            "workload": {"replicas": 2, "rps": args.rps, "duration_seconds": args.duration, "failure_after_seconds": args.interrupt_after, "spot_notice_to_termination_seconds": 120, "pdb_min_available": 1, "pod_termination_grace_period_seconds": 30, "topology_spread": "hostname, maxSkew=1, minDomains=2, DoNotSchedule"},
            "condition": os.environ.get("PHASE3_WORKLOAD_CONDITION", "baseline"),
            "workload_manifest_path": str(workload_manifest),
            "workload_manifest_sha256": workload_manifest_sha256,
            "pre_stop_sleep_seconds": int(os.environ.get("PHASE3_PRESTOP_SECONDS", "0")),
            "environment_lifecycle": "persistent_across_batch" if args.preserve_environment else "per_trial_cleanup",
            "pre_interruption_ready_replicas": before_ready,
            "min_ready_replicas_observed": min(recovered_ready_counts) if recovered_ready_counts else None,
            "final_ready_replicas": final_snapshot.get("ready_replica_count"),
            "fis_experiment_id": experiment_id,
            "fis_final_status": fis_status,
            "fis_start_api_requested_at_utc": fis_start_requested_at,
            "fis_start_api_response_at_utc": fis_start_response_at,
            "fis_experiment_start_time_utc": fis_experiment_start_time,
            "fis_target_count": 1,
            "fis_target": selected_target,
            "interruption_eventbridge_time_utc": first_eventbridge_spot,
            "rebalance_eventbridge_time_utc": next((item.get("event_time_utc") for item in rebalance_events if item.get("event_time_utc")), None),
            "recovery_timeline_anchor": "target-matched EC2 Spot Instance Interruption Warning event time; Rebalance Recommendation is recorded separately and is not treated as Karpenter drain/recovery start.",
            "unrelated_spot_events_in_audit_queue": len(all_interruption_events) - len(interruption_events),
            "karpenter_detected_at_utc": karpenter_detected_at,
            "karpenter_detection_events": karpenter_detection_events,
            "karpenter_detection_log_lines": karpenter_detection_logs,
            "replacement_nodeclaim": replacement_claim,
            "replacement_ec2_instance_id": replacement_instance_id,
            "replacement_ec2_launch_time_utc": replacement_ec2_launch_time,
            "replacement_node_ready_at_utc": replacement_node_ready,
            "replacement_pod_ready_at_utc": replacement_pod_ready,
            "http_steady_confirmed_at_utc": stable_http_at,
            "full_recovery_confirmed_at_utc": full_recovery_at,
            "recovery_timeline_seconds": recovery_timeline_seconds,
            "original_pod_events": [item for item in events if item.get("regarding", {}).get("name") in original_pod_names],
            "original_pod_drain_timeline": original_pod_drain_timeline(recovery_snapshots, original_pod_names, events),
            "recovery_snapshots": len(recovery_snapshots),
            "http": request_metrics,
            "selected_instance_types_observed": instance_types,
            "capacity_hourly_rates": capacity_rates,
            "recovered_spot_instance_counts": final_counts,
            "recovered_spot_capacity_usd_per_hour": spot_hourly_total if prices_complete else None,
            "same_types_on_demand_capacity_usd_per_hour": od_hourly_total if prices_complete else None,
            "capacity_rate_note": "Provisioned capacity rate only: current Spot rate and same-type Linux Shared On-Demand list rate; not actual billed spend or realized savings.",
            "raw_event_timeline_file": str(raw_path),
            "raw_http_requests_file": str(csv_path),
        }
        summary["interruption_to_http_steady_seconds"] = recovery_timeline_seconds["notice_to_http_steady"]
        summary["interruption_to_full_recovery_seconds"] = recovery_timeline_seconds["notice_to_full_recovery"]
        recorder.write({"kind": "trial_summary", **summary})
        args.output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if fis_status != "completed":
            raise RuntimeError(f"FIS experiment did not complete successfully: {fis_status}")
        if first_eventbridge_spot is None:
            raise RuntimeError("EventBridge audit queue did not record a Spot interruption warning")
        if not replacement_claim or not replacement_node_ready or not replacement_pod_ready:
            raise RuntimeError("replacement NodeClaim, Node Ready, or replacement Pod Ready timeline was incomplete")
        if final_snapshot.get("ready_replica_count") != 2 or stable_http_at is None:
            raise RuntimeError("service did not recover to two Ready replicas and three consecutive successful HTTP requests")
        if not prices_complete:
            recorder.write({"kind": "capacity_pricing_incomplete", "trial": args.trial, "observed_at_utc": utc_now(), "counts": final_counts, "rates": capacity_rates})
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        recorder.write({"kind": "trial_failure", "trial": args.trial, "failed_at_utc": utc_now(), "error": failure, "fis_experiment_id": experiment_id})
        if experiment_id is None and reachability_gate_enabled():
            try:
                _PHASE3_PREFLIGHT.capture_controller_context(recorder, args.trial, "pre-fis-failure-before-cleanup")
            except Exception as diagnostic_error:
                recorder.write({
                    "kind": f"v{REACHABILITY_GATE_VERSION}_controller_context_capture_failed",
                    "trial": args.trial,
                    "observed_at_utc": utc_now(),
                    "error": f"{type(diagnostic_error).__name__}: {diagnostic_error}",
                })
    finally:
        try:
            if args.preserve_environment:
                cleanup_result = {
                    "complete": True,
                    "retained": True,
                    "scope": "workload, Ingress, Spot workers, Karpenter, ALB, and EKS remain for the next fixed attempt",
                }
            else:
                cleanup_result = cleanup_workers(
                    recorder,
                    pre_fis_gate_failed=bool(failure and experiment_id is None and reachability_gate_enabled()),
                )
            recorder.write({"kind": "trial_worker_cleanup", "trial": args.trial, "observed_at_utc": utc_now(), **cleanup_result})
        except Exception as exc:
            cleanup_result = {"complete": False, "errors": [f"{type(exc).__name__}: {exc}"]}
            recorder.write({"kind": "trial_worker_cleanup", "trial": args.trial, "observed_at_utc": utc_now(), **cleanup_result})

    if failure or not cleanup_result or not cleanup_result.get("complete"):
        failure_obj = {
            "trial": args.trial,
            "started_at_utc": trial_started,
            "failed_at_utc": utc_now(),
            "error": failure or "Spot worker cleanup did not reach zero resources",
            "cleanup": cleanup_result,
            "fis_experiment_id": experiment_id,
            "raw_event_timeline_file": str(raw_path),
        }
        args.output.with_name(args.output.stem + ".failure.json").write_text(
            json.dumps(failure_obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(failure_obj, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps({"trial": args.trial, "status": "completed", "raw": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
