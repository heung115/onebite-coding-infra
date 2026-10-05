#!/usr/bin/env bash
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
source "$HERE/common.sh"

LABEL="${1:?usage: measure_prune_observe.sh LABEL PUSH_ACK_EPOCH add|control|restore|prune}"
PUSH_ACK="${2:?missing push ack epoch}"
MODE="${3:?missing mode}"
APP_NS=argocd
APP=argocd-prune-probe-20260930
NS=dev-back
CM=argocd-prune-probe-20260930
POLL_SECONDS=0.5
TIMEOUT_SECONDS=180
CONTROL_HOLD_SECONDS=30
OUT="$HERE/raw/prune-20260930/$LABEL"

case "$MODE" in add|control|restore|prune) ;; *) echo "invalid mode: $MODE" >&2; exit 2 ;; esac
if [ -e "$OUT" ]; then echo "Output already exists: $OUT" >&2; exit 2; fi
if ! [[ "$PUSH_ACK" =~ ^[0-9]+([.][0-9]+)?$ ]]; then echo "invalid push ack epoch" >&2; exit 2; fi
mkdir -p "$OUT"

now(){ python3 -c 'import time; print(f"{time.time():.6f}")'; }
elapsed(){ awk -v s="$PUSH_ACK" -v e="$1" 'BEGIN {if (e=="") print "NA"; else printf "%.3f", e-s}'; }
app_json(){ kubectl -n "$APP_NS" get application "$APP" -o json; }
app_snapshot(){
  app_json | jq --arg cm "$CM" '{name:.metadata.name, syncPolicy:.spec.syncPolicy, sync:.status.sync, health:.status.health, operation:{phase:.status.operationState.phase,startedAt:.status.operationState.startedAt,finishedAt:.status.operationState.finishedAt,message:.status.operationState.message,pruneResult:[.status.operationState.syncResult.resources[]? | select(.kind=="ConfigMap" and .name==$cm) | {kind,namespace,name,status,message,syncPhase}]}}'
}
cm_present(){ kubectl -n "$NS" get configmap "$CM" -o name >/dev/null 2>&1; }

START_APP="$(app_json)"
BASE_OP_START="$(jq -r '.status.operationState.startedAt // ""' <<< "$START_APP")"
BASE_REV="$(jq -r '.status.sync.revisions[0] // .status.sync.revision // ""' <<< "$START_APP")"
BASE_CM=0
if cm_present; then BASE_CM=1; fi
if [[ "$MODE" = control || "$MODE" = prune ]] && [ "$BASE_CM" != 1 ]; then
  echo "precondition failed: ConfigMap is absent before $MODE" >&2
  exit 3
fi
if [[ "$MODE" = restore ]] && [ "$BASE_CM" != 0 ]; then
  echo "precondition failed: ConfigMap is already present before $MODE" >&2
  exit 3
fi

printf 'label=%s\nmode=%s\napp=%s/%s\nconfigmap=%s/%s\npush_ack_epoch=%s\nbase_revision=%s\nbase_operation_started_at=%s\nbase_configmap_present=%s\npoll_sleep_seconds=%s\nstarted_utc=%s\n' \
  "$LABEL" "$MODE" "$APP_NS" "$APP" "$NS" "$CM" "$PUSH_ACK" "$BASE_REV" "$BASE_OP_START" "$BASE_CM" \
  "$POLL_SECONDS" "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" > "$OUT/meta.txt"
app_snapshot > "$OUT/app-before.json"
printf 'observed_at\tapp_sync\tapp_health\toperation_phase\toperation_started_at\tapp_revision\tconfigmap_present\n' > "$OUT/timeline.tsv"

