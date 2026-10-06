#!/usr/bin/env python3
"""Summarize completed trials with medians and min–max ranges; keep raw files unchanged."""

from __future__ import annotations

import argparse
import csv
import json
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def elapsed_seconds(start: str | None, end: str | None) -> float | None:
    left, right = parse_time(start), parse_time(end)
    if left is None or right is None:
        return None
    return (right - left).total_seconds()


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def describe(values: list[float]) -> str:
    if not values:
        return "—"
    median = statistics.median(values)
    return f"{median:.3f} ({min(values):.3f}–{max(values):.3f}; n={len(values)})"


def trials(session: Path, glob: str) -> list[dict[str, Any]]:
    result = []
    for path in sorted(session.glob(glob)):
        try:
            result.append(read_json(path))
        except (OSError, json.JSONDecodeError):
            continue
    return result


def request_stats(summary: dict[str, Any], session: Path) -> tuple[float | None, float | None]:
    path = Path(summary.get("raw_requests_file", ""))
    if not path.is_absolute():
        cwd_path = path.resolve()
        path = cwd_path if cwd_path.is_file() else session / path.name
    latencies: list[float] = []
    errors = summary.get("http_or_network_errors")
    requests = summary.get("requests")
    if path.is_file():
        with path.open(encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                try:
                    latencies.append(float(row["latency_ms"]))
                except (KeyError, TypeError, ValueError):
                    pass
    error_rate = None
    if isinstance(errors, int) and isinstance(requests, int) and requests:
        error_rate = errors * 100.0 / requests
    return (statistics.median(latencies) if latencies else None, error_rate)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir", type=Path)
    args = parser.parse_args()
    session = args.session_dir.resolve()
    if not session.is_dir():
        parser.error(f"not a session directory: {session}")

    lines = [
        "# Experiment summary",
        "",
        "See [REPORT.md](REPORT.md) for per-trial values, interpretation, setup failures, cleanup and cost status.",
        "",
        "Time metrics use the recorded UTC request and observation timestamps. Values are median (min–max; valid n). Failed or incomplete trials remain listed and are excluded only from that metric's numeric summary. For A, use the paired decision and sufficiency thresholds in a-paired-analysis.md; for other experiments, fewer than three valid repetitions is descriptive, not a comparison.",
        "",
        "## A — autoscaler",
        "",
        "| Condition | Ready time, seconds | Experiment nodes at completion | Completed trials |",
        "|---|---:|---:|---:|",
    ]
    a_trials = [
        item for item in trials(session, "a-*.json")
        if item.get("condition") in {"ca", "karpenter"} and isinstance(item.get("pair"), int)
    ]
    for condition in sorted({str(item.get("condition", "unknown")) for item in a_trials}):
        selected = [item for item in a_trials if item.get("condition") == condition]
        elapsed = [
            value
            for item in selected
            if (value := elapsed_seconds(item.get("scale_requested_at_utc"), item.get("markers", {}).get("all_pods_ready_at_utc"))) is not None
            and item.get("succeeded")
        ]
        node_counts = [float(len(item.get("final_experiment_nodes", []))) for item in selected if item.get("succeeded")]
        completed = sum(bool(item.get("succeeded")) for item in selected)
        lines.append(f"| {condition} | {describe(elapsed)} | {describe(node_counts)} | {completed}/{len(selected)} |")

    lines.extend(
        [
            "",
            "## B — AZ placement and node failure",
            "",
            "| Condition | Per-trial median latency, ms | HTTP errors, % | Completed trials |",
            "|---|---:|---:|---:|",
        ]
    )
    b_trials = trials(session, "b-*.summary.json")
    b_failures = trials(session, "b-*.failure.json")
    b_attempts = b_trials + b_failures
    grouped_b: dict[str, list[tuple[float | None, float | None]]] = {}
    for item in b_trials:
        latency, errors = request_stats(item, session)
        grouped_b.setdefault(str(item.get("condition", "unknown")), []).append((latency, errors))
    for item in b_failures:
        grouped_b.setdefault(str(item.get("condition", "unknown")), [])
    for condition, values in sorted(grouped_b.items()):
        latencies = [latency for latency, _ in values if latency is not None]
        error_rates = [error for _, error in values if error is not None]
        attempts = sum(str(item.get("condition", "unknown")) == condition for item in b_attempts)
        error_summary = describe(error_rates)
        if error_rates:
            error_summary += "%"
        lines.append(f"| {condition} | {describe(latencies)} | {error_summary} | {len(values)}/{attempts} |")

    lines.extend(
        [
            "",
            "B reset procedures differed: valid baseline trials did not explicitly restart the Deployment, while spread trials did to verify AZ placement. Treat the cross-condition values as descriptive, not as an isolated causal effect of topology spread.",
        ]
    )

    lines.extend(
        [
            "",
            "## C — Prefix Delegation",
            "",
            "| Condition | Ready Pods | Ready time, seconds | Completed trials |",
            "|---|---:|---:|---:|",
        ]
    )
    c_trials = trials(session, "c-*.json")
    for condition in sorted({str(item.get("condition", "unknown")) for item in c_trials}):
        selected = [item for item in c_trials if item.get("condition") == condition]
        ready_counts = [
            float(sum(bool(pod.get("ready")) for pod in item.get("final_pods", [])))
            for item in selected
        ]
        ready_times = [
            value
            for item in selected
            if (value := elapsed_seconds(item.get("scale_requested_at_utc"), item.get("markers", {}).get("all_pods_ready_at_utc"))) is not None
            and item.get("succeeded")
        ]
        completed = sum(bool(item.get("succeeded")) for item in selected)
        lines.append(f"| {condition} | {describe(ready_counts)} | {describe(ready_times)} | {completed}/{len(selected)} |")

    lines.extend(
        [
            "",
            "## Failures and limits",
            "",
            "Review each trial JSON and raw JSONL/CSV for timeouts, rejected requests, Pending Pods, and capacity limits. Do not promote this generated summary into portfolio text until the underlying measurements and session environment have been reviewed.",
            "",
        ]
    )
    failures = [
        item
        for item in a_trials + b_failures + c_trials
        if item.get("error") or ("succeeded" in item and not item.get("succeeded"))
    ]
    if failures:
        lines.extend(["", "### Failed or incomplete trials", ""])
        for item in failures:
            condition = item.get("condition", "unknown")
            trial = item.get("trial", "unknown")
            reason = item.get("error") or "; ".join(item.get("limitations", [])) or "did not complete"
            lines.append(f"- {condition}, trial {trial}: {reason}")
    output = session / "summary.md"
    output.write_text("\n".join(lines), encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
