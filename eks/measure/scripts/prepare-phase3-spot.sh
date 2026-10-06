#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
WORKSPACE="${PHASE3_TF_WORKSPACE:-phase3-spot-interruption}"
EXPERIMENT_TAG="${PHASE3_EXPERIMENT_TAG:-phase3-spot-20260930}"
SESSION_DIR="${1:-$ROOT/measure/results/20260930-phase3-spot-interruption}"

if [[ "$SESSION_DIR" != /* ]]; then SESSION_DIR="$ROOT/$SESSION_DIR"; fi
case "$SESSION_DIR" in
  "$ROOT"/measure/results/*) ;;
  *) printf 'Results must stay under measure/results/.\n' >&2; exit 2 ;;
esac
if [[ "${2:-}" != "--execute" || "${ALLOW_PHASE3_SETUP:-}" != "1" ]]; then
  printf 'Usage: ALLOW_PHASE3_SETUP=1 %s <session-dir> --execute\n' "$0" >&2
  printf 'This installs Phase 3 Karpenter and workload prerequisites; it requires separate approval.\n' >&2
  exit 3
fi
if [[ "${TF_WORKSPACE:-}" != "$WORKSPACE" ]]; then
  printf 'Refusing Phase 3 setup outside Terraform workspace %s.\n' "$WORKSPACE" >&2
  exit 4
fi
if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$KUBECONFIG_PATH" >&2
  exit 5
fi

configured_region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
account_id="$(aws sts get-caller-identity --region "$REGION" --query Account --output text)"
if [[ "$configured_region" != "$REGION" || "${account_id: -4}" != "0497" ]]; then
  printf 'Refusing Phase 3 setup: target must be account ****0497 in ap-northeast-2.\n' >&2
  exit 6
fi

mkdir -p "$SESSION_DIR"
COMMAND_LOG="$SESSION_DIR/prepare-commands.log"
record() { "$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" "$@"; }

if [[ "${PHASE3_REACHABILITY_GATE_VERSION:-0}" == "4" || "${PHASE3_REACHABILITY_GATE_VERSION:-0}" == "5" || "${PHASE3_REACHABILITY_GATE_VERSION:-0}" == "6" ]]; then
  record env PHASE3_LBC_TARGET_SG_SELECTOR="onebite.io/lbc-target-sg=$CLUSTER" bash "$ROOT/measure/scripts/cluster-bootstrap.sh"
else
  record bash "$ROOT/measure/scripts/cluster-bootstrap.sh"
fi
queue_name="$("$ROOT/measure/scripts/tf.sh" output -raw phase3_karpenter_interruption_queue_name)"
ami_id="$("$ROOT/measure/scripts/tf.sh" output -raw experiment_ami_id)"
az_json="$("$ROOT/measure/scripts/tf.sh" output -json phase3_spot_availability_zones)"
if ! jq -e 'length == 2 and all(.[]; test("^ap-northeast-2[a-z]$"))' <<<"$az_json" >/dev/null; then
  printf 'Terraform output must contain the two Phase 2 Seoul worker AZs.\n' >&2
  exit 7
fi
az_values="$(jq -r 'map("\"" + . + "\"") | join(", ")' <<<"$az_json")"
if [[ "$queue_name" != "${CLUSTER}-karpenter-interruption" || ! "$ami_id" =~ ^ami-[[:xdigit:]]+$ ]]; then
  printf 'Terraform outputs do not match the pinned Phase 3 queue and AMI.\n' >&2
  exit 7
fi

record env KARPENTER_CHART_VERSION=1.14.1 KARPENTER_INTERRUPTION_QUEUE="$queue_name" \
  bash "$ROOT/measure/scripts/select-autoscaler.sh" karpenter
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete nodepool experiment --ignore-not-found --wait=true
for _ in $(seq 1 900); do
  node_count="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l karpenter.sh/nodepool=experiment -o json | jq '.items | length')"
  claim_count="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodeclaims.karpenter.sh -l karpenter.sh/nodepool=experiment -o json | jq '.items | length')"
  if [[ "$node_count" == "0" && "$claim_count" == "0" ]]; then break; fi
  sleep 2
done
if [[ "${node_count:-1}" != "0" || "${claim_count:-1}" != "0" ]]; then
  printf 'The temporary On-Demand experiment NodePool did not drain; Spot NodePool was not applied.\n' >&2
  exit 8
fi
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete ec2nodeclass experiment --ignore-not-found --wait=true

rendered="$SESSION_DIR/karpenter-phase3-spot-rendered.yaml"
sed -e "s/__EXPERIMENT_AMI_ID__/${ami_id}/g" -e "s/__EXPERIMENT_AZS__/${az_values}/g" \
  -e "s/__EXPERIMENT_TAG__/${EXPERIMENT_TAG}/g" \
  "$ROOT/measure/manifests/karpenter-phase3-spot.yaml" > "$rendered"
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f "$rendered"
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Established crd/ec2nodeclasses.karpenter.k8s.aws --timeout=5m
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Established crd/nodepools.karpenter.sh --timeout=5m
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Ready ec2nodeclass/phase3-spot --timeout=10m
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Ready nodepool/phase3-spot --timeout=10m
record env KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get ec2nodeclass/phase3-spot nodepool/phase3-spot -o yaml

printf 'Phase 3 Spot NodePool ready; application/FIS runs remain separately gated.\n' | tee -a "$COMMAND_LOG"
