#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
HELM_CONFIG="$ROOT/measure/.helm/repositories.yaml"
HELM_CACHE="$ROOT/measure/.helm/cache"
CONFIGURED_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"

if [[ "$CONFIGURED_REGION" != "$REGION" ]]; then
  printf 'Refusing bootstrap: configured region is %s, expected %s.\n' "${CONFIGURED_REGION:-unset}" "$REGION" >&2
  exit 2
fi
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "${ACCOUNT_ID: -4}" != "0497" ]]; then
  printf 'Refusing bootstrap: caller account does not end in 0497.\n' >&2
  exit 3
fi
mkdir -p "$HOME/.kube" "$ROOT/measure/.helm/cache"
if [[ -e "$KUBECONFIG_PATH" ]]; then
  PREVIOUS_CONTEXT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl config current-context 2>/dev/null || true)"
else
  PREVIOUS_CONTEXT=""
fi
# Merge a uniquely named context into only the dedicated kubeconfig and restore its prior selection.
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --alias "$CLUSTER" --user-alias "$CLUSTER-user" --kubeconfig "$KUBECONFIG_PATH"
if [[ -n "$PREVIOUS_CONTEXT" && "$PREVIOUS_CONTEXT" != "$CLUSTER" ]]; then
  KUBECONFIG="$KUBECONFIG_PATH" kubectl config use-context "$PREVIOUS_CONTEXT" >/dev/null
fi

KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" create namespace measure --dry-run=client -o yaml | KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f -
KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f "$ROOT/measure/manifests/scale-probe.yaml"
KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f "$ROOT/measure/manifests/bulk-probe.yaml"

VPC_ID="$("$ROOT/measure/scripts/tf.sh" output -raw vpc_id)"
LBC_TARGET_SG_SELECTOR="${PHASE3_LBC_TARGET_SG_SELECTOR:-}"
LBC_SELECTOR_ARGS=()
if [[ -n "$LBC_TARGET_SG_SELECTOR" ]]; then
  LBC_SELECTOR_ARGS+=(--set-string "serviceTargetENISGTags=$LBC_TARGET_SG_SELECTOR")
fi
helm repo add eks https://aws.github.io/eks-charts \
  --repository-config "$HELM_CONFIG" --repository-cache "$HELM_CACHE"
helm repo update eks --repository-config "$HELM_CONFIG" --repository-cache "$HELM_CACHE"
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller \
  --version 3.5.0 \
  --namespace kube-system \
  --kubeconfig "$KUBECONFIG_PATH" \
  --kube-context "$CLUSTER" \
  --repository-config "$HELM_CONFIG" --repository-cache "$HELM_CACHE" \
  --set clusterName="$CLUSTER" \
  --set region="$REGION" \
  --set vpcId="$VPC_ID" \
  --set serviceAccount.create=true \
  --set serviceAccount.name=aws-load-balancer-controller \
  --set defaultTags.Project=onebite \
  --set defaultTags.Temporary=true \
  "${LBC_SELECTOR_ARGS[@]}" \
  --wait --timeout 10m
KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" rollout status deployment/aws-load-balancer-controller -n kube-system --timeout=10m

printf 'Dedicated kubeconfig: %s\n' "$KUBECONFIG_PATH"
printf 'Base cluster add-ons are ready; autoscaler mode is selected separately.\n'
