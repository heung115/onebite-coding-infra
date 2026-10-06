#!/usr/bin/env bash
# ArgoCD(EKS) 용 read-only deploy key 를 새로 만들어 GitOps 저장소에 등록하고, 개인키는 Secrets Manager 에만 넣는다.
# 매번 새 키를 쓰고 예전 argocd-eks-readonly 키는 지운다. 개인키는 디스크에 남기지 않는다.
set -euo pipefail
REPO="heung115/tmp-onebite-gitops"
TITLE="argocd-eks-readonly"
REGION="ap-northeast-2"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCT" == *0497 ]] || { echo "계정 불일치: 중단"; exit 1; }

for id in $(gh api "repos/$REPO/keys" --jq ".[]|select(.title==\"$TITLE\")|.id"); do
  gh api -X DELETE "repos/$REPO/keys/$id" >/dev/null
done

TMPD="$(mktemp -d)"
trap 'find "$TMPD" -type f -exec sh -c ": > \"\$1\"" _ {} \; ; find "$TMPD" -type f -delete; rmdir "$TMPD"' EXIT
ssh-keygen -q -t ed25519 -N "" -C "$TITLE" -f "$TMPD/key"
gh api "repos/$REPO/keys" -f title="$TITLE" -f key="$(cat "$TMPD/key.pub")" -F read_only=true >/dev/null
aws secretsmanager put-secret-value --region "$REGION" --secret-id onebite-argocd-repo \
  --secret-string "file://$TMPD/key" >/dev/null
echo "deploy key $TITLE 등록 (read-only), 개인키는 Secrets Manager onebite-argocd-repo"
