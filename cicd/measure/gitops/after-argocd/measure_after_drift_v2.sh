#!/usr/bin/env bash
# After(ArgoCD) 드리프트 v2: before 와 같은 수동 변경(replicas 1->2, env DRIFT_TEST 추가) 후
# replicas 원복 시각과 env 원복 여부를 따로 기록한다. (v1 은 두 조건을 AND 로 기다려 env 미원복 시 타임아웃)
set -euo pipefail
LABEL="${@: -1}"
OUT="$(cd "$(dirname "$0")" && pwd)/raw/$LABEL"; mkdir -p "$OUT"
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
envcnt(){ kubectl -n $NS get deploy $DEP -o json | jq -r '[.spec.template.spec.containers[0].env[]? | select(.name=="DRIFT_TEST")] | length'; }
# 사전 조건: 드리프트 없음
[ "$(envcnt)" = 0 ] || { kubectl -n $NS set env deploy/$DEP DRIFT_TEST- >/dev/null; kubectl -n $NS rollout status deploy/$DEP --timeout=600s >/dev/null; }
kubectl -n $NS rollout status deploy/$DEP --timeout=600s >/dev/null
echo "pre: replicas=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.replicas}') env=$(envcnt) app=$(app_state)" | tee "$OUT/meta.txt"
kubectl -n $NS scale deploy/$DEP --replicas=2 >/dev/null
kubectl -n $NS set env deploy/$DEP DRIFT_TEST=manual-edit >/dev/null
T_EDIT=$(now); echo "t_edit=$T_EDIT" | tee -a "$OUT/meta.txt"
T_REP=""
for i in $(seq 1 600); do
  rep=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.replicas}')
  if [ "$rep" = 1 ]; then T_REP=$(now); break; fi
  sleep 0.5
done
echo "t_replicas_reverted=$T_REP" | tee -a "$OUT/meta.txt"
# 기본 reconciliation(120s + jitter 60s) 한 주기 이상 기다린 뒤 env 상태 확인
sleep 200
echo "env_after_200s=$(envcnt) app_after_200s=$(app_state)" | tee -a "$OUT/meta.txt"
kubectl -n argocd get application $APP -o json | jq '{sync:.status.sync.status, health:.status.health.status, op:.status.operationState|{phase,startedAt,finishedAt,message}}' > "$OUT/app.json"
# 정리: 남은 env 제거 (측정 구간 밖)
kubectl -n $NS set env deploy/$DEP DRIFT_TEST- >/dev/null || true
kubectl -n $NS rollout status deploy/$DEP --timeout=600s >/dev/null
echo "done $LABEL" | tee -a "$OUT/meta.txt"