T_OOS=""; T_OPERATION=""; T_CM_CHANGE=""; T_DONE=""; OP_START_SEEN=""
LAST_SYNC=""; LAST_HEALTH=""; LAST_PHASE=""; LAST_OP_START=""; LAST_REV=""; LAST_PRESENT=""
DEADLINE=$((SECONDS + TIMEOUT_SECONDS))
while [ "$SECONDS" -lt "$DEADLINE" ]; do
  APP_JSON="$(app_json)"
  SYNC="$(jq -r '.status.sync.status // "Unknown"' <<< "$APP_JSON")"
  HEALTH="$(jq -r '.status.health.status // "Unknown"' <<< "$APP_JSON")"
  PHASE="$(jq -r '.status.operationState.phase // ""' <<< "$APP_JSON")"
  OP_START="$(jq -r '.status.operationState.startedAt // ""' <<< "$APP_JSON")"
  REV="$(jq -r '.status.sync.revisions[0] // .status.sync.revision // ""' <<< "$APP_JSON")"
  PRESENT=0
  if cm_present; then PRESENT=1; fi
  OBS="$(now)"
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$OBS" "$SYNC" "$HEALTH" "$PHASE" "$OP_START" "$REV" "$PRESENT" >> "$OUT/timeline.tsv"
  LAST_SYNC="$SYNC"; LAST_HEALTH="$HEALTH"; LAST_PHASE="$PHASE"; LAST_OP_START="$OP_START"; LAST_REV="$REV"; LAST_PRESENT="$PRESENT"

  if [ -z "$T_OOS" ] && [ "$SYNC" = OutOfSync ]; then T_OOS="$OBS"; fi
  if [ -z "$T_OPERATION" ] && [ -n "$OP_START" ] && [ "$OP_START" != "$BASE_OP_START" ]; then
    T_OPERATION="$OBS"
    OP_START_SEEN="$OP_START"
  fi
  if [ "$MODE" = add ] || [ "$MODE" = restore ]; then
    if [ -z "$T_CM_CHANGE" ] && [ "$PRESENT" = 1 ]; then T_CM_CHANGE="$OBS"; fi
    if [ "$PRESENT" = 1 ] && [ "$SYNC" = Synced ] && [ "$HEALTH" = Healthy ] && [ "$PHASE" = Succeeded ]; then T_DONE="$OBS"; break; fi
  elif [ "$MODE" = control ]; then
    if [ -n "$T_OOS" ] && [ "$PRESENT" = 0 ]; then echo "control failed: ConfigMap was pruned despite prune=false" >&2; break; fi
    if [ -n "$T_OOS" ] && [ "$OP_START" != "$BASE_OP_START" ]; then echo "control failed: an automated sync started while prune=false" >&2; break; fi
    if [ -n "$T_OOS" ] && awk -v s="$T_OOS" -v e="$OBS" -v hold="$CONTROL_HOLD_SECONDS" 'BEGIN {exit !((e-s)>=hold)}'; then T_DONE="$OBS"; break; fi
  elif [ "$MODE" = prune ]; then
    if [ -z "$T_CM_CHANGE" ] && [ "$PRESENT" = 0 ]; then T_CM_CHANGE="$OBS"; fi
    if [ -n "$T_OPERATION" ] && [ -n "$T_CM_CHANGE" ] && [ "$SYNC" = Synced ] && [ "$HEALTH" = Healthy ] && [ "$PHASE" = Succeeded ]; then T_DONE="$OBS"; break; fi
  fi
  sleep "$POLL_SECONDS"
done

app_snapshot > "$OUT/app-after.json"
printf 'out_of_sync_observed=%s\noperation_started_observed=%s\nargocd_operation_started_at=%s\nconfigmap_state_change_observed=%s\ncompleted_observed=%s\nfinal_sync=%s\nfinal_health=%s\nfinal_operation_phase=%s\nfinal_operation_started_at=%s\nfinal_app_revision=%s\nfinal_configmap_present=%s\n' \
  "${T_OOS:-NA}" "${T_OPERATION:-NA}" "${OP_START_SEEN:-NA}" "${T_CM_CHANGE:-NA}" "${T_DONE:-NA}" \
  "$LAST_SYNC" "$LAST_HEALTH" "$LAST_PHASE" "$LAST_OP_START" "$LAST_REV" "$LAST_PRESENT" >> "$OUT/meta.txt"
case "$MODE" in
  add|restore)
    printf 'mode\tpush_to_configmap_present_s\tpush_to_synced_healthy_s\n%s\t%s\t%s\n' "$MODE" "$(elapsed "$T_CM_CHANGE")" "$(elapsed "$T_DONE")" > "$OUT/results.tsv"
    ;;
  control)
    local_oos="NA"; [ -n "$T_OOS" ] && local_oos="$(awk -v s="$PUSH_ACK" -v e="$T_OOS" 'BEGIN {printf "%.3f", e-s}')"
    local_hold="NA"; [ -n "$T_OOS" ] && [ -n "$T_DONE" ] && local_hold="$(awk -v s="$T_OOS" -v e="$T_DONE" 'BEGIN {printf "%.3f", e-s}')"
    printf 'mode\tpush_to_out_of_sync_s\tretained_after_out_of_sync_s\toperation_started\tconfigmap_present\ncontrol\t%s\t%s\t%s\t%s\n' \
      "$local_oos" "$local_hold" "$([ "$OP_START_SEEN" = "" ] && echo no || echo yes)" "$LAST_PRESENT" > "$OUT/results.tsv"
    ;;
  prune)
    oos="$(elapsed "$T_OOS")"; operation="$(elapsed "$T_OPERATION")"; deletion="$(elapsed "$T_CM_CHANGE")"; done="$(elapsed "$T_DONE")"
    oos_to_delete=NA
    if [ -n "$T_OOS" ] && [ -n "$T_CM_CHANGE" ]; then oos_to_delete="$(awk -v s="$T_OOS" -v e="$T_CM_CHANGE" 'BEGIN {printf "%.3f", e-s}')"; fi
    printf 'mode\tpush_to_out_of_sync_s\tpush_to_prune_operation_s\tpush_to_configmap_deleted_s\tout_of_sync_to_deleted_s\tpush_to_synced_healthy_s\nprune\t%s\t%s\t%s\t%s\t%s\n' \
      "$oos" "$operation" "$deletion" "$oos_to_delete" "$done" > "$OUT/results.tsv"
    ;;
esac

if [ -z "$T_DONE" ]; then echo "$MODE observation timed out or failed; inspect $OUT" >&2; exit 4; fi
echo "$MODE: outOfSync=$(elapsed "$T_OOS")s operation=$(elapsed "$T_OPERATION")s configMapChange=$(elapsed "$T_CM_CHANGE")s complete=$(elapsed "$T_DONE")s final=$LAST_SYNC/$LAST_HEALTH cmPresent=$LAST_PRESENT"
