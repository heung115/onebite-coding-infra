#!/usr/bin/env python3
"""Run one fixed-rate ALB trial and terminate one tagged experiment-pool EC2 node."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


KUBECONFIG = Path.home() / ".kube" / "onebite-eks.kubeconfig"
KUBE_CONTEXT = "onebite-eks-measure"
REGION = "ap-northeast-2"
CLUSTER = "onebite-eks-measure"
NODEGROUP = f"{CLUSTER}-experiment"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def verify_caller() -> None:
    account = subprocess.run(
        ["aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"],
        capture_output=True,
        text=True,
        check=False,
    )
    configured_region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not configured_region:
        configured_region = subprocess.run(
            ["aws", "configure", "get", "region"], capture_output=True, text=True, check=False
        ).stdout.strip()
    if account.returncode or account.stdout.strip()[-4:] != "0497" or configured_region != REGION:
        raise RuntimeError("AWS target must be account ****0497 in ap-northeast-2")


class Recorder:
    def __init__(self, raw_path: Path) -> None:
        self.raw_path = raw_path
        self.raw_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def write(self, row: dict[str, Any]) -> None:
        with self.lock, self.raw_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")

    def kubectl(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["KUBECONFIG"] = str(KUBECONFIG)
        argv = ["kubectl", "--context", KUBE_CONTEXT, *args]
        started = utc_now()
        result = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
        self.write(
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
        self.write(
            {
                "kind": "aws_command",
                "started_at_utc": started,
                "finished_at_utc": utc_now(),
                "argv": argv,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        return result


def fetch(url: str, timeout: float) -> dict[str, Any]:
    started_at = utc_now()
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(4096)
            status: int | str = response.status
            error = ""
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = f"HTTPError: {exc.reason}"
    except Exception as exc:  # Keep timeouts and network errors in the raw data.
        status = ""
        error = f"{type(exc).__name__}: {exc}"
    return {
        "started_at_utc": started_at,
        "status": status,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "error": error,
    }


def get_pods_and_nodes(recorder: Recorder) -> tuple[dict[str, Any], dict[str, Any]]:
    pods = recorder.kubectl("get", "pods", "-n", "measure", "-l", "app=backend-probe", "-o", "json")
    nodes = recorder.kubectl("get", "nodes", "-l", "measure-pool=experiment", "-o", "json")
    if pods.returncode or nodes.returncode:
        raise RuntimeError("could not inspect the experiment workload or nodes")
    return json.loads(pods.stdout), json.loads(nodes.stdout)


def choose_node(pods: dict[str, Any], nodes: dict[str, Any]) -> tuple[str, str, str]:
    pod_nodes = {
        pod.get("spec", {}).get("nodeName")
        for pod in pods.get("items", [])
        if pod.get("spec", {}).get("nodeName")
    }
    all_candidates = []
    for node in nodes.get("items", []):
        metadata = node.get("metadata", {})
        labels = metadata.get("labels", {})
        name = metadata.get("name", "")
        provider_id = node.get("spec", {}).get("providerID", "")
        instance_id = provider_id.rsplit("/", 1)[-1]
        zone = labels.get("topology.kubernetes.io/zone", "")
        if instance_id.startswith("i-") and zone:
            all_candidates.append((name, instance_id, zone))
    if len({row[2] for row in all_candidates}) < 2:
        raise RuntimeError("two experiment-pool nodes across separate AZs are required")
    app_candidates = [row for row in all_candidates if row[0] in pod_nodes]
    if not app_candidates:
        raise RuntimeError("no application Pod is running on an experiment-pool node")
    return sorted(app_candidates)[0]


def verify_instance_tags(recorder: Recorder, instance_id: str) -> dict[str, Any]:
    result = recorder.aws(
        "ec2",
        "describe-instances",
        "--region",
        REGION,
        "--instance-ids",
        instance_id,
        "--output",
        "json",
    )
    if result.returncode:
        raise RuntimeError("AWS could not confirm the selected worker instance")
    payload = json.loads(result.stdout)
    matches = [instance for reservation in payload.get("Reservations", []) for instance in reservation.get("Instances", [])]
    if len(matches) != 1:
        raise RuntimeError("AWS returned an unexpected instance count")
    instance = matches[0]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    expected = {"Project": "onebite", "Temporary": "true"}
    if any(tags.get(key) != value for key, value in expected.items()):
        raise RuntimeError("selected instance did not match the temporary experiment node group tags")
    if instance.get("State", {}).get("Name") != "running":
        raise RuntimeError("selected experiment node is not running")

    group_result = recorder.aws(
        "eks",
        "describe-nodegroup",
        "--region",
        REGION,
        "--cluster-name",
        CLUSTER,
        "--nodegroup-name",
        NODEGROUP,
        "--query",
        "nodegroup.resources.autoScalingGroups[].name",
        "--output",
        "json",
    )
    membership_result = recorder.aws(
        "autoscaling",
        "describe-auto-scaling-instances",
        "--region",
        REGION,
        "--instance-ids",
        instance_id,
        "--query",
        "AutoScalingInstances[].AutoScalingGroupName",
        "--output",
        "json",
    )
    if group_result.returncode or membership_result.returncode:
        raise RuntimeError("AWS could not confirm managed node-group membership")
    experiment_groups = set(json.loads(group_result.stdout))
    instance_groups = set(json.loads(membership_result.stdout))
    if not experiment_groups.intersection(instance_groups):
        raise RuntimeError("selected EC2 instance is not in the experiment managed node group")
    return {
        "instance_id": instance_id,
        "instance_type": instance.get("InstanceType"),
        "availability_zone": instance.get("Placement", {}).get("AvailabilityZone"),
        "state": instance.get("State", {}).get("Name"),
        "verified_tags": expected,
        "verified_nodegroup": NODEGROUP,
        "autoscaling_group": sorted(experiment_groups.intersection(instance_groups))[0],
    }


def patch_spread(recorder: Recorder, condition: str) -> None:
    if condition == "baseline":
        result = recorder.kubectl("get", "deployment", "backend-probe", "-n", "measure", "-o", "json")
        if result.returncode:
            raise RuntimeError("backend-probe deployment is missing")
        spec = json.loads(result.stdout).get("spec", {}).get("template", {}).get("spec", {})
        if spec.get("topologySpreadConstraints"):
            raise RuntimeError("baseline contains a topology spread constraint")
        return

    patch = {
        "spec": {
            "template": {
                "spec": {
                    "topologySpreadConstraints": [
                        {
                            "maxSkew": 1,
                            "minDomains": 2,
                            "topologyKey": "topology.kubernetes.io/zone",
                            "whenUnsatisfiable": "DoNotSchedule",
                            "matchLabelKeys": ["pod-template-hash"],
                            "labelSelector": {"matchLabels": {"app": "backend-probe"}},
                        }
                    ]
                }
            }
        }
    }
    result = recorder.kubectl(
        "patch",
        "deployment",
        "backend-probe",
        "-n",
        "measure",
        "--type=merge",
        "-p",
        json.dumps(patch, separators=(",", ":")),
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not set topology spread condition")


def wait_for_rollout(recorder: Recorder) -> None:
    result = recorder.kubectl(
        "rollout",
        "status",
        "deployment/backend-probe",
        "-n",
        "measure",
        "--timeout=5m",
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "backend-probe rollout did not complete")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--condition", choices=["baseline", "spread"], required=True)
    parser.add_argument("--trial", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute-node-failure", action="store_true", help="Required before terminating the tagged app node.")
    parser.add_argument("--rps", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=180.0)
    parser.add_argument("--failure-after", type=float, default=30.0)
    parser.add_argument("--request-timeout", type=float, default=5.0)
    args = parser.parse_args()

    if not args.execute_node_failure:
        parser.error("pass --execute-node-failure only after the user has approved the experiment")
    if not KUBECONFIG.is_file():
        parser.error(f"Dedicated kubeconfig is missing: {KUBECONFIG}")
    if args.output.exists() or args.output.with_suffix(".jsonl").exists():
        parser.error("Refusing to overwrite previous trial results")

    raw_path = args.output.with_suffix(".jsonl")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    recorder = Recorder(raw_path)
    start_wall = utc_now()
    try:
        verify_caller()
        patch_spread(recorder, args.condition)
        restarted = recorder.kubectl("rollout", "restart", "deployment/backend-probe", "-n", "measure")
        if restarted.returncode:
            raise RuntimeError(restarted.stderr.strip() or "could not reset backend-probe Pods")
        wait_for_rollout(recorder)
        pods, nodes = get_pods_and_nodes(recorder)
        ready_pods = [
            pod
            for pod in pods.get("items", [])
            if any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ]
        if len(ready_pods) != 2:
            raise RuntimeError("exactly two Ready backend-probe Pods are required before a trial")
        if args.condition == "spread":
            node_zones = {
                item.get("metadata", {}).get("name"): item.get("metadata", {}).get("labels", {}).get("topology.kubernetes.io/zone")
                for item in nodes.get("items", [])
            }
            pod_zones = {
                node_zones.get(item.get("spec", {}).get("nodeName"))
                for item in ready_pods
            }
            if None in pod_zones or len(pod_zones) != 2:
                raise RuntimeError("spread condition requires the two Ready Pods to occupy separate AZs")
        instance_name, instance_id, zone = choose_node(pods, nodes)
        verified = verify_instance_tags(recorder, instance_id)
        recorder.write({"kind": "preflight", "instance": verified, "node_name": instance_name, "zone": zone})

        interval = 1.0 / args.rps
        start_mono = time.monotonic()
        stop_at = start_mono + args.duration
        next_at = start_mono
        next_snapshot_at = start_mono
        failure_done = False
        failure_at: str | None = None
        futures: list[tuple[int, concurrent.futures.Future[dict[str, Any]]]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
            request_id = 0
            while time.monotonic() < stop_at:
                now = time.monotonic()
                if now - start_mono >= args.failure_after and not failure_done:
                    terminate = recorder.aws(
                        "ec2",
                        "terminate-instances",
                        "--region",
                        REGION,
                        "--instance-ids",
                        instance_id,
                        "--output",
                        "json",
                    )
                    if terminate.returncode:
                        raise RuntimeError("AWS rejected termination of the verified experiment node")
                    failure_at = utc_now()
                    recorder.write(
                        {
                            "kind": "node_failure_requested",
                            "requested_at_utc": failure_at,
                            "instance_id": instance_id,
                            "node_name": instance_name,
                            "zone": zone,
                            "aws_response": json.loads(terminate.stdout),
                        }
                    )
                    failure_done = True

                if now >= next_at:
                    futures.append((request_id, pool.submit(fetch, args.url, args.request_timeout)))
                    request_id += 1
                    next_at += interval
                else:
                    time.sleep(min(0.01, next_at - now))

                # Save node and Pod state alongside each request window.
                if now >= next_snapshot_at:
                    try:
                        pod_state, node_state = get_pods_and_nodes(recorder)
                        recorder.write(
                            {
                                "kind": "availability_snapshot",
                                "observed_at_utc": utc_now(),
                                "pods": [
                                    {
                                        "name": item.get("metadata", {}).get("name"),
                                        "node": item.get("spec", {}).get("nodeName"),
                                        "phase": item.get("status", {}).get("phase"),
                                        "ready": any(
                                            condition.get("type") == "Ready" and condition.get("status") == "True"
                                            for condition in item.get("status", {}).get("conditions", [])
                                        ),
                                    }
                                    for item in pod_state.get("items", [])
                                ],
                                "experiment_nodes": [
                                    {
                                        "name": item.get("metadata", {}).get("name"),
                                        "zone": item.get("metadata", {}).get("labels", {}).get("topology.kubernetes.io/zone"),
                                        "ready": any(
                                            condition.get("type") == "Ready" and condition.get("status") == "True"
                                            for condition in item.get("status", {}).get("conditions", [])
                                        ),
                                    }
                                    for item in node_state.get("items", [])
                                ],
                            }
                        )
                    except Exception as exc:
                        recorder.write({"kind": "snapshot_error", "at_utc": utc_now(), "error": str(exc)})
                    next_snapshot_at += 1.0

        rows = [(request_id, future.result()) for request_id, future in futures]
        rows.sort(key=lambda row: row[0])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("w", encoding="utf-8", newline="") as stream:
            fields = ["request_id", "started_at_utc", "status", "latency_ms", "error"]
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for request_id, row in rows:
                writer.writerow({"request_id": request_id, **row})

        failures = sum(not str(row["status"]).startswith("2") for _, row in rows)
        summary = {
            "condition": args.condition,
            "trial": args.trial,
            "started_at_utc": start_wall,
            "finished_at_utc": utc_now(),
            "duration_seconds": args.duration,
            "request_rate_per_second": args.rps,
            "requests": len(rows),
            "http_or_network_errors": failures,
            "node_failure_requested_at_utc": failure_at,
            "terminated_node": {**verified, "kubernetes_node": instance_name, "zone": zone},
            "pod_disruption_budget": "minAvailable=1; does not prevent involuntary EC2 termination",
            "raw_requests_file": str(args.output),
            "raw_state_file": str(raw_path),
        }
        args.output.with_name(args.output.stem + ".summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        failure = {
            "condition": args.condition,
            "trial": args.trial,
            "started_at_utc": start_wall,
            "failed_at_utc": utc_now(),
            "error": f"{type(exc).__name__}: {exc}",
            "raw_state_file": str(raw_path),
        }
        args.output.with_name(args.output.stem + ".failure.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(failure, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
