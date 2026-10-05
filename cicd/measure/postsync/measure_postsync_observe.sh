#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
: "${KUBECONFIG:?Set KUBECONFIG for the target cluster}"
LABEL="${1:?usage: measure_postsync_observe.sh LABEL PUSH_ACK_EPOCH success|failure}"
PUSH_ACK="${2:?missing push acknowledgement epoch}"
EXPECTED="${3:?missing expected outcome}"
BASELINE_MODE="${4:-existing-app}"
APP_NS=argocd
APP="${POSTSYNC_APP:-argocd-postsync-smoke-20260930}"
NS=dev-back
JOB=argocd-postsync-smoke-20260930
DEPLOY=backend-dev-back
export APP JOB
POLL_SECONDS=0.5
TIMEOUT_SECONDS=180
OUT="$HERE/raw/postsync-20260930/$LABEL"

case "$EXPECTED" in success|failure) ;; *) echo "expected outcome must be success|failure" >&2; exit 2 ;; esac
if ! [[ "$PUSH_ACK" =~ ^[0-9]+([.][0-9]+)?$ ]]; then echo "invalid push acknowledgement epoch" >&2; exit 2; fi
if [ -e "$OUT" ]; then echo "Output already exists: $OUT" >&2; exit 2; fi
mkdir -p "$OUT"

now(){ python3 -c 'import time; print(f"{time.time():.6f}")'; }
elapsed(){ awk -v s="$PUSH_ACK" -v e="$1" 'BEGIN {if (e=="") print "NA"; else printf "%.3f", e-s}'; }
app_json(){ kubectl -n "$APP_NS" get application "$APP" -o json; }
job_json(){ kubectl -n "$NS" get job "$JOB" -o json 2>/dev/null || printf '{}'; }
deploy_json(){ kubectl -n "$NS" get deployment "$DEPLOY" -o json; }
snapshot_app(){
  app_json | jq '{name:.metadata.name,syncPolicy:.spec.syncPolicy,sync:.status.sync,health:.status.health,operation:{phase:.status.operationState.phase,startedAt:.status.operationState.startedAt,finishedAt:.status.operationState.finishedAt,message:.status.operationState.message,resources:[.status.operationState.syncResult.resources[]? | {kind,namespace,name,syncPhase,hookPhase,status,message}]}}'
}

BASE_APP="$(app_json)"
BASE_OP_START="$(jq -r '.status.operationState.startedAt // ""' <<< "$BASE_APP")"
REVISION="$(jq -r '.status.sync.revision // .status.sync.revisions[0] // ""' <<< "$BASE_APP")"
PRIOR_OP_START="$BASE_OP_START"
BASELINE_CURRENT_OPERATION=0
if [ "$BASELINE_MODE" = new-app ]; then BASE_OP_START=""; REVISION=""; fi
if [ "$BASELINE_MODE" = existing-app ] && [ -n "$BASE_OP_START" ]; then
  BASE_OP_EPOCH="$(python3 - "$BASE_OP_START" <<'PY'
import datetime, sys
print(datetime.datetime.fromisoformat(sys.argv[1].replace("Z", "+00:00")).timestamp())
PY
  )"
  if awk -v start="$BASE_OP_EPOCH" -v push="$PUSH_ACK" 'BEGIN {exit !(start >= push)}'; then
    BASE_OP_START=""
    BASELINE_CURRENT_OPERATION=1
  fi
fi
printf 'label=%s\nexpected=%s\napp=%s/%s\njob=%s/%s\npush_ack_epoch=%s\nbase_revision=%s\nprior_operation_started_at=%s\nobserver_baseline_operation_started_at=%s\noperation_already_newer_than_push_at_observer_start=%s\npoll_sleep_seconds=%s\nstarted_utc=%s\n' \
  "$LABEL" "$EXPECTED" "$APP_NS" "$APP" "$NS" "$JOB" "$PUSH_ACK" "$REVISION" "$PRIOR_OP_START" "$BASE_OP_START" "$BASELINE_CURRENT_OPERATION" "$POLL_SECONDS" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$OUT/meta.txt"
snapshot_app > "$OUT/app-before.json"
printf 'observed_epoch\tpush_elapsed_s\tapp_sync\tapp_health\toperation_phase\toperation_started_at\thook_phase\tjob_condition\tjob_created_at\tjob_completion_at\tbackend_ready\tbackend_ready_replicas\n' > "$OUT/timeline.tsv"

