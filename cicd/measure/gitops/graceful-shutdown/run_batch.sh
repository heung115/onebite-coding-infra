#!/usr/bin/env bash
# preStop 없음 3회 → preStop sleep 10만 추가 3회. 같은 클러스터와 부하 조건을 유지한다.
# 세션과 분리해서 돌린다: nohup ./run_batch.sh > raw/batch.log 2>&1 &
set -uo pipefail
cd "$(dirname "$0")"
: "${KUBECONFIG:?Set KUBECONFIG}"
: "${KUBE_CONTEXT:?Set KUBE_CONTEXT}"
K=(kubectl --kubeconfig "$KUBECONFIG" --context "$KUBE_CONTEXT")
NS="${NAMESPACE:-dev-back}"; DEP=backend-dev-back
log(){ echo "$(date '+%F %T') $*"; }
run(){ log "start $1"; if ./run_one.sh "$1"; then log "ok $1"; else log "FAIL $1 rc=$?"; fi; sleep 45; }

log "phase A: original chart (no preStop, grace 30)"
for i in 1 2 3; do run "base-$i"; done

log "phase B: add preStop sleep 10 only (grace 30 unchanged)"
"${K[@]}" -n $NS patch deploy $DEP --type=json -p='[{"op":"add","path":"/spec/template/spec/containers/0/lifecycle","value":{"preStop":{"exec":{"command":["/bin/sh","-c","sleep 10"]}}}}]'
"${K[@]}" -n $NS rollout status deploy/$DEP --timeout=300s
sleep 45
for i in 1 2 3; do run "prestop10-$i"; done
log "BATCH-DONE"
