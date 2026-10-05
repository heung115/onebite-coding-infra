#!/usr/bin/env python3
"""6.5GB VM 재측정(before / after ArgoCD) 집계. 사용: python3 analyze_6.5g.py > rerun-6.5g/summary.json"""
import json, statistics, datetime, pathlib
M = pathlib.Path(__file__).resolve().parent
def meta(p):
    d = {}
    for line in (p / "meta.txt").read_text().split("\n"):
        for tok in line.split():
            if "=" in tok:
                k, v = tok.split("=", 1); d.setdefault(k, v)
    return d
def ts(s): return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
def f(x): return float(x) if x not in (None, "") else None
def steps(p):
    r = json.loads((p / "run.json").read_text()); out = {}
    for j in r["jobs"]:
        for s in j["steps"]:
            out[(j["name"], s["name"])] = ts(s["completedAt"]) - ts(s["startedAt"])
    return r, out
def stat(vals):
    v = [x for x in vals if x is not None]
    return {"median": round(statistics.median(v), 1), "min": round(min(v), 1), "max": round(max(v), 1), "n": len(v)} if v else None

B = M / "before-deploy/raw"; A = M / "after-argocd/raw"
res = {"before_deploy": [], "before_failure": [], "after_deploy_default": [], "after_deploy_poll30": [],
       "after_failure_app": [], "after_failure_gitops": []}
for i in (1, 2, 3):
    p = B / f"b65-deploy-{i}"; m = meta(p); r, st = steps(p)
    res["before_deploy"].append({"i": i, "conclusion": r["conclusion"],
        "push_to_ready": f(m["t_ready"]) - f(m["t_push"]),
        "push_to_restart": f(m["t_newrev"]) - f(m["t_push"]),
        "restart_to_ready": f(m["t_ready"]) - f(m["t_newrev"]),
        "build": st[("build-and-push", "Build and Push Docker Image")],
        "run_total": ts(r["updatedAt"]) - ts(r["createdAt"])})
    p = B / f"b65-failure-{i}"; m = meta(p)
    res["before_failure"].append({"i": i, "conclusion": m["bad_conclusion"],
        "ci_done_after_push": f(m["t_bad_run_done"]) - f(m["t_bad_push"]),
        "first_fail_after_ci_success": f(m["t_first_fail"]) - f(m["t_bad_run_done"]),
        "recover_after_fix_push": f(m["t_recovered"]) - f(m["t_fix_push"]),
        "old_pod_ready": m.get("old_pod_still_ready")})
for key, lbl in (("after_deploy_default", "a65-deploy-default"), ("after_deploy_poll30", "a65-deploy-poll30")):
    for i in (1, 2, 3):
        p = A / f"{lbl}-{i}"; m = meta(p); r, st = steps(p)
        res[key].append({"i": i, "conclusion": r["conclusion"],
            "push_to_ready": f(m["t_ready"]) - f(m["t_push"]),
            "build": st[("build-and-push", "Build and Push Docker Image")],
            "push_to_gitops_commit": f(m["t_gitops_commit"]) - f(m["t_push"]),
            "gitops_commit_to_applied": f(m["t_newrev"]) - f(m["t_gitops_commit"]),
            "applied_to_ready": f(m["t_ready"]) - f(m["t_newrev"]),
            "app_state": m.get("app_state")})
for mode in ("app", "gitops"):
    for i in (1, 2, 3):
        p = A / f"a65-failure-{mode}-{i}"; m = meta(p)
        res[f"after_failure_{mode}"].append({"i": i, "conclusion": m["bad_conclusion"],
            "first_fail_after_ci_success": f(m["t_first_fail"]) - f(m["t_bad_run_done"]),
            "degraded_after_bad_applied": (f(m["t_degraded"]) - f(m["t_bad_applied"])) if m.get("t_degraded") else None,
            "degraded_after_first_fail": (f(m["t_degraded"]) - f(m["t_first_fail"])) if m.get("t_degraded") else None,
            "recover_after_fix_push": f(m["t_recovered"]) - f(m["t_fix_push"]),
            "old_pod_ready": m.get("old_pod_still_ready")})
summary = {}
for k, rows in res.items():
    summary[k] = {c: stat([r[c] for r in rows]) for c in rows[0] if c not in ("i", "conclusion", "old_pod_ready", "app_state")}
    summary[k]["conclusions"] = [r["conclusion"] for r in rows]
print(json.dumps({"runs": res, "summary": summary}, ensure_ascii=False, indent=2))
