#!/usr/bin/env python3
"""Compare the isolated preStop=5s batch with the immutable Phase 3 baseline."""

from __future__ import annotations

import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[2]
RESULTS = ROOT / "measure" / "results"
BASELINE = RESULTS / "20261002-phase3-v6-baseline"
TUNED = RESULTS / "20261003-phase3-prestop5"
BASELINE_METRICS = BASELINE / "analysis" / "cross-batch-baseline-metrics.json"


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    if not path.exists():
        return
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def find_trial_summary(path: Path) -> dict[str, Any]:
    return next((row for row in iter_jsonl(path) if row.get("kind") == "trial_summary"), {})


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def delta_seconds(start: Any, end: Any) -> float | None:
    first, last = parse_time(start), parse_time(end)
    return round((last - first).total_seconds(), 6) if first and last else None


def metrics(values: list[float]) -> dict[str, Any]:
    clean = [float(value) for value in values if value is not None]
    return {
        "median": round(statistics.median(clean), 6) if clean else None,
        "min": round(min(clean), 6) if clean else None,
        "max": round(max(clean), 6) if clean else None,
        "valid_n": len(clean),
    }


def targeted_original_pod(baseline: dict[str, Any], target_instance_id: str | None) -> str | None:
    node_name = next(
        (node.get("name") for node in baseline.get("nodes", []) if node.get("instance_id") == target_instance_id),
        None,
    )
    return next(
        (pod.get("name") for pod in baseline.get("pods", []) if pod.get("node_name") == node_name),
        None,
    )


def drain_observation(
    raw_path: Path, summary: dict[str, Any], warning_at: str | None
) -> dict[str, Any]:
    target_id = (summary.get("fis_target") or {}).get("instance_id")
    baseline: dict[str, Any] = {}
    for row in iter_jsonl(raw_path):
        if row.get("kind") == "pretrial_baseline":
            baseline = row
        if not target_id and row.get("kind") == "fis_target_resolved":
            target_id = (row.get("selected_instance") or {}).get("instance_id")
        if not target_id and row.get("kind") == "eventbridge_event":
            if row.get("detail_type") == "EC2 Spot Instance Interruption Warning":
                if not warning_at or row.get("event_time_utc") == warning_at:
                    detail = row.get("detail", {})
                    target_id = detail.get("instance-id") or detail.get("instanceId")
    pod_name = targeted_original_pod(baseline, target_id)
    if not pod_name:
        return {"target_pod": None}

    killing_events: dict[tuple[Any, ...], dict[str, Any]] = {}
    deletion_times: list[str] = []
    finish_times: list[str] = []
    pod_absent_at = None
    snapshot_rows: list[dict[str, Any]] = []
    for row in iter_jsonl(raw_path):
        if row.get("kind") == "kubernetes_events":
            for event in row.get("events", []):
                ref = event.get("regarding", event.get("involvedObject", {}))
                if ref.get("name") != pod_name or event.get("reason") != "Killing":
                    continue
                key = (ref.get("name"), event.get("event_time_utc"), event.get("message"))
                killing_events[key] = event
        elif row.get("kind") == "recovery_snapshot":
            pod_rows = [pod for pod in row.get("pods", []) if pod.get("name") == pod_name]
            for pod in pod_rows:
                if pod.get("deletion_timestamp_utc"):
                    deletion_times.append(pod["deletion_timestamp_utc"])
                finish_times.extend(x for x in pod.get("container_terminated_at_utc", []) if x)
            snapshot_rows.append({
                "observed_at_utc": row.get("observed_at_utc"),
                "target_pod_present": bool(pod_rows),
            })

    killing_at = min(
        (event.get("event_time_utc") for event in killing_events.values() if parse_time(event.get("event_time_utc"))),
        key=lambda value: parse_time(value),
        default=None,
    )
    previously_present = False
    for snapshot in sorted(
        snapshot_rows,
        key=lambda row: parse_time(row.get("observed_at_utc")).timestamp()
        if parse_time(row.get("observed_at_utc")) else float("-inf"),
    ):
        observed = parse_time(snapshot.get("observed_at_utc"))
        if not killing_at or not observed or observed < parse_time(killing_at):
            continue
        if previously_present and not snapshot["target_pod_present"]:
            pod_absent_at = snapshot.get("observed_at_utc")
            break
        if snapshot["target_pod_present"]:
            previously_present = True
    finished_at = min(
        (value for value in finish_times if parse_time(value) and (not killing_at or parse_time(value) >= parse_time(killing_at))),
        key=lambda value: parse_time(value),
        default=None,
    )
    deletion_at = min(deletion_times, key=lambda value: parse_time(value), default=None)
    return {
        "target_pod": pod_name,
        "killing_event_at_utc": killing_at,
        "container_terminated_at_utc": finished_at,
        "deletion_timestamp_utc": deletion_at,
        "killing_to_container_terminated_seconds": delta_seconds(killing_at, finished_at),
        # deletionTimestamp is the grace-period deadline, not the termination start.
        # Comparing it with finishedAt produces a negative, misleading "duration".
        "deletion_to_container_terminated_seconds": None,
        "killing_to_pod_absence_observed_seconds": delta_seconds(killing_at, pod_absent_at),
        "pod_absence_first_observed_at_utc": pod_absent_at,
        "warning_to_killing_seconds": delta_seconds(warning_at or summary.get("interruption_eventbridge_time_utc"), killing_at),
        "deletion_timestamp_semantics": "Kubernetes deletion deadline; it can be terminationGracePeriodSeconds after termination starts.",
        "killing_event_count": len(killing_events),
    }


