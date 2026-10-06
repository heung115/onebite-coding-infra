#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
WORKSPACE="phase3-spot-interruption-v8-prestop5-n10-20261003"
EXPERIMENT_TAG="phase3-spot-20261003-v8"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
SESSION_DIR="${1:-$ROOT/measure/results/20261003-phase3-prestop5-repro-n10}"
TRIAL_COUNT=10

export PATH="/opt/homebrew/bin:/usr/bin:/bin:$PATH"
if [[ "$SESSION_DIR" != /* ]]; then SESSION_DIR="$ROOT/$SESSION_DIR"; fi
MANIFEST="$SESSION_DIR/config/spot-recovery-app-prestop5-n10.yaml"
EXECUTION_DIR="$SESSION_DIR/execution-01"
case "$SESSION_DIR" in
  "$ROOT"/measure/results/20261003-phase3-prestop5-repro-n10) ;;
  *) printf 'Results must stay in the new n=10 batch directory.\n' >&2; exit 2 ;;
esac
if [[ "${2:-}" != "--execute" || "${ALLOW_PHASE3_BENCHMARK:-}" != "1" ]]; then
  printf 'Usage: ALLOW_PHASE3_BENCHMARK=1 %s <new-batch-dir> --execute\n' "$0" >&2
  exit 3
fi
if [[ "${TF_WORKSPACE:-}" != "$WORKSPACE" ]]; then
  printf 'Refusing run outside Terraform workspace %s.\n' "$WORKSPACE" >&2
  exit 4
fi
if [[ ! -f "$KUBECONFIG_PATH" || ! -f "$MANIFEST" ]]; then
  printf 'The dedicated kubeconfig or preregistered workload manifest is missing.\n' >&2
  exit 5
fi
if compgen -G "$EXECUTION_DIR/spot-interruption-*.json" >/dev/null; then
  printf 'Refusing to overwrite an existing n=10 attempt.\n' >&2
  exit 6
fi

export AWS_REGION="$REGION"
account_id="$(aws sts get-caller-identity --region "$REGION" --query Account --output text)"
if [[ "${account_id: -4}" != "0497" ]]; then
  printf 'Refusing run: AWS account must end in 0497.\n' >&2
  exit 7
fi

mkdir -p "$EXECUTION_DIR"
export PHASE3_EXPERIMENT_TAG="$EXPERIMENT_TAG"
export PHASE3_TF_WORKSPACE="$WORKSPACE"
export PHASE3_REACHABILITY_GATE_VERSION=6
export PHASE3_PRESTOP_SECONDS=5
export PHASE3_WORKLOAD_CONDITION=prestop5

record() { "$ROOT/measure/scripts/record-command.sh" "$EXECUTION_DIR/prepare-commands.log" "$@"; }

template_id="$(terraform -chdir="$ROOT/measure/terraform" output -raw phase3_fis_experiment_template_id)"
interruption_queue_url="$(terraform -chdir="$ROOT/measure/terraform" output -raw phase3_karpenter_interruption_queue_url)"
audit_queue_url="$(terraform -chdir="$ROOT/measure/terraform" output -raw phase3_event_audit_queue_url)"
if [[ ! "$template_id" =~ ^EXT[0-9A-Za-z]+$ || -z "$interruption_queue_url" || -z "$audit_queue_url" ]]; then
  printf 'Terraform outputs are missing the v8 FIS template or event queues.\n' >&2
  exit 8
fi

# Create the Deployment, Service, PDB, and Ingress once for the whole batch.
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f "$MANIFEST"
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" rollout status deployment/backend-probe -n measure --timeout=15m

control_log="$EXECUTION_DIR/batch-control.jsonl"
wait_for_fis_terminal() {
  local trial="$1" experiment_id="$2" deadline status response observed_at
  deadline=$((SECONDS + 900))
  while (( SECONDS < deadline )); do
    observed_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    if ! response="$(aws fis get-experiment --id "$experiment_id" --region "$REGION" --output json 2>&1)"; then
      jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$observed_at" --arg error "$response" \
        '{kind:"previous_fis_status_error",trial:$trial,experiment_id:$id,observed_at_utc:$at,error:$error}' >> "$control_log"
      return 1
    fi
    status="$(jq -r '.experiment.state.status // "unknown"' <<<"$response")"
    jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$observed_at" --argjson payload "$response" \
      '{kind:"previous_fis_status_poll",trial:$trial,experiment_id:$id,observed_at_utc:$at,command:["aws","fis","get-experiment","--id",$id,"--region","ap-northeast-2","--output","json"],response:$payload}' >> "$control_log"
    case "$status" in
      completed|failed|stopped|cancelled)
        jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$observed_at" --arg state "$status" \
          '{kind:"previous_fis_terminal_confirmed",trial:$trial,experiment_id:$id,observed_at_utc:$at,status:$state}' >> "$control_log"
        return 0
        ;;
    esac
    sleep 5
  done
  jq -cn --argjson trial "$trial" --arg id "$experiment_id" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{kind:"previous_fis_terminal_timeout",trial:$trial,experiment_id:$id,observed_at_utc:$at,timeout_seconds:900}' >> "$control_log"
  return 1
}

failed_attempts=0
for trial in $(seq 1 "$TRIAL_COUNT"); do
  printf -v trial_id '%02d' "$trial"
  set +e
  python3 "$ROOT/measure/scripts/spot-interruption-trial.py" \
    --trial "$trial" \
    --fis-template-id "$template_id" \
    --interruption-queue-url "$interruption_queue_url" \
    --audit-queue-url "$audit_queue_url" \
    --workload-manifest "$MANIFEST" \
    --output "$EXECUTION_DIR/spot-interruption-${trial_id}.json" \
    --rps 10 --duration 600 --interrupt-after 30 --request-timeout 5 --poll-interval 2 \
    --preserve-environment --execute-fis
  trial_status=$?
  set -e

  summary="$EXECUTION_DIR/spot-interruption-${trial_id}.json"
  failure_file="$EXECUTION_DIR/spot-interruption-${trial_id}.failure.json"
  if [[ "$trial_status" -ne 0 ]]; then failed_attempts=$((failed_attempts + 1)); fi
  if [[ ! -f "$summary" && ! -f "$failure_file" ]]; then
    jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --arg exit_code "$trial_status" \
      '{kind:"attempt_output_missing",trial:$trial,observed_at_utc:$at,exit_code:$exit_code}' >> "$control_log"
    printf 'Attempt %s has no raw summary/failure record; stopping to preserve batch accounting.\n' "$trial_id" >&2
    exit 9
  fi

  fis_id=""
  if [[ -f "$summary" ]]; then fis_id="$(jq -r '.fis_experiment_id // empty' "$summary")"; fi
  if [[ -z "$fis_id" && -f "$failure_file" ]]; then fis_id="$(jq -r '.fis_experiment_id // empty' "$failure_file")"; fi
  if [[ -n "$fis_id" ]] && ! wait_for_fis_terminal "$trial" "$fis_id"; then
    printf 'FIS for attempt %s is not terminal; no later interruption will be started.\n' "$trial_id" >&2
    exit 10
  fi

  jq -cn --argjson trial "$trial" --arg at "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" --argjson exit_code "$trial_status" \
    --arg status "$(if [[ "$trial_status" -eq 0 ]]; then printf 'completed'; else printf 'failed'; fi)" \
    --arg fis_id "$fis_id" \
    '{kind:"fixed_attempt_end",trial:$trial,observed_at_utc:$at,runner_exit_code:$exit_code,result:$status,fis_experiment_id:(if $fis_id=="" then null else $fis_id end)}' >> "$control_log"
done

printf 'Exactly %s fixed attempts finished; %s had recorded failure. Workload and AWS environment remain for the approved final cleanup step.\n' "$TRIAL_COUNT" "$failed_attempts"