OP_START_SEEN=""
FINAL_APP=""
FINAL_JOB="{}"
FINAL_DEPLOY="{}"
DONE_EPOCH=""
DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  APP_JSON="$(app_json)"
  JOB_STATE="$(job_json)"
  DEPLOY_STATE="$(deploy_json)"
  OP_START="$(jq -r '.status.operationState.startedAt // ""' <<< "$APP_JSON")"
  PHASE="$(jq -r '.status.operationState.phase // ""' <<< "$APP_JSON")"
  SYNC="$(jq -r '.status.sync.status // "Unknown"' <<< "$APP_JSON")"
  HEALTH="$(jq -r '.status.health.status // "Unknown"' <<< "$APP_JSON")"
  HOOK_PHASE="$(jq -r '[.status.operationState.syncResult.resources[]? | select(.kind=="Job" and .name==env.JOB) | .hookPhase][0] // ""' <<< "$APP_JSON")"
  JOB_CONDITION="$(jq -r '[.status.conditions[]? | select(.type=="Complete" or .type=="Failed") | .type][0] // ""' <<< "$JOB_STATE")"
  JOB_CREATED="$(jq -r '.metadata.creationTimestamp // ""' <<< "$JOB_STATE")"
  JOB_COMPLETED="$(jq -r '.status.completionTime // ([.status.conditions[]? | select(.type=="Failed") | .lastTransitionTime][0] // "")' <<< "$JOB_STATE")"
  READY_REPLICAS="$(jq -r '.status.readyReplicas // 0' <<< "$DEPLOY_STATE")"
  DESIRED_REPLICAS="$(jq -r '.spec.replicas // 0' <<< "$DEPLOY_STATE")"
  BACKEND_READY=0
  if [ "$READY_REPLICAS" -ge 1 ] && [ "$READY_REPLICAS" -ge "$DESIRED_REPLICAS" ]; then BACKEND_READY=1; fi
  OBS="$(now)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$OBS" "$(elapsed "$OBS")" "$SYNC" "$HEALTH" "$PHASE" "$OP_START" "$HOOK_PHASE" "$JOB_CONDITION" "$JOB_CREATED" "$JOB_COMPLETED" "$BACKEND_READY" "$READY_REPLICAS" >> "$OUT/timeline.tsv"

  if [ -n "$OP_START" ] && [ "$OP_START" != "$BASE_OP_START" ] && { [ "$PHASE" = Succeeded ] || [ "$PHASE" = Failed ] || [ "$PHASE" = Error ]; }; then
    OP_START_SEEN="$OP_START"
    FINAL_APP="$APP_JSON"
    FINAL_JOB="$JOB_STATE"
    FINAL_DEPLOY="$DEPLOY_STATE"
    DONE_EPOCH="$OBS"
    if [ "$BASELINE_CURRENT_OPERATION" = 1 ]; then
      OP_FINISHED="$(jq -r '.status.operationState.finishedAt // ""' <<< "$APP_JSON")"
      if [ -n "$OP_FINISHED" ]; then
        DONE_EPOCH="$(python3 - "$OP_FINISHED" <<'PY'
import datetime, sys
print(f"{datetime.datetime.fromisoformat(sys.argv[1].replace('Z', '+00:00')).timestamp():.6f}")
PY
        )"
      fi
    fi
    break
  fi
  sleep "$POLL_SECONDS"
done

if [ -z "$DONE_EPOCH" ]; then
  FINAL_APP="$(app_json)"
  FINAL_JOB="$(job_json)"
  FINAL_DEPLOY="$(deploy_json)"
fi
snapshot_app > "$OUT/app-after.json"
printf '%s\n' "$FINAL_JOB" | jq '{metadata:{name:.metadata.name,namespace:.metadata.namespace,creationTimestamp:.metadata.creationTimestamp},spec:{backoffLimit:.spec.backoffLimit,activeDeadlineSeconds:.spec.activeDeadlineSeconds},status:.status}' > "$OUT/job-status.json"
printf '%s\n' "$FINAL_DEPLOY" | jq '{metadata:{name:.metadata.name},spec:{replicas:.spec.replicas},status:{observedGeneration:.status.observedGeneration,readyReplicas:.status.readyReplicas,availableReplicas:.status.availableReplicas,updatedReplicas:.status.updatedReplicas}}' > "$OUT/backend-deployment.json"
kubectl -n "$NS" get pods -l "job-name=$JOB" -o json > "$OUT/hook-pods.json" 2>&1 || true
kubectl -n "$NS" get job "$JOB" -o yaml > "$OUT/job.yaml" 2>&1 || true
kubectl -n "$NS" logs "job/$JOB" --all-containers=true > "$OUT/hook.log" 2>&1 || true
kubectl -n "$NS" get events --field-selector "involvedObject.name=$JOB" -o json > "$OUT/hook-events.json" 2>&1 || true
kubectl -n "$NS" exec "deploy/$DEPLOY" -- curl --silent --show-error --output /dev/null --write-out 'readiness_test_http_code=%{http_code}\n' http://127.0.0.1:8080/test > "$OUT/backend-readiness-check.txt" 2>&1 || true
kubectl -n "$NS" exec "deploy/$DEPLOY" -- curl --silent --show-error --output /dev/null --write-out 'smoke_root_http_code=%{http_code}\n' http://127.0.0.1:8080/ > "$OUT/backend-root-check.txt" 2>&1 || true

