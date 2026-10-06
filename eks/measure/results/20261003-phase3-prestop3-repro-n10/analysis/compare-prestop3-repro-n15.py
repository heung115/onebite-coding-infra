#!/usr/bin/env python3
"""Compare the fixed new preStop3 batch with immutable 120-second history."""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import statistics
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
BATCH = OUT.parent
EXECUTION = BATCH / "execution-01"
OLD_BATCH = REPO / "measure/results/20261003-phase3-prestop3-window120"
OLD_EXECUTION = OLD_BATCH / "execution-01"
PREVIOUS_ANALYZER = REPO / "measure/results/20261003-phase3-prestop2-window120/analysis/recalculate-http-window120.py"
SPEC = importlib.util.spec_from_file_location("onebite_window120", PREVIOUS_ANALYZER)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Could not load the existing window analyzer: {PREVIOUS_ANALYZER}")
WINDOW = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WINDOW)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def elapsed(summary: dict[str, Any], key: str) -> float | None:
    value = summary.get("recovery_timeline_seconds", {}).get(key)
    return float(value) if value is not None else None


def range_summary(values: list[float]) -> dict[str, Any]:
    return {
        "median": statistics.median(values) if values else None,
        "min": min(values) if values else None,
        "max": max(values) if values else None,
        "valid_n": len(values),
    }


def normalized_workload(summary: dict[str, Any]) -> dict[str, Any]:
    workload = summary.get("workload", {})
    return {
        "condition": summary.get("condition"),
        "pre_stop_sleep_seconds": summary.get("pre_stop_sleep_seconds"),
        "cluster": summary.get("cluster"),
        "region": summary.get("region"),
        "account_suffix": summary.get("account_suffix"),
        "karpenter_version": summary.get("karpenter_version"),
        "workload": {
            key: workload.get(key)
            for key in (
                "replicas", "rps", "duration_seconds", "failure_after_seconds",
                "spot_notice_to_termination_seconds", "pdb_min_available",
                "pod_termination_grace_period_seconds", "topology_spread",
            )
        },
        "environment_lifecycle": summary.get("environment_lifecycle"),
        "workload_manifest_sha256": summary.get("workload_manifest_sha256"),
    }


def replacement_observation(raw_path: Path, replacement_id: str | None) -> dict[str, Any]:
    if not raw_path.is_file() or not replacement_id:
        return {}
    latest: dict[str, Any] = {}
    with raw_path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "recovery_snapshot":
                continue
            for instance in row.get("experiment_instances", []):
                if instance.get("instance_id") == replacement_id:
                    latest = {
                        "instance_id": replacement_id,
                        "instance_type": instance.get("instance_type"),
                        "availability_zone": instance.get("availability_zone"),
                        "launch_time_utc": instance.get("launch_time_utc"),
                        "state_at_last_observation": instance.get("state"),
                    }
    return latest


