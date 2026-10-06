#!/usr/bin/env bash
set -euo pipefail

REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
NODEGROUP="${CLUSTER}-experiment"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
CONDITION="${1:-}"

if [[ "$CONDITION" != "disabled" && "$CONDITION" != "enabled" ]]; then
  printf 'Usage: %s disabled|enabled\n' "$0" >&2
  exit 2
fi
if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$KUBECONFIG_PATH" >&2
  exit 2
fi
CONFIGURED_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$CONFIGURED_REGION" != "$REGION" || "${ACCOUNT_ID: -4}" != "0497" ]]; then
  printf 'Refusing CNI change: target must be AWS account ****0497 in ap-northeast-2.\n' >&2
  exit 3
fi

for DEPLOYMENT in scale-probe bulk-probe; do
  if KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get deployment "$DEPLOYMENT" -n measure >/dev/null 2>&1; then
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" scale "deployment/$DEPLOYMENT" --replicas=0 -n measure
  fi
done
for _ in $(seq 1 120); do
  SCALE_PODS="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get pods -n measure -l app=scale-probe -o json | jq '.items | length')"
  BULK_PODS="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get pods -n measure -l app=bulk-probe -o json | jq '.items | length')"
  [[ "$SCALE_PODS" == "0" && "$BULK_PODS" == "0" ]] && break
  sleep 2
done
if [[ "${SCALE_PODS:-1}" != "0" || "${BULK_PODS:-1}" != "0" ]]; then
  printf 'Probe Pods did not terminate before changing the CNI.\n' >&2
  exit 4
fi

PREFIX_VALUE="false"
if [[ "$CONDITION" == "enabled" ]]; then
  PREFIX_VALUE="true"
fi
CONFIGURATION="$(jq -cn --arg prefix "$PREFIX_VALUE" --arg tags '{"Project":"onebite","Temporary":"true"}' \
  '{env:{ENABLE_PREFIX_DELEGATION:$prefix,WARM_PREFIX_TARGET:"1",ADDITIONAL_ENI_TAGS:$tags}}')"

aws eks update-addon --region "$REGION" --cluster-name "$CLUSTER" --addon-name vpc-cni \
  --configuration-values "$CONFIGURATION" --resolve-conflicts OVERWRITE
aws eks wait addon-active --region "$REGION" --cluster-name "$CLUSTER" --addon-name vpc-cni
KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" rollout status daemonset/aws-node -n kube-system --timeout=10m

# Replace only the application experiment pool so all its nodes start in the same CNI condition.
aws eks update-nodegroup-config --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP" \
  --scaling-config minSize=0,maxSize=2,desiredSize=0
aws eks wait nodegroup-active --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP"
COUNT=1
for _ in $(seq 1 900); do
  COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l measure-pool=experiment -o json | jq '.items | length')"
  [[ "$COUNT" == "0" ]] && break
  sleep 2
done
if [[ "$COUNT" != "0" ]]; then
  printf 'Experiment nodes did not drain within 30 minutes.\n' >&2
  exit 3
fi

aws eks update-nodegroup-config --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP" \
  --scaling-config minSize=2,maxSize=2,desiredSize=2
aws eks wait nodegroup-active --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP"
COUNT=0
for _ in $(seq 1 900); do
  COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l measure-pool=experiment -o json | jq '.items | length')"
  [[ "$COUNT" == "2" ]] && break
  sleep 2
done
if [[ "$COUNT" != "2" ]]; then
  printf 'Two experiment-pool nodes did not register within 30 minutes.\n' >&2
  exit 4
fi
KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Ready nodes -l measure-pool=experiment --timeout=15m

printf 'CNI Prefix Delegation condition set to %s; WARM_PREFIX_TARGET remained 1.\n' "$CONDITION"
