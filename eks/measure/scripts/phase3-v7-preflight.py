"""Phase 3 v7 gates: reuse v5 reachability checks without changing SG rules."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any


_BASE_PATH = Path(__file__).with_name("phase3-v5-preflight.py")
_SPEC = importlib.util.spec_from_file_location("onebite_phase3_v5_preflight_base", _BASE_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError(f"Could not load existing Phase 3 preflight gates: {_BASE_PATH}")
_BASE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BASE)


def verify_backend_http_rules(
    recorder: Any,
    worker_sg_ids: list[str],
    backend_sg_id: str,
    experiment_tag: str,
    initial_groups: dict[str, Any],
) -> list[str]:
    """Require the existing exact rule; never authorize a replacement rule."""
    selected = sorted(set(worker_sg_ids))
    if not selected or not backend_sg_id:
        raise RuntimeError("Read-only SG gate could not identify backend and worker SGs")
    missing: list[str] = []
    for group_id in selected:
        group = initial_groups.get(group_id)
        if group is None or not _BASE.exact_backend_http_rule(group, backend_sg_id):
            missing.append(group_id)
    recorder.write({
        "kind": "phase3_v7_backend_sg_read_only_gate",
        "observed_at_utc": _BASE.utc_now(),
        "backend_security_group_id": backend_sg_id,
        "worker_security_group_ids": selected,
        "experiment_tag": experiment_tag,
        "rule_protocol": "tcp",
        "rule_port": 80,
        "rule_present_for_all_selected_workers": not missing,
        "missing_worker_security_group_ids": missing,
        "network_mutation_attempted": False,
    })
    if missing:
        raise RuntimeError(
            "Read-only SG gate failed: existing ALB backend SG -> worker SG TCP 80 rule is missing; no rule was created"
        )
    return []


# The existing v5 gate checks every reachability condition and ordering. Replace
# only its optional SG-rule setup with a read-only assertion for this batch.
_BASE.ensure_backend_http_rules = verify_backend_http_rules


def run_preflight(*args: Any, **kwargs: Any) -> dict[str, Any]:
    kwargs["verify_node_peer_http_rule"] = True
    return _BASE.run_preflight(*args, **kwargs)


def capture_controller_context(*args: Any, **kwargs: Any) -> None:
    _BASE.capture_controller_context(*args, **kwargs)
