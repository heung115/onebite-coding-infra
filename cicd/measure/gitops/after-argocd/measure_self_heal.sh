#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"

APP_NS=argocd
APP=backend-dev-back
NS=dev-back
DEP=backend-dev-back
EXPECTED_REPLICAS=1
POLL_SECONDS=0.5
TIMEOUT_SECONDS=180
LABEL="${1:-self-heal-$(date -u +%Y%m%dT%H%M%SZ)}"
OUT="$HERE/raw/$LABEL"
if [ -e "$OUT" ]; then
  echo "Output already exists; choose a new label: $OUT" >&2
  exit 2
fi
mkdir -p "$OUT"

now(){ python3 -c 'import time; print(f"{time.time():.6f}")'; }
app_state(){ kubectl -n "$APP_NS" get application "$APP" -o jsonpath='{.status.sync.status}/{.status.health.status}'; }
replicas(){ kubectl -n "$NS" get deployment "$DEP" -o jsonpath='{.spec.replicas}'; }
remote_main(){ git -C "$GSRC" ls-remote origin refs/heads/main | awk 'NR==1 {print $1}'; }
duration(){
  local end="$1"
  if [ -z "$end" ]; then printf 'NA'; else awk -v s="$T0" -v e="$end" 'BEGIN {printf "%.3f", e-s}'; fi
}
app_snapshot(){
  kubectl -n "$APP_NS" get application "$APP" -o json |
    jq '{name:.metadata.name, syncPolicy:.spec.syncPolicy, sync:.status.sync, health:.status.health, operationState:.status.operationState|{phase,startedAt,finishedAt,message}}'
}
deployment_snapshot(){
  kubectl -n "$NS" get deployment "$DEP" -o json |
    jq '{name:.metadata.name, generation:.metadata.generation, resourceVersion:.metadata.resourceVersion, specReplicas:.spec.replicas, status:{observedGeneration:.status.observedGeneration,replicas:.status.replicas,updatedReplicas:.status.updatedReplicas,readyReplicas:.status.readyReplicas,availableReplicas:.status.availableReplicas}}'
}

BASE_REMOTE_REV="$(remote_main)"
BASE_LOCAL_HEAD="$(git -C "$GSRC" rev-parse HEAD)"
BASE_APP_REV="$(kubectl -n "$APP_NS" get application "$APP" -o jsonpath='{.status.sync.revisions[0]}')"
BASE_STATE="$(app_state)"
BASE_REPLICAS="$(replicas)"
HPA_COUNT="$(kubectl -n "$NS" get hpa -o name 2>/dev/null | wc -l | tr -d ' ')"
LOCAL_STATUS="$(git -C "$GSRC" status --porcelain)"

if [ -z "$BASE_REMOTE_REV" ] || [ "$BASE_APP_REV" != "$BASE_REMOTE_REV" ]; then
  echo "Preflight failed: ArgoCD revision does not match GitHub main" >&2
  echo "remote=$BASE_REMOTE_REV app=$BASE_APP_REV" >&2
  exit 2
fi
if [ "$BASE_STATE" != "Synced/Healthy" ] || [ "$BASE_REPLICAS" != "$EXPECTED_REPLICAS" ] || [ "$HPA_COUNT" != "0" ] || [ -n "$LOCAL_STATUS" ]; then
  echo "Preflight failed: app/deployment/repo is not in the expected stable state" >&2
  echo "state=$BASE_STATE replicas=$BASE_REPLICAS hpaCount=$HPA_COUNT localGitStatus=${LOCAL_STATUS:-clean}" >&2
  exit 2
fi

printf 'label=%s\napp=%s/%s\ndeployment=%s/%s\nexpected_replicas=%s\nremote_main=%s\nlocal_checkout_head=%s\nargocd_revision=%s\npoll_seconds=%s\ntimeout_seconds=%s\nstarted_utc=%s\n' \
  "$LABEL" "$APP_NS" "$APP" "$NS" "$DEP" "$EXPECTED_REPLICAS" "$BASE_REMOTE_REV" "$BASE_LOCAL_HEAD" "$BASE_APP_REV" \
  "$POLL_SECONDS" "$TIMEOUT_SECONDS" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$OUT/meta.txt"
printf 'bash %s %s\n' "$0" "$LABEL" > "$OUT/commands.txt"
printf '%s\n' \
  'git -C "$GSRC" ls-remote origin refs/heads/main' \
  'git -C "$GSRC" rev-parse HEAD' \
  'kubectl -n argocd get application backend-dev-back -o json' \
  'kubectl -n dev-back get deployment backend-dev-back -o json' \
  'kubectl -n dev-back scale deployment/backend-dev-back --replicas=2' \
  'poll Application sync/health/operationState and Deployment spec/status every 0.5s' \
  'kubectl -n dev-back rollout status deployment/backend-dev-back --timeout=180s' >> "$OUT/commands.txt"
