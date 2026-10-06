#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
WORKSPACE="phase3-spot-interruption-v8-prestop5-n10-20261003"
EXPERIMENT_TAG="phase3-spot-20261003-v8"
TEMPLATE_ID="EXT2tn1ZqqwRgtx8"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
BATCH_DIR="$ROOT/measure/results/20261003-phase3-prestop3-repro-n10"
EXECUTION_DIR="$BATCH_DIR/execution-01"
MANIFEST="$BATCH_DIR/config/spot-recovery-app-prestop3.yaml"
TRIAL_COUNT=10

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
if [[ "${ALLOW_PHASE3_BENCHMARK:-}" != "1" || "${1:-}" != "--execute" ]]; then
  printf 'Usage after approval: ALLOW_PHASE3_BENCHMARK=1 %s --execute\n' "$0" >&2
  exit 2
fi
if [[ ! -f "$KUBECONFIG_PATH" || ! -f "$MANIFEST" ]]; then
  printf 'The dedicated kubeconfig or preregistered workload manifest is missing.\n' >&2
  exit 3
fi
if [[ -e "$EXECUTION_DIR" ]]; then
  printf 'Refusing to overwrite an existing execution directory.\n' >&2
  exit 4
fi

export AWS_REGION="$REGION"
export TF_WORKSPACE="$WORKSPACE"
export PHASE3_TF_WORKSPACE="$WORKSPACE"
export PHASE3_EXPERIMENT_TAG="$EXPERIMENT_TAG"
export PHASE3_REACHABILITY_GATE_VERSION=7
export PHASE3_PRESTOP_SECONDS=3
export PHASE3_WORKLOAD_CONDITION=prestop3-window120
export KUBECONFIG="$KUBECONFIG_PATH"

account_id="$(aws sts get-caller-identity --region "$REGION" --query Account --output text)"
if [[ "${account_id: -4}" != "0497" ]]; then
  printf 'Refusing run: AWS account must end in 0497.\n' >&2
  exit 5
fi
if [[ "$(aws eks describe-cluster --name "$CLUSTER" --region "$REGION" --query cluster.status --output text)" != "ACTIVE" ]]; then
  printf 'Refusing run: the persistent EKS cluster is not ACTIVE.\n' >&2
  exit 7
fi
interruption_queue_url="$(aws sqs get-queue-url --queue-name onebite-eks-measure-karpenter-interruption --region "$REGION" --query QueueUrl --output text)"
audit_queue_url="$(aws sqs get-queue-url --queue-name onebite-eks-measure-phase3-event-audit --region "$REGION" --query QueueUrl --output text)"
if [[ -z "$interruption_queue_url" || -z "$audit_queue_url" ]]; then
  printf 'Persistent interruption queues are missing.\n' >&2
  exit 8
fi
template_json="$(aws fis get-experiment-template --id "$TEMPLATE_ID" --region "$REGION" --output json)"
if ! jq -e '
  .experimentTemplate.targets["phase3-spot-worker"].resourceType == "aws:ec2:spot-instance" and
  .experimentTemplate.targets["phase3-spot-worker"].selectionMode == "COUNT(1)" and
  .experimentTemplate.targets["phase3-spot-worker"].resourceTags.Project == "onebite" and
  .experimentTemplate.targets["phase3-spot-worker"].resourceTags.Temporary == "true" and
  .experimentTemplate.targets["phase3-spot-worker"].resourceTags["measure-experiment"] == "phase3-spot-20261003-v8" and
  (.experimentTemplate.actions | to_entries | length == 1) and
  .experimentTemplate.actions["interrupt-one-phase3-spot-worker"].actionId == "aws:ec2:send-spot-instance-interruptions" and
  .experimentTemplate.actions["interrupt-one-phase3-spot-worker"].parameters.durationBeforeInterruption == "PT2M"
' <<<"$template_json" >/dev/null; then
  printf 'Refusing run: persistent FIS template differs from preregistered PT2M/COUNT(1) configuration.\n' >&2
  exit 9
