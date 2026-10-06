#!/usr/bin/env bash
set -euo pipefail

REGION="ap-northeast-2"
CLUSTER="onebite-eks-measure"
CONFIGURED_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

if [[ "$CONFIGURED_REGION" != "$REGION" || "${ACCOUNT_ID: -4}" != "0497" ]]; then
  printf 'Refusing EKS endpoint update: target must be account ****0497 in ap-northeast-2.\n' >&2
  exit 2
fi

CURRENT_CONFIG="$(aws eks describe-cluster --region "$REGION" --name "$CLUSTER" \
  --query 'cluster.resourcesVpcConfig.{private:endpointPrivateAccess,public:endpointPublicAccess,cidrs:publicAccessCidrs}' --output json)"
PUBLIC_CIDRS="$(jq -c '.cidrs' <<<"$CURRENT_CONFIG")"
if [[ "$(jq -r '.public' <<<"$CURRENT_CONFIG")" != "true" ]]; then
  printf 'Public endpoint was not enabled; refusing to change endpoint access unexpectedly.\n' >&2
  exit 3
fi

if [[ "$(jq -r '.private' <<<"$CURRENT_CONFIG")" != "true" ]]; then
  UPDATED_CONFIG="$(jq -cn --argjson cidrs "$PUBLIC_CIDRS" \
    '{endpointPublicAccess:true,endpointPrivateAccess:true,publicAccessCidrs:$cidrs}')"
  aws eks update-cluster-config --region "$REGION" --name "$CLUSTER" \
    --resources-vpc-config "$UPDATED_CONFIG" >/dev/null
fi
aws eks wait cluster-active --region "$REGION" --name "$CLUSTER"
FINAL_CONFIG="$(aws eks describe-cluster --region "$REGION" --name "$CLUSTER" \
  --query 'cluster.{status:status,private:resourcesVpcConfig.endpointPrivateAccess,public:resourcesVpcConfig.endpointPublicAccess,cidrs:resourcesVpcConfig.publicAccessCidrs}' --output json)"
FINAL_CIDRS="$(jq -c '.cidrs' <<<"$FINAL_CONFIG")"
if [[ "$(jq -r '.status' <<<"$FINAL_CONFIG")" != "ACTIVE" || \
      "$(jq -r '.private' <<<"$FINAL_CONFIG")" != "true" || \
      "$(jq -r '.public' <<<"$FINAL_CONFIG")" != "true" || \
      "$FINAL_CIDRS" != "$PUBLIC_CIDRS" ]]; then
  printf 'Could not verify private endpoint access with the original public CIDR allow-list.\n' >&2
  exit 4
fi
jq '{status,private,public}' <<<"$FINAL_CONFIG"
