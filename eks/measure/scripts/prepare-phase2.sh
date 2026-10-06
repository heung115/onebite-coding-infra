#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
SESSION_DIR="${1:-$ROOT/measure/results/20260930-phase2-instance-selection}"
if [[ "$SESSION_DIR" != /* ]]; then SESSION_DIR="$ROOT/$SESSION_DIR"; fi
case "$SESSION_DIR" in
  "$ROOT"/measure/results/*) ;;
  *) printf 'Results must stay under measure/results/.\n' >&2; exit 2 ;;
esac
if [[ "${2:-}" != "--execute" || "${ALLOW_PHASE2_EXECUTION:-}" != "1" ]]; then
  printf 'Usage: ALLOW_PHASE2_EXECUTION=1 %s <session-dir> --execute\n' "$0" >&2
  printf 'This installs Karpenter and Phase 2 NodePools; use only after the separate approval.\n' >&2
  exit 3
fi
if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$KUBECONFIG_PATH" >&2
  exit 4
fi
configured_region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
account_id="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$configured_region" != "$REGION" || "${account_id: -4}" != "0497" ]]; then
  printf 'Refusing Phase 2 preparation: target must be account ****0497 in ap-northeast-2.\n' >&2
  exit 5
fi

mkdir -p "$SESSION_DIR"
COMMAND_LOG="$SESSION_DIR/commands.log"
record() { "$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" "$@"; }

record bash "$ROOT/measure/scripts/select-autoscaler.sh" karpenter

# Reuse the A controller installation but replace its single fixed NodePool/NodeClass with Phase 2 pools.
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete nodepool experiment --ignore-not-found --wait=true
for _ in $(seq 1 900); do
  node_count="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l karpenter.sh/nodepool=experiment -o json | jq '.items | length')"
  claim_count="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodeclaims.karpenter.sh -l karpenter.sh/nodepool=experiment -o json | jq '.items | length')"
  if [[ "$node_count" == "0" && "$claim_count" == "0" ]]; then break; fi
  sleep 2
done
if [[ "${node_count:-1}" != "0" || "${claim_count:-1}" != "0" ]]; then
  printf 'The A experiment NodePool did not drain completely; Phase 2 NodePools were not applied.\n' >&2
  exit 6
fi
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete ec2nodeclass experiment --ignore-not-found --wait=true

ami_id="$(terraform -chdir="$ROOT/measure/terraform" output -raw experiment_ami_id)"
az="$(terraform -chdir="$ROOT/measure/terraform" output -raw experiment_availability_zone)"
if [[ ! "$ami_id" =~ ^ami-[[:xdigit:]]+$ || ! "$az" =~ ^ap-northeast-2[a-z]$ ]]; then
  printf 'Terraform outputs did not provide the pinned Seoul AMI and AZ.\n' >&2
  exit 7
fi
rendered="$SESSION_DIR/karpenter-phase2-rendered.yaml"
sed -e "s/__EXPERIMENT_AMI_ID__/${ami_id}/g" -e "s/__EXPERIMENT_AZ__/${az}/g" \
  "$ROOT/measure/manifests/karpenter-phase2.yaml" > "$rendered"
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f "$rendered"
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Established crd/ec2nodeclasses.karpenter.k8s.aws --timeout=5m
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Established crd/nodepools.karpenter.sh --timeout=5m
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Ready ec2nodeclass/phase2 --timeout=10m
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Ready nodepool/phase2-fixed --timeout=10m
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Ready nodepool/phase2-flexible --timeout=10m
record KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get ec2nodeclass/phase2 nodepool/phase2-fixed nodepool/phase2-flexible -o yaml
record SESSION_DIR="$SESSION_DIR" ENVIRONMENT_OUTPUT="$SESSION_DIR/environment-phase2.json" bash "$ROOT/measure/scripts/capture-environment.sh"
printf 'Phase 2 NodeClass and NodePools are ready; calibration/benchmark are separate gated steps.\n' | tee -a "$COMMAND_LOG"