PHASE="$(jq -r '.status.operationState.phase // ""' <<< "$FINAL_APP")"
SYNC="$(jq -r '.status.sync.status // "Unknown"' <<< "$FINAL_APP")"
HEALTH="$(jq -r '.status.health.status // "Unknown"' <<< "$FINAL_APP")"
HOOK_PHASE="$(jq -r '[.status.operationState.syncResult.resources[]? | select(.kind=="Job" and .name==env.JOB) | .hookPhase][0] // ""' <<< "$FINAL_APP")"
READY_REPLICAS="$(jq -r '.status.readyReplicas // 0' <<< "$FINAL_DEPLOY")"
DESIRED_REPLICAS="$(jq -r '.spec.replicas // 0' <<< "$FINAL_DEPLOY")"
BACKEND_READY=0
if [ "$READY_REPLICAS" -ge 1 ] && [ "$READY_REPLICAS" -ge "$DESIRED_REPLICAS" ]; then BACKEND_READY=1; fi
PUSH_TO_DONE="NA"
HOOK_DURATION="NA"
if [ -n "$DONE_EPOCH" ]; then PUSH_TO_DONE="$(elapsed "$DONE_EPOCH")"; fi
JOB_CREATED="$(jq -r '.metadata.creationTimestamp // ""' <<< "$FINAL_JOB")"
JOB_FINISHED="$(jq -r '.status.completionTime // ([.status.conditions[]? | select(.type=="Failed") | .lastTransitionTime][0] // "")' <<< "$FINAL_JOB")"
if [ -n "$JOB_CREATED" ] && [ -n "$JOB_FINISHED" ]; then
  HOOK_DURATION="$(python3 - "$JOB_CREATED" "$JOB_FINISHED" <<'PY'
import datetime, sys
def parse(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
print(f"{parse(sys.argv[2]) - parse(sys.argv[1]):.3f}")
PY
  )"
fi
printf 'expected=%s\noperation_phase=%s\napp_sync=%s\napp_health=%s\nhook_phase=%s\njob_condition=%s\nbackend_ready=%s\nbackend_ready_replicas=%s\npush_to_operation_terminal_s=%s\nhook_job_duration_s=%s\noperation_started_at=%s\noperation_finished_at=%s\njob_created_at=%s\njob_finished_at=%s\ncompleted_observed_epoch=%s\n' \
  "$EXPECTED" "$PHASE" "$SYNC" "$HEALTH" "$HOOK_PHASE" "$(jq -r '[.status.conditions[]? | select(.type=="Complete" or .type=="Failed") | .type][0] // ""' <<< "$FINAL_JOB")" \
  "$BACKEND_READY" "$READY_REPLICAS" "$PUSH_TO_DONE" "$HOOK_DURATION" "$OP_START_SEEN" \
  "$(jq -r '.status.operationState.finishedAt // ""' <<< "$FINAL_APP")" "$JOB_CREATED" "$JOB_FINISHED" "$DONE_EPOCH" > "$OUT/results.txt"

if [ -z "$DONE_EPOCH" ] || [ "$BACKEND_READY" != 1 ]; then echo "observation timeout or backend not Ready; inspect $OUT" >&2; exit 4; fi
if [ "$EXPECTED" = success ] && { [ "$PHASE" != Succeeded ] || [ "$HOOK_PHASE" != Succeeded ] || [ "$SYNC" != Synced ] || [ "$HEALTH" != Healthy ]; }; then
  echo "success expectation failed; inspect $OUT" >&2; exit 5
fi
if [ "$EXPECTED" = failure ] && { [ "$PHASE" != Failed ] || [ "$HOOK_PHASE" != Failed ]; }; then
  echo "failure expectation failed; inspect $OUT" >&2; exit 5
fi
printf '%s: operation=%s hook=%s app=%s/%s backendReady=%s pushToTerminal=%ss hookDuration=%ss\n' \
  "$LABEL" "$PHASE" "$HOOK_PHASE" "$SYNC" "$HEALTH" "$BACKEND_READY" "$PUSH_TO_DONE" "$HOOK_DURATION"