fi
active_fis="$(aws fis list-experiments --max-results 100 --region "$REGION" --output json | jq --arg template "$TEMPLATE_ID" '[.experiments[] | select(.experimentTemplateId == $template and (.state.status | IN("completed","failed","stopped","cancelled") | not))] | length')"
if [[ "$active_fis" != "0" ]]; then
  printf 'Refusing run: a previous FIS execution is not terminal.\n' >&2
  exit 10
fi
for queue_url in "$interruption_queue_url" "$audit_queue_url"; do
  queue_attributes="$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$queue_url" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --output json)"
  if ! jq -e '(.Attributes.ApproximateNumberOfMessages // "0" | tonumber) == 0 and (.Attributes.ApproximateNumberOfMessagesNotVisible // "0" | tonumber) == 0' <<<"$queue_attributes" >/dev/null; then
    printf 'Refusing run: an interruption/event-audit queue contains visible or in-flight messages.\n' >&2
    exit 11
  fi
done

mkdir -p "$EXECUTION_DIR"
control_log="$EXECUTION_DIR/batch-control.jsonl"
record() { "$ROOT/measure/scripts/record-command.sh" "$EXECUTION_DIR/prepare-commands.log" "$@"; }
jq -cn --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg manifest "$MANIFEST" --arg region "$REGION" \
  '{kind:"batch_started",observed_at_utc:$at,attempts_fixed:10,condition:"preStop sleep 3s",region:$region,cluster:"onebite-eks-measure",manifest:$manifest,manifest_sha256:"4758ed124ea9e17f834041d1327f1cd4a8288557e35088162bcd185f77c1f87d",terraform_apply:false,terraform_destroy:false,workload_apply:false}' > "$control_log"

# The persistent Deployment is deliberately not re-applied; the per-attempt
# read-only gate verifies the live spec before any FIS action can start.
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" rollout status deployment/backend-probe -n measure --timeout=15m

wait_for_fis_terminal() {
  local trial="$1" experiment_id="$2" deadline status response observed_at
  deadline=$((SECONDS + 900))
  while (( SECONDS < deadline )); do
    observed_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    if ! response="$(aws fis get-experiment --id "$experiment_id" --region "$REGION" --output json 2>&1)"; then
      jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$observed_at" --arg error "$response" \
        '{kind:"fis_status_error",trial:$trial,experiment_id:$id,observed_at_utc:$at,error:$error}' >> "$control_log"
      return 1
    fi
    status="$(jq -r '.experiment.state.status // "unknown"' <<<"$response")"
    jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$observed_at" --argjson payload "$response" \
      '{kind:"fis_status_poll",trial:$trial,experiment_id:$id,observed_at_utc:$at,response:$payload}' >> "$control_log"
    case "$status" in
      completed|failed|stopped|cancelled)
        jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$observed_at" --arg state "$status" \
          '{kind:"fis_terminal_confirmed",trial:$trial,experiment_id:$id,observed_at_utc:$at,status:$state}' >> "$control_log"
        return 0
        ;;
    esac
    sleep 5
  done
  jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{kind:"fis_terminal_timeout",trial:$trial,experiment_id:$id,observed_at_utc:$at,timeout_seconds:900}' >> "$control_log"
  return 1
}