def strict_http_metrics(csv_path: Path, warning_at: str | None) -> dict[str, Any]:
    if not csv_path.exists():
        return {"total_requests": 0, "failed_requests": 0, "failure_rate_percent": None}
    with csv_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows.sort(key=lambda row: int(row.get("request_id") or 0))
    failed = [not str(row.get("status", "")).startswith("2") for row in rows]
    failed_count = sum(failed)
    failure_windows: list[float] = []
    index = 0
    while index < len(rows):
        if not failed[index]:
            index += 1
            continue
        first = index
        while index < len(rows) and failed[index]:
            index += 1
        start = parse_time(rows[first].get("started_at_utc"))
        if index < len(rows):
            end = parse_time(rows[index].get("finished_at_utc") or rows[index].get("started_at_utc"))
        else:
            end = parse_time(rows[index - 1].get("finished_at_utc"))
        if start and end:
            failure_windows.append(max(0.0, (end - start).total_seconds()))

    last_failed_finish = max(
        (parse_time(row.get("finished_at_utc")) for row, is_failed in zip(rows, failed) if is_failed and parse_time(row.get("finished_at_utc"))),
        default=None,
    )
    warning_dt = parse_time(warning_at)
    streak = 0
    after_last_failure_third_2xx = None
    if last_failed_finish:
        for row, is_failed in zip(rows, failed):
            started = parse_time(row.get("started_at_utc"))
            if not started or started < last_failed_finish:
                continue
            if warning_dt and started < warning_dt:
                continue
            streak = 0 if is_failed else streak + 1
            if streak == 3:
                after_last_failure_third_2xx = row.get("finished_at_utc")
                break
    return {
        "total_requests": len(rows),
        "failed_requests": failed_count,
        "failure_rate_percent": 100.0 * failed_count / len(rows) if rows else None,
        "max_contiguous_failure_window_seconds": max(failure_windows) if failure_windows else 0.0,
        "last_failed_response_finished_at_utc": last_failed_finish.isoformat() if last_failed_finish else None,
        "third_2xx_after_last_failure_finished_at_utc": after_last_failure_third_2xx,
        "warning_to_third_2xx_after_last_failure_seconds": delta_seconds(warning_at, after_last_failure_third_2xx),
    }


