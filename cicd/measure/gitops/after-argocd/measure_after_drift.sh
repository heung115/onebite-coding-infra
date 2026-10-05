#!/usr/bin/env bash
# After(ArgoCD): before 드리프트 실험과 같은 수동 변경(replicas 1→2, env 추가)을 하고 ArgoCD selfHeal 이 되돌리는 시간을 잰다.
set -euo pipefail
LABEL="${@: -1}"
OUT="$(cd "$(dirname "$0")" && pwd)/raw/$LABEL"; mkdir -p "$OUT"
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
kubectl -n $NS scale deploy/$DEP --replicas=2 >/dev/null
kubectl -n $NS set env deploy/$DEP DRIFT_TEST=manual-edit >/dev/null
T_EDIT=$(now); echo "t_edit=$T_EDIT" | tee "$OUT/meta.txt"
T_OOS=""; T_REVERTED=""
for i in $(seq 1 300); do
  s=$(kubectl -n argocd get application $APP -o jsonpath='{.status.sync.status}' 2>/dev/null || true)
  [ -z "$T_OOS" ] && [ "$s" = "OutOfSync" ] && T_OOS=$(now)
  rep=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.replicas}')
  env=$(kubectl -n $NS get deploy $DEP -o json | jq -r '[.spec.template.spec.containers[0].env[]? | select(.name=="DRIFT_TEST")] | length')
  if [ "$rep" = 1 ] && [ "$env" = 0 ]; then T_REVERTED=$(now); break; fi
  sleep 1
done
echo "t_outofsync=$T_OOS t_reverted=$T_REVERTED" | tee -a "$OUT/meta.txt"
kubectl -n argocd get application $APP -o json | jq '{sync:.status.sync.status, health:.status.health.status, history:[.status.history[-3:][]? | {revision,deployedAt}], op:.status.operationState|{phase,startedAt,finishedAt,message}}' > "$OUT/app.json"
kubectl -n $NS rollout status deploy/$DEP --timeout=600s >/dev/null
echo "done $LABEL" | tee -a "$OUT/meta.txt"