failed_attempts=0
batch_stopped_reason=""
for trial in $(seq 1 "$TRIAL_COUNT"); do
  printf -v trial_id '%02d' "$trial"
  state_log="$EXECUTION_DIR/pre-attempt-fis-queue-${trial_id}.json"
  fis_state_error=""
  if ! fis_state="$(aws fis list-experiments --max-results 100 --region "$REGION" --output json 2>&1)"; then
    fis_state_error="AWS FIS list-experiments failed: $fis_state"
    fis_json='{}'
    active_count=1
  else
    fis_json="$fis_state"
    active_count="$(jq --arg template "$TEMPLATE_ID" '[.experiments[] | select(.experimentTemplateId == $template and (.state.status | IN("completed","failed","stopped","cancelled") | not))] | length' <<<"$fis_json")"
  fi
  if ! interruption_attributes="$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$interruption_queue_url" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --output json 2>&1)"; then
    fis_state_error="${fis_state_error:+$fis_state_error; }interruption queue attributes failed: $interruption_attributes"
    interruption_json='{}'
  else
    interruption_json="$interruption_attributes"
  fi
  if ! audit_attributes="$(aws sqs get-queue-attributes --region "$REGION" --queue-url "$audit_queue_url" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --output json 2>&1)"; then
    fis_state_error="${fis_state_error:+$fis_state_error; }audit queue attributes failed: $audit_attributes"
    audit_json='{}'
  else
    audit_json="$audit_attributes"
  fi
  interruption_depth="$(jq -r '[(.Attributes.ApproximateNumberOfMessages // "0"|tonumber),(.Attributes.ApproximateNumberOfMessagesNotVisible // "0"|tonumber)] | add' <<<"$interruption_json" 2>/dev/null || printf '1')"
  audit_depth="$(jq -r '[(.Attributes.ApproximateNumberOfMessages // "0"|tonumber),(.Attributes.ApproximateNumberOfMessagesNotVisible // "0"|tonumber)] | add' <<<"$audit_json" 2>/dev/null || printf '1')"
  jq -cn --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --argjson trial "$trial" \
    --argjson fis "$fis_json" --argjson interruption "$interruption_json" --argjson audit "$audit_json" \
    --argjson active "$active_count" --argjson interruption_depth "$interruption_depth" --argjson audit_depth "$audit_depth" \
    --arg error "$fis_state_error" \
    '{kind:"pre_attempt_fis_queue_snapshot",trial:$trial,observed_at_utc:$at,fis_experiments:$fis,active_matching_fis_count:$active,interruption_queue:$interruption,audit_queue:$audit,interruption_queue_total_depth:$interruption_depth,audit_queue_total_depth:$audit_depth,read_only:true,errors:(if $error=="" then [] else [$error] end),passed:($error=="" and $active==0 and $interruption_depth==0 and $audit_depth==0)}' > "$state_log"
  if ! jq -e '.passed == true' "$state_log" >/dev/null; then
    failed_attempts=$((failed_attempts + 1))
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg file "$state_log" \
      '{kind:"pre_attempt_fis_queue_gate_failed",trial:$trial,observed_at_utc:$at,gate_raw_file:$file,fis_started:false,error:"Earlier FIS is not terminal or one of the event queues is not empty/readable",replacement_attempt:false}' > "$EXECUTION_DIR/spot-interruption-${trial_id}.failure.json"
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg file "$state_log" \
      '{kind:"attempt_failed_before_fis",trial:$trial,observed_at_utc:$at,gate_raw_file:$file,fis_started:false}' > "$EXECUTION_DIR/spot-interruption-${trial_id}.jsonl"
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg file "$state_log" \
      '{kind:"fixed_attempt_end",trial:$trial,observed_at_utc:$at,result:"failed_pre_fis_terminal_or_queue_gate",gate_raw_file:$file,fis_experiment_id:null}' >> "$control_log"
    continue
  fi

  steady_log="$EXECUTION_DIR/pre-attempt-steady-state-${trial_id}.jsonl"
  set +e
  python3 "$BATCH_DIR/check-ready-pod-target-set.py" --attempt "$trial" --output "$steady_log" --timeout-seconds 900 --interval-seconds 15
  steady_status=$?
  set -e

  summary="$EXECUTION_DIR/spot-interruption-${trial_id}.json"
  failure_file="$EXECUTION_DIR/spot-interruption-${trial_id}.failure.json"
  if [[ "$steady_status" -ne 0 ]]; then
    failed_attempts=$((failed_attempts + 1))
    if [[ "$steady_status" -eq 11 ]]; then gate_error="Live workload deviated from the preregistered preStop3/PDB/topology/readiness/termination configuration"; else gate_error="Ready Pod IP set did not exactly match two healthy Target Group target IPs on port 80 before timeout"; fi
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg file "$steady_log" --argjson exit_code "$steady_status" --arg error "$gate_error" \
      '{kind:"pre_attempt_steady_state_gate_failed",trial:$trial,observed_at_utc:$at,gate_raw_file:$file,gate_exit_code:$exit_code,fis_started:false,error:$error,replacement_attempt:false}' > "$failure_file"
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg file "$steady_log" --argjson exit_code "$steady_status" \
      '{kind:"attempt_failed_before_fis",trial:$trial,observed_at_utc:$at,gate_raw_file:$file,exit_code:$exit_code,fis_started:false}' > "$EXECUTION_DIR/spot-interruption-${trial_id}.jsonl"
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg file "$steady_log" \
      '{kind:"fixed_attempt_end",trial:$trial,observed_at_utc:$at,result:"failed_pre_fis_target_set_gate",gate_raw_file:$file,fis_experiment_id:null}' >> "$control_log"
    continue
  fi

  set +e
  python3 "$ROOT/measure/scripts/spot-interruption-trial.py" \
    --trial "$trial" \
    --fis-template-id "$TEMPLATE_ID" \
    --interruption-queue-url "$interruption_queue_url" \
    --audit-queue-url "$audit_queue_url" \
    --workload-manifest "$MANIFEST" \
    --output "$summary" \
    --rps 10 --duration 125 --interrupt-after 18 --request-timeout 5 --poll-interval 2 \
    --preserve-environment --execute-fis
  trial_status=$?
  set -e

  if [[ "$trial_status" -ne 0 ]]; then failed_attempts=$((failed_attempts + 1)); fi
  if [[ ! -f "$summary" && ! -f "$failure_file" ]]; then
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --argjson exit_code "$trial_status" \
      '{kind:"attempt_output_missing",trial:$trial,observed_at_utc:$at,exit_code:$exit_code}' >> "$control_log"
    printf 'Attempt %s has no raw result record; stopping to preserve fixed attempt accounting.\n' "$trial_id" >&2
    exit 12
  fi

  fis_id=""
  if [[ -f "$summary" ]]; then fis_id="$(jq -r '.fis_experiment_id // empty' "$summary")"; fi
  if [[ -z "$fis_id" && -f "$failure_file" ]]; then fis_id="$(jq -r '.fis_experiment_id // empty' "$failure_file")"; fi
  if [[ -n "$fis_id" ]] && ! wait_for_fis_terminal "$trial" "$fis_id"; then
    batch_stopped_reason="FIS $fis_id did not reach a terminal state within 900 seconds after fixed attempt $trial_id; no later FIS was started."
    jq -cn --argjson trial "$trial" --arg id "$fis_id" --arg reason "$batch_stopped_reason" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
      '{kind:"batch_safety_stop_nonterminal_fis",trial:$trial,fis_experiment_id:$id,observed_at_utc:$at,reason:$reason}' >> "$control_log"
    printf '%s\n' "$batch_stopped_reason" >&2
    break
  fi

  jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --argjson exit_code "$trial_status" \
    --arg status "$(if [[ "$trial_status" -eq 0 ]]; then printf 'completed'; else printf 'failed'; fi)" \
    --arg fis_id "$fis_id" \
    '{kind:"fixed_attempt_end",trial:$trial,observed_at_utc:$at,runner_exit_code:$exit_code,result:$status,fis_experiment_id:(if $fis_id=="" then null else $fis_id end)}' >> "$control_log"
