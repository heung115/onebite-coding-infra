#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
WORKSPACE="${PHASE3_TF_WORKSPACE:-phase3-spot-interruption}"
EXPERIMENT_TAG="${PHASE3_EXPERIMENT_TAG:-phase3-spot-20260930}"
REACHABILITY_GATE_VERSION="${PHASE3_REACHABILITY_GATE_VERSION:-0}"
if [[ "$REACHABILITY_GATE_VERSION" == "0" && "${PHASE3_V3_REACHABILITY_GATE:-0}" == "1" ]]; then
  REACHABILITY_GATE_VERSION="3"
fi
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
SESSION_DIR="${1:-$ROOT/measure/results/20260930-phase3-spot-interruption}"
TRIAL_COUNT="${PHASE3_TRIAL_COUNT:-3}"

if [[ "$SESSION_DIR" != /* ]]; then SESSION_DIR="$ROOT/$SESSION_DIR"; fi
case "$SESSION_DIR" in
  "$ROOT"/measure/results/*) ;;
  *) printf 'Results must stay under measure/results/.\n' >&2; exit 2 ;;
esac
if [[ ! "$TRIAL_COUNT" =~ ^[1-5]$ ]]; then
  printf 'PHASE3_TRIAL_COUNT must be between 1 and 5.\n' >&2
  exit 2
fi
if [[ "${2:-}" != "--execute" || "${ALLOW_PHASE3_BENCHMARK:-}" != "1" ]]; then
  printf 'Usage: ALLOW_PHASE3_BENCHMARK=1 %s <session-dir> --execute\n' "$0" >&2
  printf 'This starts three AWS FIS Spot interruption trials and requires separate approval.\n' >&2
  exit 3
fi
if [[ "${TF_WORKSPACE:-}" != "$WORKSPACE" ]]; then
  printf 'Refusing Phase 3 run outside Terraform workspace %s.\n' "$WORKSPACE" >&2
  exit 4
fi
if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$KUBECONFIG_PATH" >&2
  exit 5
fi

export AWS_REGION="$REGION"
account_id="$(aws sts get-caller-identity --region "$REGION" --query Account --output text)"
if [[ "${account_id: -4}" != "0497" ]]; then
  printf 'Refusing Phase 3 run: caller account does not end in 0497.\n' >&2
  exit 6
fi

mkdir -p "$SESSION_DIR"
export PHASE3_EXPERIMENT_TAG="$EXPERIMENT_TAG"
if [[ "$REACHABILITY_GATE_VERSION" != "0" && "$REACHABILITY_GATE_VERSION" != "3" && "$REACHABILITY_GATE_VERSION" != "4" && "$REACHABILITY_GATE_VERSION" != "5" && "$REACHABILITY_GATE_VERSION" != "6" ]]; then
  printf 'Reachability gate version must be 0, 3, 4, 5, or 6.\n' >&2
  exit 4
fi
export PHASE3_REACHABILITY_GATE_VERSION="$REACHABILITY_GATE_VERSION"
template_id="$(terraform -chdir="$ROOT/measure/terraform" output -raw phase3_fis_experiment_template_id)"
interruption_queue_url="$(terraform -chdir="$ROOT/measure/terraform" output -raw phase3_karpenter_interruption_queue_url)"
audit_queue_url="$(terraform -chdir="$ROOT/measure/terraform" output -raw phase3_event_audit_queue_url)"
if [[ ! "$template_id" =~ ^EXT[0-9A-Za-z]+$ || -z "$interruption_queue_url" || -z "$audit_queue_url" ]]; then
  printf 'Terraform outputs are missing the Phase 3 FIS template or queues.\n' >&2
  exit 7
fi

failed_trials=0
workload_args=()
if [[ -n "${PHASE3_WORKLOAD_MANIFEST:-}" ]]; then
  workload_args+=(--workload-manifest "$PHASE3_WORKLOAD_MANIFEST")
fi
for trial in $(seq 1 "$TRIAL_COUNT"); do
  printf -v trial_id '%02d' "$trial"
  set +e
  python3 "$ROOT/measure/scripts/spot-interruption-trial.py" \
    --trial "$trial" \
    --fis-template-id "$template_id" \
    --interruption-queue-url "$interruption_queue_url" \
    --audit-queue-url "$audit_queue_url" \
    --output "$SESSION_DIR/spot-interruption-${trial_id}.json" \
    "${workload_args[@]}" \
    --execute-fis
  trial_status=$?
  set -e
  if [[ "$trial_status" -ne 0 ]]; then
    failed_trials=$((failed_trials + 1))
    failure_file="$SESSION_DIR/spot-interruption-${trial_id}.failure.json"
    cleanup_complete="$(jq -r '.cleanup.complete // false' "$failure_file" 2>/dev/null || printf 'false')"
    if [[ "$cleanup_complete" != "true" ]]; then
      printf 'Trial %s failed and cleanup is incomplete; stopping before another FIS run. Raw data remains in %s.\n' "$trial_id" "$SESSION_DIR" >&2
      exit 8
    fi
    fis_experiment_id="$(jq -r '.fis_experiment_id // empty' "$failure_file" 2>/dev/null || true)"
    if [[ "$REACHABILITY_GATE_VERSION" != "0" && -z "$fis_experiment_id" ]]; then
      printf 'Trial %s failed before FIS at the v%s reachability gate; cleanup is verified and the next preregistered trial will continue. Raw data remains in %s.\n' "$trial_id" "$REACHABILITY_GATE_VERSION" "$SESSION_DIR" >&2
      continue
    fi
    printf 'Trial %s failed; cleanup is verified. Its raw data is retained and the next preregistered trial will continue.\n' "$trial_id" >&2
  fi
done

printf '%s preregistered Spot interruption trials finished (%s failures); application workers were reset after each.\n' "$TRIAL_COUNT" "$failed_trials"
if [[ "$failed_trials" -gt 0 ]]; then exit 1; fi