def read_batch_attempts(batch_dir: Path, trial_ids: range, batch_label: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[Path]]:
    execution = batch_dir / "execution-01"
    items: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    inputs: set[Path] = set()
    for trial in trial_ids:
        prefix = f"spot-interruption-{trial:02d}"
        summary_path = execution / f"{prefix}.json"
        failure_path = execution / f"{prefix}.failure.json"
        csv_path = execution / f"{prefix}.csv"
        raw_path = execution / f"{prefix}.jsonl"
        gate_path = execution / f"pre-attempt-steady-state-{trial:02d}.jsonl"
        queue_path = execution / f"pre-attempt-fis-queue-{trial:02d}.json"
        summary = load(summary_path, {})
        failure = load(failure_path, {})
        has_attempt_record = any(path.is_file() for path in (summary_path, failure_path, csv_path, raw_path, gate_path, queue_path))
        if not has_attempt_record:
            continue
        for path in (summary_path, failure_path, csv_path, raw_path, gate_path, queue_path):
            if path.is_file():
                inputs.add(path)
        warning = summary.get("interruption_eventbridge_time_utc")
        window_row = None
        if warning and csv_path.is_file():
            item = {
                "condition": "preStop 3s",
                "batch": batch_label,
                "trial": trial,
                "warning_at_utc": warning,
                "csv_path": csv_path,
                "warning_source_path": summary_path,
                "fis_final_status": summary.get("fis_final_status"),
            }
            window_row = WINDOW.analyze(item)
            window_row["fis_final_status"] = summary.get("fis_final_status")
            window_row["interruption_measurement_valid"] = (
                window_row.get("window_fully_covered_by_raw_csv") is True
                and summary.get("fis_final_status") == "completed"
            )
            items.append(item)
        raw_replacement = replacement_observation(raw_path, summary.get("replacement_ec2_instance_id"))
        target = summary.get("fis_target") or {}
        complete = bool(window_row and window_row.get("interruption_measurement_valid"))
        attempts.append({
            "attempt": trial,
            "batch": batch_label,
            "status": "valid_interruption_window" if complete else (
                "interruption_window_incomplete" if warning else "failed_before_or_during_FIS"
            ),
            "fis_started": bool(summary.get("fis_experiment_id") or failure.get("fis_experiment_id")),
            "fis_experiment_id": summary.get("fis_experiment_id") or failure.get("fis_experiment_id"),
            "fis_final_status": summary.get("fis_final_status"),
            "warning_at_utc": warning,
            "http_window_requests": window_row.get("window_requests") if window_row else None,
            "http_window_failures": window_row.get("window_failed_requests") if window_row else None,
            "http_window_failure_rate_percent": window_row.get("window_failure_rate_percent") if window_row else None,
            "failure_observed": window_row.get("failure_observed") if window_row else None,
            "maximum_contiguous_failure_seconds": window_row.get("max_contiguous_failure_window_seconds") if window_row else None,
            "warning_to_last_failure_completion_seconds": window_row.get("warning_to_last_failed_response_completion_seconds") if window_row else None,
            "warning_to_node_ready_seconds": elapsed(summary, "notice_to_replacement_node_ready"),
            "warning_to_replacement_pod_ready_seconds": elapsed(summary, "notice_to_replacement_pod_ready"),
            "warning_to_ready_replica_2_seconds": elapsed(summary, "notice_to_full_recovery"),
            "interrupted_instance_type": target.get("instance_type"),
            "interrupted_az": target.get("availability_zone"),
            "replacement_instance": raw_replacement,
            "failure_reason": failure.get("error") or failure.get("failure_reason") or failure.get("error_message"),
            "workload_signature": normalized_workload(summary) if summary else None,
            "summary_path": str(summary_path.relative_to(REPO)) if summary_path.is_file() else None,
            "failure_path": str(failure_path.relative_to(REPO)) if failure_path.is_file() else None,
            "raw_path": str(raw_path.relative_to(REPO)) if raw_path.is_file() else None,
            "gate_path": str(gate_path.relative_to(REPO)) if gate_path.is_file() else None,
            "queue_preflight_path": str(queue_path.relative_to(REPO)) if queue_path.is_file() else None,
        })
    return items, attempts, sorted(inputs)