done

final_steady_log="$EXECUTION_DIR/postrun-target-set-natural-settlement.jsonl"
set +e
python3 "$BATCH_DIR/check-ready-pod-target-set.py" --attempt 10 --output "$final_steady_log" --timeout-seconds 900 --interval-seconds 15
final_steady_status=$?
set -e
jq -cn --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg file "$final_steady_log" --argjson exit_code "$final_steady_status" --arg reason "$batch_stopped_reason" \
  '{kind:"postrun_target_set_settlement",observed_at_utc:$at,raw_file:$file,exit_code:$exit_code,batch_safety_stop_reason:(if $reason=="" then null else $reason end),terraform_destroy:false}' >> "$control_log"

record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get pods -n measure -o json
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -o wide
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodeclaims -o wide
record aws fis list-experiments --max-results 100 --region "$REGION" --output json
record aws sqs get-queue-attributes --region "$REGION" --queue-url "$interruption_queue_url" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --output json
record aws sqs get-queue-attributes --region "$REGION" --queue-url "$audit_queue_url" --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible --output json
python3 "$BATCH_DIR/analysis/compare-prestop3-repro-n15.py"
printf 'Fixed attempt loop finished; %s recorded failures. Persistent EKS/ALB/workload/Spot capacity remains retained.\n' "$failed_attempts"
