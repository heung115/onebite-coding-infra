#!/usr/bin/env bash
# ArgoCD 최적화 측정 배치 (launchd 로 실행). 같은 스크립트·트리거·측정 구간, VM 6.5GB, 모니터링 off.
#  opt1 = reconciliation 30s(jitter 0) + repo-server --revision-cache-expiration=30s
#  opt2 = opt1 + backend Deployment progressDeadlineSeconds 180 (Git 차트 값)
set -u
M="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$M/rerun-6.5g"
PH="${1:-opt1}"
LOG=$M/rerun-6.5g/opt-batch.log
log(){ echo "$(date '+%F %T') $*" >> $LOG; }
run(){ local lbl="$1"; shift; log "start $lbl"; sleep 20; if "$@" "$lbl" > "$M/rerun-6.5g/$lbl.out" 2>&1; then log "ok $lbl"; else log "FAIL $lbl rc=$?"; fi; }
guard(){ local g l; for g in $(seq 1 40); do l=$(timeout 20 ssh -o BatchMode=yes -o ConnectTimeout=10 "${HOMESERVER_SSH_ALIAS:?Set HOMESERVER_SSH_ALIAS}" 'cut -d" " -f1 /proc/loadavg' 2>/dev/null || echo 99); awk "BEGIN{exit !($l < 8)}" && return 0; log "guard: load=$l waiting"; sleep 15; done; log "guard: still overloaded"; }
cd $M/after-argocd
if [ "$PH" = opt1 ]; then
  for n in 1 2 3; do guard; run o1-deploy-$n ./measure_after_deploy.sh; done
  for n in 1 2 3; do guard; run o1-failure-gitops-$n ./measure_after_failure.sh gitops; done
  for n in 1 2 3; do guard; run o1-failure-app-$n ./measure_after_failure.sh app; done
else
  for n in 1 2 3; do guard; run o2-failure-app-$n ./measure_after_failure.sh app; done
fi
log "PHASE-DONE $PH"
