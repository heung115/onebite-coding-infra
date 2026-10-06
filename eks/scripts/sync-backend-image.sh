#!/usr/bin/env bash
# GitOps eks 브랜치가 쓰는 백엔드 이미지 태그를 GHCR(private) → ECR 로 복사한다.
# 토큰은 이 Mac 에서만 쓰고(gh auth token), 컨테이너 stdin/env 로만 넘긴다. 클러스터·Git 에는 넣지 않는다.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REGION="ap-northeast-2"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCT" == *0497 ]] || { echo "계정 불일치: 중단"; exit 1; }
ECR="$ACCT.dkr.ecr.$REGION.amazonaws.com"
SRC_REPO="ghcr.io/heung115/tmp-onebite-be"
DST_REPO="$ECR/onebite/tmp-onebite-be"

# GitOps eks 브랜치의 envs/<env>/backend.yaml 에서 태그 수집
TAGS="$(for e in dev-front dev-back prod; do
  gh api "repos/heung115/tmp-onebite-gitops/contents/envs/$e/backend.yaml?ref=eks" --jq .content | base64 -d \
    | awk '/tag:/{gsub(/"/,"",$2); print $2}'
done | sort -u | tr '\n' ' ')"
[[ -n "${TAGS// /}" ]] || { echo "태그를 못 찾음: 중단"; exit 1; }
echo "복사할 태그: $(echo $TAGS)"

export ECR SRC_REPO DST_REPO TAGS
GHCR_TOKEN="$(gh auth token)" ECR_TOKEN="$(aws ecr get-login-password --region "$REGION")" \
docker run --rm -e GHCR_TOKEN -e ECR_TOKEN -e ECR -e SRC_REPO -e DST_REPO -e TAGS \
  --entrypoint sh gcr.io/go-containerregistry/crane:debug -c '
    set -e
    printf %s "$GHCR_TOKEN" | crane auth login ghcr.io -u heung115 --password-stdin >/dev/null
    printf %s "$ECR_TOKEN"  | crane auth login "$ECR" -u AWS --password-stdin >/dev/null
    for t in $TAGS; do
      crane copy --platform linux/amd64 "$SRC_REPO:$t" "$DST_REPO:$t" 2>/dev/null && echo "  $t 복사 완료"
    done'

aws ecr describe-images --region "$REGION" --repository-name onebite/tmp-onebite-be \
  --query 'imageDetails[].imageTags[]' --output text
