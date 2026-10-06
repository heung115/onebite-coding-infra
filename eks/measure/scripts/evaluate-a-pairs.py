#!/usr/bin/env python3
"""Evaluate the preregistered interim stop rule and summarize paired A results."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any


MIN_VALID_PER_CONDITION = 10
MIN_COMPLETE_PAIRS = 10
MIN_DIRECTION_SHARE = 0.75
TRIAL_NAME = re.compile(r"a-(ca|karpenter)-(\d{2})\.json$")


def elapsed_seconds(item: dict[str, Any]) -> float | None:
    start = item.get("scale_requested_at_utc")
    end = item.get("markers", {}).get("all_pods_ready_at_utc")
    if not start or not end:
        return None
    try:
        value = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                 - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def describe(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "median_seconds": round(statistics.median(values), 3) if values else None,
        "min_seconds": round(min(values), 3) if values else None,
        "max_seconds": round(max(values), 3) if values else None,
    }


def load_trials(directory: Path, max_pair: int | None = None) -> tuple[dict[str, dict[int, dict[str, Any]]], dict[str, int]]:
    valid: dict[str, dict[int, dict[str, Any]]] = {"ca": {}, "karpenter": {}}
    attempts = {"ca": 0, "karpenter": 0}
    for path in sorted(directory.glob("a-*.json")):
        match = TRIAL_NAME.fullmatch(path.name)
        if not match:
            continue
        condition, _trial = match.groups()
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pair = item.get("pair")
        if not isinstance(pair, int) or (max_pair is not None and pair > max_pair):
            continue
        attempts[condition] += 1
        value = elapsed_seconds(item)
        if item.get("succeeded") is True and value is not None:
            valid[condition][pair] = {"seconds": value, "file": path.name}
    return valid, attempts


def evaluate(directory: Path, phase: str) -> dict[str, Any]:
    valid, attempts = load_trials(directory, max_pair=12 if phase == "interim" else None)
    complete_pair_ids = sorted(set(valid["ca"]) & set(valid["karpenter"]))
    differences = [
        {"pair": pair, "seconds": valid["karpenter"][pair]["seconds"] - valid["ca"][pair]["seconds"]}
        for pair in complete_pair_ids
    ]
    values = [row["seconds"] for row in differences]
    positive = sum(value > 0 for value in values)
    negative = sum(value < 0 for value in values)
    ties = sum(value == 0 for value in values)
    dominant_sign = 1 if positive > negative else -1 if negative > positive else 0
    dominant_count = max(positive, negative)
    required_direction_count = math.ceil(MIN_DIRECTION_SHARE * len(values)) if values else 0
    full_median = statistics.median(values) if values else None
    outlier_checks: dict[str, float | None] = {}
    for drop_count in (1, 2):
        remaining = sorted(differences, key=lambda row: abs(row["seconds"]))[:-drop_count] if len(differences) > drop_count else []
        trimmed_median = statistics.median(row["seconds"] for row in remaining) if remaining else None
        outlier_checks[f"drop_largest_{drop_count}_absolute_differences"] = (
            round(trimmed_median, 3) if trimmed_median is not None else None
        )

    valid_counts = {condition: len(valid[condition]) for condition in ("ca", "karpenter")}
    enough_valid = all(count >= MIN_VALID_PER_CONDITION for count in valid_counts.values())
    enough_pairs = len(complete_pair_ids) >= MIN_COMPLETE_PAIRS
    direction_consistent = (
        bool(values)
        and dominant_sign != 0
        and dominant_count >= required_direction_count
        and sign(full_median) == dominant_sign
    )
    outlier_robust = bool(direction_consistent)
    for trimmed_median in outlier_checks.values():
        if trimmed_median is None or sign(float(trimmed_median)) != dominant_sign:
            outlier_robust = False
    passed = enough_valid and enough_pairs and direction_consistent and outlier_robust
    reasons = []
    if not enough_valid:
        reasons.append(f"valid trials per condition must be at least {MIN_VALID_PER_CONDITION}")
    if not enough_pairs:
        reasons.append(f"complete valid pairs must be at least {MIN_COMPLETE_PAIRS}")
    if not direction_consistent:
        reasons.append(f"one paired-difference direction must account for at least {MIN_DIRECTION_SHARE:.0%} of complete pairs and match the median")
    if not outlier_robust:
        reasons.append("median paired-difference direction must remain unchanged after removing the 1 and 2 largest absolute differences")

    decision: dict[str, Any] = {
        "phase": phase,
        "decision": "sufficient_at_12_pairs" if phase == "interim" and passed else (
            "continue_to_pairs_13_20" if phase == "interim" else "final_directional_result" if passed else "final_inconclusive"
        ),
        "stop_after_12": bool(phase == "interim" and passed),
        "criteria_passed": passed,
        "thresholds": {
            "min_valid_trials_per_condition": MIN_VALID_PER_CONDITION,
            "min_complete_valid_pairs": MIN_COMPLETE_PAIRS,
            "min_paired_difference_direction_share": MIN_DIRECTION_SHARE,
            "outlier_sensitivity": "median sign must remain the same after removing the 1 and 2 largest absolute paired differences",
        },
        "attempted_runs": attempts,
        "valid_runs": valid_counts,
        "complete_valid_pair_count": len(complete_pair_ids),
        "paired_difference_definition": "Karpenter scale-request-to-all-Pods-Ready seconds minus CA seconds; positive means Karpenter took longer",
        "paired_difference_direction_counts": {"positive": positive, "negative": negative, "ties": ties},
        "dominant_direction": "positive" if dominant_sign > 0 else "negative" if dominant_sign < 0 else "none",
        "dominant_direction_share": round(dominant_count / len(values), 4) if values else None,
        "required_direction_count": required_direction_count,
        "paired_difference_median_seconds": round(full_median, 3) if full_median is not None else None,
        "paired_difference_range_seconds": [round(min(values), 3), round(max(values), 3)] if values else None,
        "trimmed_medians_seconds": outlier_checks,
        "complete_pairs": [
            {"pair": pair, "ca_seconds": round(valid["ca"][pair]["seconds"], 3),
             "karpenter_seconds": round(valid["karpenter"][pair]["seconds"], 3),
             "difference_seconds": round(valid["karpenter"][pair]["seconds"] - valid["ca"][pair]["seconds"], 3)}
            for pair in complete_pair_ids
        ],
        "failed_or_incomplete_trials": [],
        "reasons_to_continue_or_limit_claim": reasons,
    }
    for path in sorted(directory.glob("a-*.json")):
        match = TRIAL_NAME.fullmatch(path.name)
        if not match:
            continue
        condition, _trial = match.groups()
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        pair = item.get("pair")
        if phase == "interim" and isinstance(pair, int) and pair > 12:
            continue
        if item.get("succeeded") is not True or elapsed_seconds(item) is None:
            decision["failed_or_incomplete_trials"].append({
                "file": path.name,
                "condition": condition,
                "pair": pair,
                "error": item.get("error") or "; ".join(item.get("limitations", [])) or "missing successful scale-request-to-all-Pods-Ready timestamps",
            })
    decision["condition_ready_time_seconds"] = {
        condition: describe([row["seconds"] for row in valid[condition].values()])
        for condition in ("ca", "karpenter")
    }
    return decision


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("session_dir", type=Path)
    parser.add_argument("--phase", choices=("interim", "final"), required=True)
    args = parser.parse_args()
    directory = args.session_dir.resolve()
    if not directory.is_dir():
        parser.error(f"not a results directory: {directory}")
    decision = evaluate(directory, args.phase)
    output = directory / ("a-interim-decision.json" if args.phase == "interim" else "a-paired-analysis.json")
    output.write_text(json.dumps(decision, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"phase": args.phase, "decision": decision["decision"],
                      "criteria_passed": decision["criteria_passed"],
                      "valid_runs": decision["valid_runs"],
                      "complete_valid_pair_count": decision["complete_valid_pair_count"]}, ensure_ascii=False))
    if args.phase == "final":
        write_report(directory, decision)
    return 0


def write_report(directory: Path, result: dict[str, Any]) -> None:
    def fmt(value: Any) -> str:
        return "—" if value is None else f"{value:.3f}"

    lines = [
        "# A — paired autoscaler analysis",
        "",
        f"결론 상태: {result['decision']}. 유효 실행과 모든 실패 시도는 아래 요약 및 원시 JSON에 보존했다.",
        "",
        "측정값은 scale 요청부터 모든 benchmark Pod가 Ready가 될 때까지의 초 단위 시간이다. paired difference는 Karpenter − CA이며, 양수는 Karpenter가 더 오래 걸렸음을 뜻한다.",
        "",
        "## 조건별 결과",
        "",
        "| 조건 | 유효 회차 | 중앙값 (초) | 범위 (초) |",
        "|---|---:|---:|---:|",
    ]
    for condition in ("ca", "karpenter"):
        stats = result["condition_ready_time_seconds"][condition]
        lines.append(f"| {condition} | {stats['n']} | {fmt(stats['median_seconds'])} | {fmt(stats['min_seconds'])}–{fmt(stats['max_seconds'])} |")
    paired_range = result["paired_difference_range_seconds"]
    range_text = "—" if paired_range is None else f"{paired_range[0]:.3f}–{paired_range[1]:.3f}"
    lines.extend([
        "",
        "## Paired 결과와 12쌍 중간 판단",
        "",
        f"- 완전한 유효 쌍: {result['complete_valid_pair_count']}",
        f"- Paired difference 중앙값/범위: {fmt(result['paired_difference_median_seconds'])}초 / {range_text}초",
        f"- 방향별 쌍 수: 양수 {result['paired_difference_direction_counts']['positive']}, 음수 {result['paired_difference_direction_counts']['negative']}, 동률 {result['paired_difference_direction_counts']['ties']}",
        f"- 우세 방향 일치율: {fmt(result['dominant_direction_share'])}; 이상치 제외 중앙값: 1개 제외 {fmt(result['trimmed_medians_seconds']['drop_largest_1_absolute_differences'])}초, 2개 제외 {fmt(result['trimmed_medians_seconds']['drop_largest_2_absolute_differences'])}초",
        f"- 최종 판단: {result['decision']}",
        "",
        "12쌍에서 조건별 유효 실행 10회 이상, 완전 유효 쌍 10개 이상, 한 방향이 완전 쌍의 75% 이상이며 중앙값과 일치, 절대 paired difference가 큰 1개와 2개를 각각 제외해도 중앙값 방향 유지 조건을 모두 확인했다. 통과 시 12쌍을 최종 표본으로 사용하고, 미통과 시 사전 무작위화한 13–20쌍을 추가한다. 20쌍 후에도 통과하지 못하면 방향성 결론을 내리지 않는다.",
        "",
        "## 실패·제한",
        "",
    ])
    if result["failed_or_incomplete_trials"]:
        for item in result["failed_or_incomplete_trials"]:
            lines.append(f"- {item['file']} (pair {item['pair']}, {item['condition']}): {item['error']}")
    else:
        lines.append("- 실패 또는 불완전 회차 없음.")
    lines.extend(["", "원시 타임라인과 회차별 요약 JSON을 이 디렉터리에 유지한다.", ""])
    (directory / "a-paired-analysis.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