def baseline_runs() -> list[dict[str, Any]]:
    aggregate = read_json(BASELINE_METRICS)
    result = []
    for episode in aggregate.get("runs", []):
        batch = episode["batch"]
        trial = int(episode["trial"])
        batch_dir = RESULTS / batch / "execution-01"
        raw_path = batch_dir / f"spot-interruption-{trial:02d}.jsonl"
        summary = find_trial_summary(raw_path)
        http = episode.get("http", {})
        result.append({
            "batch": batch,
            "trial": trial,
            "valid_interruption": True,
            "warning_at_utc": episode.get("warning_at_utc"),
            "http": {
                "total_requests": http.get("total_requests"),
                "failed_requests": http.get("failed_requests"),
                "failure_rate_percent": http.get("failure_rate_percent"),
                "max_contiguous_failure_window_seconds": http.get("max_contiguous_failure_window_seconds"),
                "warning_to_third_2xx_after_last_failure_seconds": http.get("warning_to_third_2xx_after_last_failure_seconds"),
            },
            "timeline": {
                "warning_to_replacement_pod_ready_seconds": episode.get("warning_to_replacement_pod_ready_seconds"),
                "warning_to_ready_replicas_2_seconds": episode.get("warning_to_ready_replicas_2_seconds"),
                **drain_observation(raw_path, summary, episode.get("warning_at_utc")),
            },
        })
    return result


def tuned_runs() -> list[dict[str, Any]]:
    run_dir = TUNED / "execution-01"
    result = []
    for trial in range(1, 6):
        summary_path = run_dir / f"spot-interruption-{trial:02d}.json"
        if not summary_path.exists():
            failure_path = run_dir / f"spot-interruption-{trial:02d}.failure.json"
            result.append({
                "batch": "20261003-phase3-prestop5",
                "trial": trial,
                "valid_interruption": False,
                "failure": read_json(failure_path) if failure_path.exists() else "not attempted or result unavailable",
            })
            continue
        summary = read_json(summary_path)
        warning_at = summary.get("interruption_eventbridge_time_utc")
        http = strict_http_metrics(run_dir / f"spot-interruption-{trial:02d}.csv", warning_at)
        raw_path = run_dir / f"spot-interruption-{trial:02d}.jsonl"
        drain = drain_observation(raw_path, summary, warning_at)
        result.append({
            "batch": "20261003-phase3-prestop5",
            "trial": trial,
            "valid_interruption": summary.get("fis_final_status") == "completed" and bool(warning_at),
            "warning_at_utc": warning_at,
            "condition": summary.get("condition"),
            "pre_stop_sleep_seconds": summary.get("pre_stop_sleep_seconds"),
            "http": http,
            "timeline": {
                "warning_to_replacement_pod_ready_seconds": summary.get("recovery_timeline_seconds", {}).get("notice_to_replacement_pod_ready"),
                "warning_to_ready_replicas_2_seconds": summary.get("recovery_timeline_seconds", {}).get("notice_to_full_recovery"),
                **drain,
            },
        })
    return result


def valid_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [row for row in runs if row.get("valid_interruption")]


def summarize_group(runs: list[dict[str, Any]]) -> dict[str, Any]:
    valid = valid_runs(runs)
    paths = {
        "http_failure_rate_percent": [r.get("http", {}).get("failure_rate_percent") for r in valid],
        "http_max_contiguous_failure_window_seconds": [r.get("http", {}).get("max_contiguous_failure_window_seconds") for r in valid],
        "warning_to_third_2xx_after_last_failure_seconds": [r.get("http", {}).get("warning_to_third_2xx_after_last_failure_seconds") for r in valid],
        "warning_to_replacement_pod_ready_seconds": [r.get("timeline", {}).get("warning_to_replacement_pod_ready_seconds") for r in valid],
        "warning_to_ready_replicas_2_seconds": [r.get("timeline", {}).get("warning_to_ready_replicas_2_seconds") for r in valid],
        "warning_to_pod_killing_seconds": [r.get("timeline", {}).get("warning_to_killing_seconds") for r in valid],
        "killing_to_container_terminated_seconds": [r.get("timeline", {}).get("killing_to_container_terminated_seconds") for r in valid],
        "killing_to_pod_absence_observed_seconds": [r.get("timeline", {}).get("killing_to_pod_absence_observed_seconds") for r in valid],
    }
    return {
        "attempts": len(runs),
        "valid_interruption_n": len(valid),
        "failed_or_invalid_trials": [r["trial"] for r in runs if not r.get("valid_interruption")],
        "metrics_median_min_max_n": {key: metrics([v for v in values if v is not None]) for key, values in paths.items()},
    }


