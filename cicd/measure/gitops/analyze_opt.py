#!/usr/bin/env python3
"""ArgoCD 최적화 단계별 집계: base(기본 120s+60s) / poll30(30s, 캐시 기본 2m) / opt1(30s + revision cache 30s) / opt2(opt1 + progressDeadline 180)."""
import json, statistics, datetime, pathlib
A = pathlib.Path(__file__).resolve().parent / "after-argocd/raw"
def meta(p):
    d = {}
    for line in (p / "meta.txt").read_text().split("\n"):
        for tok in line.split():
            if "=" in tok:
                k, v = tok.split("=", 1); d.setdefault(k, v)
    return d
f = lambda x: float(x) if x not in (None, "") else None
def st(v):
    v = [x for x in v if x is not None]
    return {"median": round(statistics.median(v), 1), "min": round(min(v), 1), "max": round(max(v), 1), "n": len(v)} if v else None
def deploy(lbl):
    rows = []
    for i in (1, 2, 3):
        p = A / f"{lbl}-{i}"
        if not (p / "meta.txt").exists(): continue
        m = meta(p)
        rows.append({"push_to_ready": f(m["t_ready"]) - f(m["t_push"]),
                     "gitops_commit_to_applied": f(m["t_newrev"]) - f(m["t_gitops_commit"]),
                     "applied_to_ready": f(m["t_ready"]) - f(m["t_newrev"])})
    return rows
def failure(lbl):
    rows = []
    for i in (1, 2, 3):
        p = A / f"{lbl}-{i}"
        if not (p / "meta.txt").exists(): continue
        m = meta(p)
        r = {"recover_after_fix_push": f(m["t_recovered"]) - f(m["t_fix_push"]),
             "fix_push_to_applied": f(m["t_fix_applied"]) - f(m["t_fix_push"])}
        if m.get("t_degraded"):
            r["degraded_after_bad_applied"] = f(m["t_degraded"]) - f(m["t_bad_applied"])
            r["degraded_after_first_fail"] = f(m["t_degraded"]) - f(m["t_first_fail"])
        rows.append(r)
    return rows
sets = {
 "deploy": {"base": deploy("a65-deploy-default"), "poll30": deploy("a65-deploy-poll30"), "opt1": deploy("o1-deploy")},
 "failure_gitops": {"base": failure("a65-failure-gitops"), "opt1": failure("o1-failure-gitops")},
 "failure_app": {"base": failure("a65-failure-app"), "opt1": failure("o1-failure-app"), "opt2": failure("o2-failure-app")},
}
out = {}
for k, byphase in sets.items():
    out[k] = {}
    for ph, rows in byphase.items():
        if rows: out[k][ph] = {c: st([r.get(c) for r in rows]) for c in rows[0]}
print(json.dumps({"summary": out, "runs": sets}, ensure_ascii=False, indent=2))
