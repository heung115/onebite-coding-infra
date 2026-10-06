#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
NODEGROUP="${CLUSTER}-experiment"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"

CONFIGURED_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$CONFIGURED_REGION" != "$REGION" || "${ACCOUNT_ID: -4}" != "0497" ]]; then
  printf 'Refusing cleanup: target must be AWS account ****0497 in ap-northeast-2.\n' >&2
  exit 2
fi
if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$KUBECONFIG_PATH" >&2
  exit 3
fi

for DEPLOYMENT in scale-probe bulk-probe backend-probe; do
  if KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get deployment "$DEPLOYMENT" -n measure >/dev/null 2>&1; then
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" scale "deployment/$DEPLOYMENT" --replicas=0 -n measure
  fi
done
KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete -f "$ROOT/measure/manifests/cluster-autoscaler.yaml" --ignore-not-found

if KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get crd nodepools.karpenter.sh >/dev/null 2>&1; then
  for NODEPOOL in experiment phase3-spot; do
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete nodepool "$NODEPOOL" --ignore-not-found
  done
fi

aws eks update-nodegroup-config --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP" \
  --scaling-config minSize=0,maxSize=2,desiredSize=0
aws eks wait nodegroup-active --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP"

NODE_COUNT=1
CLAIM_COUNT=1
for _ in $(seq 1 900); do
  NODE_COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l measure-pool=experiment -o json | jq '.items | length')"
  if KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get crd nodeclaims.karpenter.sh >/dev/null 2>&1; then
    CLAIM_COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodeclaims.karpenter.sh -l 'karpenter.sh/nodepool in (experiment,phase3-spot)' -o json | jq '.items | length')"
  else
    CLAIM_COUNT=0
  fi
  [[ "$NODE_COUNT" == "0" && "$CLAIM_COUNT" == "0" ]] && break
  sleep 2
done
if [[ "$NODE_COUNT" != "0" || "$CLAIM_COUNT" != "0" ]]; then
  printf 'Experiment-pool nodes or NodeClaims remain; keeping Karpenter installed for safe node cleanup.\n' >&2
  exit 4
fi

if KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get crd nodepools.karpenter.sh >/dev/null 2>&1; then
  for NODECLASS in experiment phase3-spot; do
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete ec2nodeclass "$NODECLASS" --ignore-not-found
  done
fi
helm uninstall karpenter --namespace karpenter --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CLUSTER" 2>/dev/null || true
KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete namespace karpenter --ignore-not-found --wait=true

aws eks update-nodegroup-config --region "$REGION" --cluster-name "$CLUSTER" \
  --nodegroup-name "${CLUSTER}-system" --scaling-config minSize=0,maxSize=1,desiredSize=0
aws eks wait nodegroup-active --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "${CLUSTER}-system"

COUNT=1
for _ in $(seq 1 900); do
  COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l measure-pool=system -o json | jq '.items | length')"
  [[ "$COUNT" == "0" ]] && break
  sleep 2
done
if [[ "$COUNT" != "0" ]]; then
  printf 'System-pool nodes remain; refusing to proceed before the EBS cleanup check.\n' >&2
  exit 5
fi

VOLUMES=1
for _ in $(seq 1 300); do
  VOLUMES="$(aws ec2 describe-volumes --region "$REGION" --filters Name=tag:Project,Values=onebite Name=tag:Temporary,Values=true --query 'length(Volumes[])' --output text)"
  [[ "$VOLUMES" == "0" ]] && break
  sleep 2
done
if [[ "$VOLUMES" != "0" ]]; then
  printf 'Tagged EBS volumes remain after node shutdown; inspect them before Terraform destroy.\n' >&2
  aws ec2 describe-volumes --region "$REGION" --filters Name=tag:Project,Values=onebite Name=tag:Temporary,Values=true --output json
  exit 6
fi

printf 'Autoscaler controllers and both Karpenter experiment pools removed; both node groups are at zero and tagged EBS volumes are gone.\n'