def main() -> None:
    base = baseline_runs()
    tuned = tuned_runs()
    comparison = {
        "baseline_batch": "20261002-phase3-v6-baseline",
        "baseline_raw_batches": [
            "20261002-phase3-spot-interruption-v5",
            "20261002-phase3-v6-baseline",
        ],
        "tuned_batch": "20261003-phase3-prestop5",
        "only_intended_condition_change": "container preStop exec sleep 5s",
        "baseline": summarize_group(base),
        "tuned": summarize_group(tuned),
        "baseline_runs": base,
        "tuned_runs": tuned,
        "definitions": {
            "http_failure_rate": "failed non-2xx or transport-error requests / all requests * 100",
            "last_failure_recovery": "third consecutive 2xx completion after the last failed response completion; requests must start at or after the last failure completion",
            "drain_span": "Kubernetes Killing event to container terminated.finishedAt when both are present; this is the observable termination span, not an independently instrumented hook-only duration",
            "pod_absence": "Kubernetes API Pod snapshots are polled every two seconds; report Killing event to the first subsequent snapshot where the original Pod is absent",
            "missing_data": "Do not impute; metric valid_n is reported separately for each field.",
        },
    }
    json_path = TUNED / "analysis" / "phase3-prestop5-comparison.json"
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(comparison, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Phase 3 preStop 5s comparison",
        "",
        "Baseline v5/v6 raw files and reports remain in their original batch directories. The tuned batch contains exactly the preregistered five attempts; invalid attempts are retained and are not replaced.",
        "",
        "| Metric | Baseline median / min–max / n | preStop 5s median / min–max / n |",
        "|---|---:|---:|",
    ]
    labels = {
        "http_failure_rate_percent": "HTTP failure rate (%)",
        "http_max_contiguous_failure_window_seconds": "Maximum contiguous HTTP failure (s)",
        "warning_to_third_2xx_after_last_failure_seconds": "Warning → 3×2xx after last failure (s)",
        "warning_to_replacement_pod_ready_seconds": "Warning → replacement Pod Ready (s)",
        "warning_to_ready_replicas_2_seconds": "Warning → Ready replicas 2 (s)",
        "warning_to_pod_killing_seconds": "Warning → original Pod Killing event (s)",
        "killing_to_container_terminated_seconds": "Killing → container terminated (s)",
        "killing_to_pod_absence_observed_seconds": "Killing → original Pod absent from API snapshots (s)",
    }
    for key, label in labels.items():
        b = comparison["baseline"]["metrics_median_min_max_n"][key]
        t = comparison["tuned"]["metrics_median_min_max_n"][key]
        def fmt(row: dict[str, Any]) -> str:
            if row["valid_n"] == 0:
                return "— / — / 0"
            return f'{row["median"]:.6g} / {row["min"]:.6g}–{row["max"]:.6g} / {row["valid_n"]}'
        lines.append(f"| {label} | {fmt(b)} | {fmt(t)} |")
    lines += [
        "",
        "`Killing → container terminated` includes the configured preStop hook and container shutdown; Kubernetes raw does not necessarily expose a separate hook-completion timestamp. Sample counts can differ by metric because raw status snapshots do not retain every deleted Pod.",
        "",
        "Per-run details and the strict HTTP recovery derivation are in `analysis/phase3-prestop5-comparison.json`.",
        "",
    ]
    (TUNED / "analysis" / "PHASE3-PRESTOP5-COMPARISON.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
