#!/usr/bin/env bash
# onebite EKS 올리기 (처음/재기동 동일). 약 20~25분.
#   1) AWS 계층 (VPC, EKS, 노드, IAM, Secrets Manager, ECR)
#   2) 시크릿 값 → Secrets Manager, ArgoCD deploy key, 백엔드 이미지 → ECR
#   3) 한입코딩 k8s 계층 (namespace, postgres, redis, nextjs, ai, ingress, ArgoCD, ESO) + ArgoCD 루트 앱
#   4) kubeconfig (~/.kube/onebite-eks.kubeconfig 에만)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$ROOT/_logs"; mkdir -p "$LOG"
cd "$ROOT/aws"

ACCT="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCT" == *0497 ]] || { echo "계정 불일치: 중단"; exit 1; }

# 작업 PC 공인 IP 가 바뀌면 EKS API 허용 CIDR 갱신
MYIP="$(curl -s https://checkip.amazonaws.com | tr -d '\n')"
printf 'allowed_account_id = "%s"\nadmin_cidrs        = ["%s/32"]\n' "$ACCT" "$MYIP" > terraform.tfvars
chmod 600 terraform.tfvars

terraform init -input=false >/dev/null

echo "[1/4] AWS 계층"
terraform apply -input=false -auto-approve \
  -target=module.vpc -target=module.eks \
  -target=aws_secretsmanager_secret.this -target=aws_ecr_repository.backend \
  -target=aws_eks_pod_identity_association.lbc -target=aws_eks_pod_identity_association.eso \
  -target=aws_iam_role_policy.eso -target=aws_iam_role_policy_attachment.lbc | tee "$LOG/up-1-aws.log" | grep -E '^Apply complete|Error' || true

echo "[2/4] 시크릿, deploy key, 백엔드 이미지"
"$ROOT/scripts/put-secrets.sh"
"$ROOT/scripts/argocd-repo-key.sh"
"$ROOT/scripts/sync-backend-image.sh"

echo "[3/4] 한입코딩 k8s 계층"
terraform apply -input=false -auto-approve | tee "$LOG/up-3-k8s.log" | grep -E '^Apply complete|Error' || true

echo "[4/4] kubeconfig"
aws eks update-kubeconfig --region ap-northeast-2 --name onebite-eks \
  --kubeconfig "$HOME/.kube/onebite-eks.kubeconfig" --alias onebite-eks >/dev/null
chmod 600 "$HOME/.kube/onebite-eks.kubeconfig"

"$ROOT/scripts/check.sh"
