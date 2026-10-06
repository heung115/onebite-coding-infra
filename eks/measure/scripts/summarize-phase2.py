#!/usr/bin/env python3
"""Summarize Phase 2 selection, resource use, cost, readiness and failures."""

from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any


FILE_RE = re.compile(r"^phase2-(general|cpu-heavy|memory-heavy)-(fixed|flexible)-(\d{2})\.json$")
PROFILES = ("general", "cpu-heavy", "memory-heavy")
CONDITIONS = ("fixed", "flexible")


def number(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (float, int)):
        return float(value)
    if isinstance(value, str):
        raw = value.strip()
        if raw.endswith("m"):
            return float(raw[:-1])
        try:
            return float(Decimal(raw))
        except Exception:
            return None
    return None


def metric_value(run: dict[str, Any], key: str) -> float | None:
    resource = run.get("resource_metrics", {})
    if key == "ready_seconds":
        return number(run.get("scale_request_to_all_pods_ready_seconds"))
    if key == "node_count":
        return number(resource.get("node_count"))
    if key == "provisioned_ec2_capacity_rate_usd_per_hour":
        return number(resource.get("provisioned_ec2_capacity_rate_usd_per_hour"))
    group, unit = key.split(".", 1)
    return number(resource.get(group, {}).get(unit))


METRICS = (
    ("node_count", "node count", "nodes"),
    ("allocatable.cpu", "allocatable CPU", "mCPU"),
    ("allocatable.memory_mib", "allocatable Memory", "MiB"),
    ("all_scheduled_pod_requested.cpu", "all Pod requested CPU", "mCPU"),
    ("all_scheduled_pod_requested.memory_mib", "all Pod requested Memory", "MiB"),
    ("unused.cpu", "unused CPU", "mCPU"),
    ("unused.memory_mib", "unused Memory", "MiB"),
    ("provisioned_ec2_capacity_rate_usd_per_hour", "Provisioned EC2 capacity rate", "USD/hour"),
    ("ready_seconds", "Pod Ready time (secondary)", "seconds"),
)


def median_range(values: list[float]) -> dict[str, Any] | None:
    if not values:
        return None
    return {"n": len(values), "median": statistics.median(values), "min": min(values), "max": max(values)}


def load_trials(directory: Path) -> tuple[dict[tuple[str, str, int], dict[str, Any]], list[str]]:
    trials: dict[tuple[str, str, int], dict[str, Any]] = {}
    for path in sorted(directory.iterdir()):
        match = FILE_RE.match(path.name)
        if not match:
            continue
        profile, condition, trial_no = match.group(1), match.group(2), int(match.group(3))
        data = json.loads(path.read_text(encoding="utf-8"))
        data["file"] = path.name
        trials[(profile, condition, trial_no)] = data
    missing = []
    for profile in PROFILES:
        for condition in CONDITIONS:
            for trial_no in range(1, 4):
                if (profile, condition, trial_no) not in trials:
                    missing.append(f"phase2-{profile}-{condition}-{trial_no:02d}.json")
    return trials, missing


def make_summary(directory: Path) -> dict[str, Any]:
    trials, missing = load_trials(directory)
    result: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "planned_runs": 18,
        "attempted_runs": len(trials),
        "missing_runs": missing,
        "profiles": {},
        "paired_differences": {},
    }
    for profile in PROFILES:
        result["profiles"][profile] = {}
        for condition in CONDITIONS:
            rows = [trials[(profile, condition, n)] for n in range(1, 4) if (profile, condition, n) in trials]
            valid = [row for row in rows if row.get("succeeded") and row.get("resource_metrics")]
            result["profiles"][profile][condition] = {
                "attempted": len(rows),
                "valid": len(valid),
                "failed": [
                    {"file": row.get("file"), "reason": row.get("failure_reason"), "cleanup_failure": row.get("cleanup_failure")}
                    for row in rows if not row.get("succeeded")
                ],
                "instance_type_counts": dict(Counter(
                    instance_type
                    for row in valid
                    for instance_type, count in row.get("resource_metrics", {}).get("instance_type_counts", {}).items()
                    for _ in range(count)
                )),
                "instance_family_counts": dict(Counter(
                    family
                    for row in valid
                    for family, count in row.get("resource_metrics", {}).get("instance_family_counts", {}).items()
                    for _ in range(count)
                )),
                "instance_size_counts": dict(Counter(
                    size
                    for row in valid
                    for size, count in row.get("resource_metrics", {}).get("instance_size_counts", {}).items()
                    for _ in range(count)
                )),
                "pod_packing_by_trial": [
                    {
                        "trial": row.get("trial"),
                        "nodes": [
                            {
                                "instance_type": node.get("instance_type"),
                                "benchmark_pod_count": node.get("benchmark_pod_count"),
                                "benchmark_pod_names": node.get("benchmark_pod_names", []),
                            }
                            for node in row.get("resource_metrics", {}).get("per_node", [])
                        ],
                    }
                    for row in valid
                ],
                "metrics": {},
            }
            for key, _, _ in METRICS:
                values = [value for row in valid if (value := metric_value(row, key)) is not None]
                result["profiles"][profile][condition]["metrics"][key] = median_range(values)

        pairs = []
        for pair_index in range(1, 4):
            pair = f"{profile}-{pair_index:02d}"
            fixed = [row for (p, c, _), row in trials.items() if p == profile and c == "fixed" and row.get("pair") == pair]
            flexible = [row for (p, c, _), row in trials.items() if p == profile and c == "flexible" and row.get("pair") == pair]
            if not fixed or not flexible or not fixed[0].get("succeeded") or not flexible[0].get("succeeded"):
                continue
            deltas = {}
            for key, _, _ in METRICS:
                left = metric_value(fixed[0], key)
                right = metric_value(flexible[0], key)
                deltas[key] = right - left if left is not None and right is not None else None
            pairs.append({"pair": pair, "fixed_file": fixed[0].get("file"), "flexible_file": flexible[0].get("file"), "flexible_minus_fixed": deltas})
        result["paired_differences"][profile] = pairs
    return result


def fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 1000 else f"{value:.0f}"
    return str(value)


def render_markdown(summary: dict[str, Any], trials: dict[tuple[str, str, int], dict[str, Any]]) -> str:
    lines = [
        "# Phase 2 — Karpenter Fixed vs Flexible 결과",
        "",
        f"생성 시각(UTC): {summary['created_at_utc']}",
        f"시도 {summary['attempted_runs']}/18회 · 미기록 {len(summary['missing_runs'])}회",
        "",
        "비교는 실제 선택 family/type·size, 두 benchmark Pod의 node별 packing, node 수, resource waste, EC2 provisioned capacity 시간당 요율을 중심으로 한다. Flexible 결과는 family만의 효과가 아니라 instance family/type·size·packing·node 수가 함께 반영된 capacity selection 결과로 해석하며, 우위를 전제하지 않는다. 실패는 성공 통계에서 제외하고 아래에 보존한다.",
        "",
    ]
    for profile in PROFILES:
        lines += [f"## {profile}", "", "| 지표 | Fixed (중앙값, 범위) | Flexible (중앙값, 범위) |", "|---|---:|---:|"]
        for key, label, unit in METRICS:
            condition_values = []
            for condition in CONDITIONS:
                stats = summary["profiles"][profile][condition]["metrics"].get(key)
                if stats is None:
                    condition_values.append("유효 0회")
                else:
                    condition_values.append(f"{fmt(stats['median'])} {unit} ({fmt(stats['min'])}–{fmt(stats['max'])}; n={stats['n']})")
            lines.append(f"| {label} | {condition_values[0]} | {condition_values[1]} |")
        for condition in CONDITIONS:
            data = summary["profiles"][profile][condition]
            selected = ", ".join(f"`{name}` × {count}" for name, count in sorted(data["instance_type_counts"].items())) or "없음"
            families = ", ".join(f"`{name}` × {count}" for name, count in sorted(data["instance_family_counts"].items())) or "없음"
            sizes = ", ".join(f"`{name}` × {count}" for name, count in sorted(data["instance_size_counts"].items())) or "없음"
            lines.append(f"\n- {condition}: 유효 {data['valid']}/3회, 선택 instance type: {selected}; family: {families}; size: {sizes}")
            packing = "; ".join(
                f"run {item['trial']}: " + ", ".join(
                    f"{node['instance_type']}에 Pod {node['benchmark_pod_count']}개"
                    for node in item["nodes"]
                )
                for item in data["pod_packing_by_trial"]
            ) or "없음"
            lines.append(f"  - 두 Pod packing: {packing}")
        pairs = summary["paired_differences"].get(profile, [])
        if pairs:
            lines += ["", "paired difference (Flexible − Fixed):"]
            for item in pairs:
                deltas = item["flexible_minus_fixed"]
                concise = ", ".join(f"{key}={fmt(value)}" for key, value in deltas.items() if value is not None and key != "ready_seconds")
                lines.append(f"- {item['pair']}: {concise}")
        for condition in CONDITIONS:
            for failure in summary["profiles"][profile][condition]["failed"]:
                lines.append(f"\n- 실패 `{failure['file']}`: {failure.get('reason') or '원인 미기록'}; cleanup={failure.get('cleanup_failure') or '성공'}")
        lines.append("")
    if summary["missing_runs"]:
        lines += ["## 누락 회차", "", *[f"- `{name}`" for name in summary["missing_runs"]], ""]
    lines += ["## 제한", "", "Pod Ready 시간은 보조 지표다. ‘Provisioned EC2 capacity rate’는 관찰된 EC2 instance type과 node 수에 서울 Linux Shared On-Demand 공개 요율을 곱한 시간당 capacity 요율이다. 같은 workload를 수용하도록 provision된 EC2 capacity의 rate이며 실제 청구액·청구 절감액이 아니다. EKS control plane, system node, EBS, IPv4, 데이터 전송, 세금은 포함하지 않는다. 실제 청구액은 AWS billing/Cost Explorer 확인 결과와 분리해 기록한다. Flexible 결과는 instance family/type, size, Pod packing, node 수가 함께 반영된 선택 결과라 family 단독 효과로 해석하지 않는다.", ""]
    return "\n".join(lines)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: summarize-phase2.py measure/results/<session-dir>", file=sys.stderr)
        return 2
    directory = Path(sys.argv[1]).resolve()
    if not directory.is_dir():
        print(f"Missing result directory: {directory}", file=sys.stderr)
        return 2
    summary = make_summary(directory)
    trials, _ = load_trials(directory)
    (directory / "phase2-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (directory / "phase2-summary.md").write_text(render_markdown(summary, trials), encoding="utf-8")
    print(json.dumps({"attempted_runs": summary["attempted_runs"], "missing_runs": len(summary["missing_runs"]), "summary": str(directory / "phase2-summary.md")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
