#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
NODEGROUP="${CLUSTER}-experiment"
KUBECONFIG_PATH="$HOME/.kube/onebite-eks.kubeconfig"
MODE="${1:-}"
INTERRUPTION_QUEUE="${KARPENTER_INTERRUPTION_QUEUE:-}"
KARPENTER_QUEUE_ARGS=()
if [[ -n "$INTERRUPTION_QUEUE" ]]; then
  KARPENTER_QUEUE_ARGS+=(--set-string "settings.interruptionQueue=$INTERRUPTION_QUEUE")
fi

if [[ ! -f "$KUBECONFIG_PATH" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$KUBECONFIG_PATH" >&2
  exit 2
fi
CONFIGURED_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if [[ "$CONFIGURED_REGION" != "$REGION" || "${ACCOUNT_ID: -4}" != "0497" ]]; then
  printf 'Refusing autoscaler change: target must be AWS account ****0497 in ap-northeast-2.\n' >&2
  exit 3
fi
if [[ "$MODE" != "ca" && "$MODE" != "karpenter" && "$MODE" != "none" ]]; then
  printf 'Usage: %s ca|karpenter|none\n' "$0" >&2
  exit 2
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
  printf 'Probe Pods did not terminate before changing autoscaler mode.\n' >&2
  exit 4
fi

remove_karpenter() {
  if KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get crd nodepools.karpenter.sh >/dev/null 2>&1; then
    for NODEPOOL in experiment phase3-spot; do
      KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete nodepool "$NODEPOOL" --ignore-not-found
    done
    for _ in $(seq 1 900); do
      NODE_COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l karpenter.sh/nodepool=experiment -o json | jq '.items | length')"
      PHASE3_NODE_COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodes -l karpenter.sh/nodepool=phase3-spot -o json | jq '.items | length')"
      CLAIM_COUNT="$(KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" get nodeclaims.karpenter.sh -l 'karpenter.sh/nodepool in (experiment,phase3-spot)' -o json | jq '.items | length')"
      [[ "$NODE_COUNT" == "0" && "$PHASE3_NODE_COUNT" == "0" && "$CLAIM_COUNT" == "0" ]] && break
      sleep 2
    done
    if [[ "${NODE_COUNT:-1}" != "0" || "${PHASE3_NODE_COUNT:-1}" != "0" || "${CLAIM_COUNT:-1}" != "0" ]]; then
      printf 'Karpenter experiment nodes or NodeClaims did not terminate; leaving the controller installed.\n' >&2
      return 1
    fi
    for NODECLASS in experiment phase3-spot; do
      KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete ec2nodeclass "$NODECLASS" --ignore-not-found
    done
  fi
  helm uninstall karpenter --namespace karpenter --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CLUSTER" 2>/dev/null || true
  KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete namespace karpenter --ignore-not-found --wait=true
}

remove_ca() {
  KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" delete -f "$ROOT/measure/manifests/cluster-autoscaler.yaml" --ignore-not-found
}

case "$MODE" in
  ca)
    remove_karpenter
    aws eks update-nodegroup-config --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP" \
      --scaling-config minSize=0,maxSize=2,desiredSize=0
    aws eks wait nodegroup-active --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP"
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f "$ROOT/measure/manifests/cluster-autoscaler.yaml"
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" rollout status deployment/cluster-autoscaler -n kube-system --timeout=10m
    ;;
  karpenter)
    remove_ca
    aws eks update-nodegroup-config --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP" \
      --scaling-config minSize=0,maxSize=2,desiredSize=0
    aws eks wait nodegroup-active --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP"
    helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
      --version "${KARPENTER_CHART_VERSION:-1.14.1}" \
      --namespace karpenter --create-namespace --kubeconfig "$KUBECONFIG_PATH" --kube-context "$CLUSTER" \
      --set settings.clusterName="$CLUSTER" \
      "${KARPENTER_QUEUE_ARGS[@]}" \
      --set replicas=1 \
      --set serviceAccount.create=true \
      --set serviceAccount.name=karpenter \
      --set nodeSelector.measure-pool=system \
      --set controller.resources.requests.cpu=100m \
      --set controller.resources.requests.memory=256Mi \
      --wait --timeout 10m
    TF_OUTPUTS="$(terraform -chdir="$ROOT/measure/terraform" output -json)"
    EXPERIMENT_AMI_ID="$(jq -r '.experiment_ami_id.value' <<<"$TF_OUTPUTS")"
    EXPERIMENT_AZ="$(jq -r '.experiment_availability_zone.value' <<<"$TF_OUTPUTS")"
    if [[ ! "$EXPERIMENT_AMI_ID" =~ ^ami-[[:xdigit:]]+$ ]]; then
      printf 'Terraform output did not provide a valid pinned experiment AMI ID.\n' >&2
      exit 5
    fi
    if [[ ! "$EXPERIMENT_AZ" =~ ^ap-northeast-2[a-z]$ ]]; then
      printf 'Terraform output did not provide a valid Seoul experiment AZ.\n' >&2
      exit 5
    fi
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" apply -f <(
      sed -e "s/__EXPERIMENT_AMI_ID__/${EXPERIMENT_AMI_ID}/g" \
        -e "s/__EXPERIMENT_AZ__/${EXPERIMENT_AZ}/g" \
        "$ROOT/measure/manifests/karpenter-nodes.yaml"
    )
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Established crd/ec2nodeclasses.karpenter.k8s.aws --timeout=5m
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Established crd/nodepools.karpenter.sh --timeout=5m
    ;;
  none)
    remove_karpenter
    remove_ca
    aws eks update-nodegroup-config --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP" \
      --scaling-config minSize=2,maxSize=2,desiredSize=2
    aws eks wait nodegroup-active --region "$REGION" --cluster-name "$CLUSTER" --nodegroup-name "$NODEGROUP"
    KUBECONFIG="$KUBECONFIG_PATH" kubectl --context "$CLUSTER" wait --for=condition=Ready nodes -l measure-pool=experiment --timeout=15m
    ;;
esac

printf 'Autoscaler mode set to %s.\n' "$MODE"