app_snapshot > "$OUT/app-before.json"
deployment_snapshot > "$OUT/deployment-before.json"
printf 'observed_at\tapp_sync\tapp_health\toperation_phase\toperation_started_at\tdeployment_generation\tobserved_generation\tspec_replicas\tstatus_replicas\tupdated_replicas\tready_replicas\tavailable_replicas\n' > "$OUT/timeline.tsv"
printf 'run\tout_of_sync_s\tself_heal_start_s\tspec_replica_1_s\tsettled_replica_1_s\tsynced_healthy_s\tsuccess\n' > "$OUT/results.tsv"

CURRENT_RUN=0
cleanup(){
  local current remote
  [ "$CURRENT_RUN" -gt 0 ] || return 0
  current="$(replicas 2>/dev/null || true)"
  remote="$(remote_main 2>/dev/null || true)"
  if [ "$remote" = "$BASE_REMOTE_REV" ] && [ "$current" != "$EXPECTED_REPLICAS" ]; then
    echo "cleanup: restoring replicas to $EXPECTED_REPLICAS" >&2
    kubectl -n "$NS" scale "deployment/$DEP" --replicas="$EXPECTED_REPLICAS" >/dev/null || true
  fi
  if [ "$remote" = "$BASE_REMOTE_REV" ]; then
    kubectl -n "$NS" rollout status "deployment/$DEP" --timeout=180s >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

for RUN in 1 2 3; do
  CURRENT_RUN="$RUN"
  state="$(app_state)"
  count="$(replicas)"
  if [ "$state" != "Synced/Healthy" ] || [ "$count" != "$EXPECTED_REPLICAS" ]; then
    echo "Pre-run $RUN failed: state=$state replicas=$count" >&2
    exit 3
  fi
  base_op_start="$(kubectl -n "$APP_NS" get application "$APP" -o jsonpath='{.status.operationState.startedAt}')"
  run_dir="$OUT/run-$RUN"
  mkdir -p "$run_dir"
  printf 'run=%s\npre_state=%s\npre_replicas=%s\npre_operation_started_at=%s\n' "$RUN" "$state" "$count" "$base_op_start" > "$run_dir/meta.txt"

  T_SCALE_START="$(now)"
  kubectl -n "$NS" scale "deployment/$DEP" --replicas=2 | tee "$run_dir/scale.out"
  T0="$(now)"
  printf 'scale_request_started=%s\ndrift_t0_api_ack=%s\n' "$T_SCALE_START" "$T0" | tee -a "$run_dir/meta.txt" >> "$OUT/meta.txt"

  T_OOS=""; T_SELFHEAL=""; T_SPEC_ONE=""; T_SETTLED=""; T_DONE=""
  OP_START_SEEN=""
  DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
  while [ "$SECONDS" -lt "$DEADLINE" ]; do
    app_line="$(kubectl -n "$APP_NS" get application "$APP" -o jsonpath='{.status.sync.status}{"\t"}{.status.health.status}{"\t"}{.status.operationState.phase}{"\t"}{.status.operationState.startedAt}')"
    deploy_line="$(kubectl -n "$NS" get deployment "$DEP" -o jsonpath='{.metadata.generation}{"\t"}{.status.observedGeneration}{"\t"}{.spec.replicas}{"\t"}{.status.replicas}{"\t"}{.status.updatedReplicas}{"\t"}{.status.readyReplicas}{"\t"}{.status.availableReplicas}')"
    OBS="$(now)"
    IFS=$'\t' read -r SYNC HEALTH PHASE OP_START <<< "$app_line"
    IFS=$'\t' read -r GEN OBS_GEN SPEC_REP STATUS_REP UPDATED_REP READY_REP AVAILABLE_REP <<< "$deploy_line"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$OBS" "$SYNC" "$HEALTH" "$PHASE" "$OP_START" "$GEN" "$OBS_GEN" "$SPEC_REP" "$STATUS_REP" "$UPDATED_REP" "$READY_REP" "$AVAILABLE_REP" >> "$OUT/timeline.tsv"
    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
      "$OBS" "$SYNC" "$HEALTH" "$PHASE" "$OP_START" "$GEN" "$OBS_GEN" "$SPEC_REP" "$STATUS_REP" "$UPDATED_REP" "$READY_REP" "$AVAILABLE_REP" >> "$run_dir/timeline.tsv"

    if [ -z "$T_OOS" ] && [ "$SYNC" = "OutOfSync" ]; then T_OOS="$OBS"; fi
    if [ -z "$T_SELFHEAL" ] && [ -n "$OP_START" ] && [ "$OP_START" != "$base_op_start" ]; then
      T_SELFHEAL="$OBS"
      OP_START_SEEN="$OP_START"
    fi
    if [ -z "$T_SPEC_ONE" ] && [ "$SPEC_REP" = "$EXPECTED_REPLICAS" ]; then T_SPEC_ONE="$OBS"; fi
    if [ -z "$T_SETTLED" ] && [ "$SPEC_REP" = "$EXPECTED_REPLICAS" ] && [ "$OBS_GEN" = "$GEN" ] && \
       [ "$STATUS_REP" = "$EXPECTED_REPLICAS" ] && [ "$UPDATED_REP" = "$EXPECTED_REPLICAS" ] && \
       [ "$READY_REP" = "$EXPECTED_REPLICAS" ] && [ "$AVAILABLE_REP" = "$EXPECTED_REPLICAS" ]; then T_SETTLED="$OBS"; fi
    if [ -z "$T_DONE" ] && [ "$SYNC" = "Synced" ] && [ "$HEALTH" = "Healthy" ] && [ -n "$T_SETTLED" ]; then
      T_DONE="$OBS"
      break
    fi
    sleep "$POLL_SECONDS"
  done

  if [ -z "$T_DONE" ]; then
    echo "Run $RUN timed out before Synced/Healthy" >&2
    kubectl -n "$NS" scale "deployment/$DEP" --replicas="$EXPECTED_REPLICAS" >/dev/null || true
    exit 4
  fi

  printf 'out_of_sync_observed=%s\nself_heal_operation_observed=%s\nargocd_operation_started_at=%s\nspec_replica_1_observed=%s\nsettled_replica_1_observed=%s\nsynced_healthy_observed=%s\n' \
    "${T_OOS:-NA}" "${T_SELFHEAL:-NA}" "${OP_START_SEEN:-NA}" "${T_SPEC_ONE:-NA}" "${T_SETTLED:-NA}" "$T_DONE" >> "$run_dir/meta.txt"
  app_snapshot > "$run_dir/app-after.json"
  deployment_snapshot > "$run_dir/deployment-after.json"
  kubectl -n "$NS" rollout status "deployment/$DEP" --timeout=180s > "$run_dir/rollout-status.out"

  SUCCESS=1
  [ -n "$T_OOS" ] && [ -n "$T_SELFHEAL" ] && [ -n "$T_SPEC_ONE" ] && [ -n "$T_SETTLED" ] || SUCCESS=0
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$RUN" "$(duration "$T_OOS")" "$(duration "$T_SELFHEAL")" \
    "$(duration "$T_SPEC_ONE")" "$(duration "$T_SETTLED")" "$(duration "$T_DONE")" "$SUCCESS" >> "$OUT/results.tsv"
  printf 'run=%s outOfSync=%ss selfHealStart=%ss replicas1=%ss settled=%ss SyncedHealthy=%ss success=%s\n' \
    "$RUN" "$(duration "$T_OOS")" "$(duration "$T_SELFHEAL")" "$(duration "$T_SPEC_ONE")" \
    "$(duration "$T_SETTLED")" "$(duration "$T_DONE")" "$SUCCESS"

  # Let the Application and Deployment settle before the next independent drift.
  sleep 2
done

FINAL_REMOTE_REV="$(remote_main)"
FINAL_LOCAL_HEAD="$(git -C "$GSRC" rev-parse HEAD)"
FINAL_APP_REV="$(kubectl -n "$APP_NS" get application "$APP" -o jsonpath='{.status.sync.revisions[0]}')"
FINAL_STATE="$(app_state)"
FINAL_REPLICAS="$(replicas)"
printf 'finished_utc=%s\nfinal_remote_main=%s\nfinal_local_checkout_head=%s\nfinal_argocd_revision=%s\nfinal_state=%s\nfinal_replicas=%s\n' \
  "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$FINAL_REMOTE_REV" "$FINAL_LOCAL_HEAD" "$FINAL_APP_REV" "$FINAL_STATE" "$FINAL_REPLICAS" >> "$OUT/meta.txt"
app_snapshot > "$OUT/app-after.json"
deployment_snapshot > "$OUT/deployment-after.json"

python3 - "$OUT/results.tsv" <<'PY'
import csv, statistics, sys
with open(sys.argv[1], newline="") as f:
    rows = list(csv.DictReader(f, delimiter="\t"))
print("median_and_range_seconds:")
for column in ("out_of_sync_s", "self_heal_start_s", "spec_replica_1_s", "settled_replica_1_s", "synced_healthy_s"):
    values = [float(row[column]) for row in rows if row[column] != "NA"]
    if values:
        print(f"  {column}: median={statistics.median(values):.3f} range={min(values):.3f}-{max(values):.3f} n={len(values)}")
print(f"successes={sum(row['success'] == '1' for row in rows)}/{len(rows)}")
PY

if [ "$FINAL_REMOTE_REV" != "$BASE_REMOTE_REV" ] || [ "$FINAL_LOCAL_HEAD" != "$BASE_LOCAL_HEAD" ] || \
   [ "$FINAL_APP_REV" != "$BASE_APP_REV" ] || \
   [ "$FINAL_STATE" != "Synced/Healthy" ] || [ "$FINAL_REPLICAS" != "$EXPECTED_REPLICAS" ]; then
  echo "Final verification failed; inspect $OUT/meta.txt and raw snapshots" >&2
  exit 5
fi
if ! awk -F '\t' 'NR>1 && $7 != 1 {bad=1} END {exit bad}' "$OUT/results.tsv"; then
  echo "One or more runs did not observe every required state" >&2
  exit 6
fi
echo "All runs completed; remote Git revision unchanged and cluster is Synced/Healthy at replicas=$EXPECTED_REPLICAS"