def main() -> None:
    historical_items, historical_paths, historic_unwindowed = WINDOW.source_rows()
    previous_items, previous_attempts, previous_paths = read_batch_attempts(
        OLD_BATCH, range(1, 6), "20261003-phase3-prestop3-window120"
    )
    new_items, new_attempts, new_paths = read_batch_attempts(
        BATCH, range(1, 11), "20261003-phase3-prestop3-repro-n10"
    )
    prereg = load(BATCH / "preregistered-rules.json", {})
    previous_prereg = load(OLD_BATCH / "preregistered-rules.json", {})
    previous_manifest = OLD_BATCH / "config/spot-recovery-app-prestop3.yaml"
    new_manifest = BATCH / "config/spot-recovery-app-prestop3.yaml"
    previous_summaries = [load(OLD_EXECUTION / f"spot-interruption-{trial:02d}.json", {}) for trial in range(1, 6)]
    new_summaries = [load(EXECUTION / f"spot-interruption-{trial:02d}.json", {}) for trial in range(1, 11)]

    old_signature_set = {json.dumps(normalized_workload(x), sort_keys=True) for x in previous_summaries if x}
    new_signature_set = {json.dumps(normalized_workload(x), sort_keys=True) for x in new_summaries if x}
    manifests_match = previous_manifest.is_file() and new_manifest.is_file() and sha256(previous_manifest) == sha256(new_manifest)
    prereg_constants_match = (
        previous_prereg.get("held_constant", {}).get("http_rate_requests_per_second") == 10
        and previous_prereg.get("held_constant", {}).get("raw_capture_margin") == "same collector settings: 125-second capture, FIS start request 18 seconds after load start"
        and previous_prereg.get("held_constant", {}).get("http_analysis_window") == "[Spot Warning - 30 seconds, Spot Warning + 90 seconds)"
        and previous_prereg.get("held_constant", {}).get("fis_action") == "aws:ec2:send-spot-instance-interruptions"
        and previous_prereg.get("held_constant", {}).get("fis_duration_before_interruption") == "PT2M"
        and previous_prereg.get("held_constant", {}).get("termination_grace_period_seconds") == 30
        and prereg.get("fixed_workload_and_environment", {}).get("raw_capture_seconds") == 125
        and prereg.get("fixed_workload_and_environment", {}).get("fis_start_after_load_seconds") == 18
        and prereg.get("fixed_workload_and_environment", {}).get("fis_duration_before_interruption") == "PT2M"
        and prereg.get("fixed_workload_and_environment", {}).get("http_load") == "10 requests/second"
        and prereg.get("fixed_workload_and_environment", {}).get("analysis_window", {}).get("start") == "warning - 30 seconds, inclusive"
        and prereg.get("fixed_workload_and_environment", {}).get("analysis_window", {}).get("end") == "warning + 90 seconds, exclusive"
        and prereg.get("fixed_workload_and_environment", {}).get("analysis_window", {}).get("request_inclusion") == "started_at_utc in [window_start, window_end)"
    )
    signature_match = bool(old_signature_set) and bool(new_signature_set) and new_signature_set == old_signature_set
    equivalence = {
        "manifests_byte_identical": manifests_match,
        "preregistered_load_and_window_inputs_match": prereg_constants_match,
        "available_trial_workload_signatures_match": signature_match,
        "previous_batch_manifest_sha256": sha256(previous_manifest) if previous_manifest.is_file() else None,
        "new_batch_manifest_sha256": sha256(new_manifest) if new_manifest.is_file() else None,
        "previous_workload_signature_variants": len(old_signature_set),
        "new_workload_signature_variants": len(new_signature_set),
        "pooled_condition_allowed": manifests_match and prereg_constants_match and signature_match,
        "limits": "This verifies saved workload/run metadata and the identical manifest. Persistent cluster identity, Karpenter 1.14.1, active addon/CNI versions, and FIS template are captured in preflight snapshots; no infrastructure changes are performed by this batch.",
    }

    previous_rows = [WINDOW.analyze(item) for item in previous_items]
    new_rows = [WINDOW.analyze(item) for item in new_items]
    for row, item in zip(previous_rows, previous_items):
        row["fis_final_status"] = item.get("fis_final_status")
        row["interruption_measurement_valid"] = row.get("window_fully_covered_by_raw_csv") is True and item.get("fis_final_status") == "completed"
    for row, item in zip(new_rows, new_items):
        row["fis_final_status"] = item.get("fis_final_status")
        row["interruption_measurement_valid"] = row.get("window_fully_covered_by_raw_csv") is True and item.get("fis_final_status") == "completed"
    old_complete_n = sum(bool(row.get("interruption_measurement_valid")) for row in previous_rows)
    new_complete_n = sum(bool(row.get("interruption_measurement_valid")) for row in new_rows)
    all_rows = [WINDOW.analyze(item) for item in historical_items]
    if equivalence["pooled_condition_allowed"]:
        for row in previous_rows + new_rows:
            row["condition"] = "preStop 3s"
        all_rows.extend(previous_rows)
        all_rows.extend(new_rows)
        prestop3_group_names = ["preStop 3s"]
    else:
        for row in previous_rows:
            row["condition"] = "preStop 3s previous batch"
        for row in new_rows:
            row["condition"] = "preStop 3s additional batch"
        all_rows.extend(previous_rows)
        all_rows.extend(new_rows)
        prestop3_group_names = ["preStop 3s previous batch", "preStop 3s additional batch"]

    names = ["no-preStop baseline", "preStop 2s", *prestop3_group_names, "preStop 5s"]
    conditions: dict[str, dict[str, Any]] = {}
    for name in names:
        rows = [row for row in all_rows if row["condition"] == name]
        complete = [row for row in rows if row.get("interruption_measurement_valid", row["window_fully_covered_by_raw_csv"])]
        conditions[name] = WINDOW.summarize(complete)
        conditions[name]["attempts_with_warning_and_csv"] = len(rows)
        conditions[name]["incomplete_windows_excluded"] = len(rows) - len(complete)

    new_valid_rows = [row for row in new_rows if row.get("interruption_measurement_valid")]
    new_failure_rows = [row for row in new_valid_rows if row["failure_observed"]]
    all_ten_valid = len(new_valid_rows) == 10 and len(new_attempts) == 10
    decision = {
        "fixed_attempts_recorded": len(new_attempts),
        "new_complete_interruption_windows": new_complete_n,
        "previous_complete_interruption_windows": old_complete_n,
        "combined_preStop3_complete_n": old_complete_n + new_complete_n if equivalence["pooled_condition_allowed"] else None,
        "all_ten_new_attempts_are_complete_windows": all_ten_valid,
        "new_valid_windows_with_http_failure": len(new_failure_rows),
        "classification": (
            "preStop3_total_n15_no_failure_observed_under_current_conditions"
            if equivalence["pooled_condition_allowed"] and all_ten_valid and not new_failure_rows
            else "preStop3_removed_from_minimum_sufficient_candidate__propose_preStop4"
            if new_failure_rows
            else "not_enough_complete_new_windows_for_n15_confirmation"
        ),
        "automatic_follow_up": False,
        "wording_limit": "Do not describe any zero-failure result as a general no-downtime guarantee.",
    }

    recovery = {
        "warning_to_node_ready_seconds": [],
        "warning_to_replacement_pod_ready_seconds": [],
        "warning_to_ready_replica_2_seconds": [],
    }
    for attempt in previous_attempts + new_attempts:
        if attempt["status"] != "valid_interruption_window":
            continue
        for source, key in (
            ("warning_to_node_ready_seconds", "warning_to_node_ready_seconds"),
            ("warning_to_replacement_pod_ready_seconds", "warning_to_replacement_pod_ready_seconds"),
            ("warning_to_ready_replica_2_seconds", "warning_to_ready_replica_2_seconds"),
        ):
            value = attempt.get(source)
            if value is not None:
                recovery[key].append(float(value))

    inputs = set(historical_paths + previous_paths + new_paths)
    for path in (previous_manifest, new_manifest, OLD_BATCH / "preregistered-rules.json", BATCH / "preregistered-rules.json", BATCH / "run-order.json"):
        if path.is_file():
            inputs.add(path)
    output = {
        "batch_id": "20261003-phase3-prestop3-repro-n10",
        "window_definition": {
            "anchor": "target-matched EC2 Spot Interruption Warning EventBridge eventTime",
            "start_inclusive": "warning - 30 seconds",
            "end_exclusive": "warning + 90 seconds",
            "duration_seconds": 120,
            "request_counting_rule": "Include request rows with started_at_utc in [window_start, window_end).",
            "failure_rule": "A status not starting with 2, including empty status, is a failure.",
        },
        "condition_equivalence": equivalence,
        "conditions": conditions,
        "previous_preStop3_attempts": previous_attempts,
        "new_fixed_attempts": new_attempts,
        "pooled_preStop3_recovery_timeline_seconds": {key: range_summary(values) for key, values in recovery.items()} if equivalence["pooled_condition_allowed"] else None,
        "historical_unwindowed_attempts": historic_unwindowed,
        "decision": decision,
        "source_sha256": [
            {"path": str(path.relative_to(REPO)), "sha256": sha256(path)}
            for path in sorted(inputs) if path.is_file()
        ],
    }
    (OUT / "prestop3-repro-n15-comparison.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = [
        "condition", "source_batch", "trial", "warning_at_utc", "window_start_utc", "window_end_utc",
        "window_requests", "window_failed_requests", "window_failure_rate_percent", "failure_observed",
        "max_contiguous_failure_window_seconds", "warning_to_last_failed_response_completion_seconds",
        "window_fully_covered_by_raw_csv", "raw_csv_path",
    ]
    with (OUT / "prestop3-repro-n15-comparison.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: row.get(key) for key in fields} for row in all_rows)
    if len(new_attempts) > 10 or len({row["attempt"] for row in new_attempts}) != len(new_attempts):
        raise SystemExit("New fixed-attempt accounting contains duplicate IDs or more than ten IDs")
    print(json.dumps({"condition_equivalence": equivalence, "conditions": conditions, "decision": decision}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
