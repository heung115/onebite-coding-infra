#!/usr/bin/env bash
# onebite EKS 임시 환경 정리
# 순서: 앱 삭제 → Ingress/PVC 삭제 → ALB/EBS 소멸 확인 → terraform destroy → 태그로 잔여 확인
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KCFG="$HOME/.kube/onebite-eks.kubeconfig"
REGION="ap-northeast-2"
CLUSTER="onebite-eks"
ENVS=(dev-front dev-back prod)
cd "$ROOT/aws"

ACCT="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCT" == *0497 ]] || { echo "계정 불일치: 중단"; exit 1; }

k() { KUBECONFIG="$KCFG" kubectl "$@"; }
tfd() { terraform destroy -input=false -auto-approve "$@"; }

if aws eks describe-cluster --region "$REGION" --name "$CLUSTER" >/dev/null 2>&1; then
  aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER" --kubeconfig "$KCFG" --alias onebite-eks >/dev/null

  echo "[1/5] 앱 삭제"
  # ArgoCD 루트 앱 → backend Application 3개 (finalizer 로 배포 리소스까지 삭제)
  tfd -target=helm_release.eks_argocd
  k -n argocd delete applications.argoproj.io --all --wait=true --timeout=5m 2>/dev/null || true
  # Terraform 이 올린 앱
  tfd -target=helm_release.nextjs -target=helm_release.ai-backend \
      -target=helm_release.postgresql -target=helm_release.redis

  echo "[2/5] Ingress, PVC 삭제"
  tfd -target=kubernetes_ingress_v1.ingress-api -target=kubernetes_ingress_v1.ingress_web
  for ns in "${ENVS[@]}"; do
    k -n "$ns" delete ingress --all --wait=true --timeout=5m 2>/dev/null || true
    k -n "$ns" delete pvc --all --wait=true --timeout=5m 2>/dev/null || true
  done
else
  echo "[1-2/5] 클러스터 없음: k8s 정리 건너뜀"
fi

echo "[3/5] ALB / EBS 소멸 대기 (최대 10분)"
for i in $(seq 1 60); do
  LB="$(aws elbv2 describe-load-balancers --region "$REGION" --output json \
    | jq '[.LoadBalancers[]|select(.LoadBalancerName|startswith("k8s-onebite"))]|length')"
  VOL="$(aws ec2 describe-volumes --region "$REGION" \
    --filters Name=tag-key,Values=kubernetes.io/created-for/pvc/name Name=tag:Project,Values=onebite \
    --query 'length(Volumes)' --output text)"
  echo "  ALB=$LB, PVC EBS=$VOL"
  [[ "$LB" == "0" && "$VOL" == "0" ]] && break
  if [[ "$i" == "60" ]]; then echo "ALB/EBS 가 남아 있음: 확인 후 다시 실행"; exit 1; fi
  sleep 10
done

echo "[4/5] terraform destroy"
# 노드를 먼저 지우고, VPC CNI 가 남긴 분리된 ENI(보안그룹 삭제를 막음)를 치운 뒤 나머지를 지운다
tfd -target=module.eks.module.eks_managed_node_group
VPC_ID="$(terraform output -raw vpc_id 2>/dev/null || true)"
if [[ -n "$VPC_ID" ]]; then
  for eni in $(aws ec2 describe-network-interfaces --region "$REGION" \
      --filters Name=vpc-id,Values="$VPC_ID" Name=status,Values=available \
                Name=tag:cluster.k8s.amazonaws.com/name,Values="$CLUSTER" \
      --query 'NetworkInterfaces[].NetworkInterfaceId' --output text); do
    aws ec2 delete-network-interface --region "$REGION" --network-interface-id "$eni" && echo "  남은 CNI ENI 삭제: $eni"
  done
fi
tfd

# ArgoCD 용 deploy key 도 회수 (개인키는 Secrets Manager 와 함께 삭제됨, 다음 up.sh 가 새로 만든다)
for id in $(gh api repos/heung115/tmp-onebite-gitops/keys --jq '.[]|select(.title=="argocd-eks-readonly")|.id'); do
  gh api -X DELETE "repos/heung115/tmp-onebite-gitops/keys/$id" >/dev/null && echo "  deploy key argocd-eks-readonly 삭제"
done

echo "[5/5] 태그(Project=onebite, Temporary=true)로 잔여 리소스 확인"
# 태그 API 는 삭제 후에도 한동안 기록을 돌려주므로, 실제 존재하는 리소스를 서비스별로 직접 확인한다
# macOS 기본 bash 3.2 호환 (연관 배열 없이)
TOTAL=0
chk() {  # chk 이름 명령...  → 개수 출력 후 합산 (명령 실패/빈 값은 0)
  local name="$1"; shift
  local n; n="$("$@" 2>/dev/null || true)"; n="${n:-0}"
  [[ "$n" =~ ^[0-9]+$ ]] || n=0
  printf '  %-7s %s\n' "$name" "$n"
  TOTAL=$((TOTAL + n))
}
TF=Name=tag:Project,Values=onebite
chk eks    aws eks list-clusters --region "$REGION" --query "length(clusters[?@=='$CLUSTER'])" --output text
chk ec2    aws ec2 describe-instances --region "$REGION" --filters "$TF" Name=instance-state-name,Values=pending,running,stopping,stopped --query 'length(Reservations)' --output text
chk nat    aws ec2 describe-nat-gateways --region "$REGION" --filter "$TF" Name=state,Values=pending,available,deleting --query 'length(NatGateways)' --output text
chk eip    aws ec2 describe-addresses --region "$REGION" --filters "$TF" --query 'length(Addresses)' --output text
chk ebs    aws ec2 describe-volumes --region "$REGION" --filters "$TF" --query 'length(Volumes)' --output text
chk eni    aws ec2 describe-network-interfaces --region "$REGION" --filters "$TF" --query 'length(NetworkInterfaces)' --output text
chk vpc    aws ec2 describe-vpcs --region "$REGION" --filters "$TF" --query 'length(Vpcs)' --output text
chk alb    aws elbv2 describe-load-balancers --region "$REGION" --query "length(LoadBalancers[?starts_with(LoadBalancerName,'k8s-onebite')])" --output text
chk secret aws secretsmanager list-secrets --region "$REGION" --include-planned-deletion --filters Key=tag-key,Values=Project --query 'length(SecretList)' --output text
chk ecr    aws ecr describe-repositories --region "$REGION" --query "length(repositories[?repositoryName=='onebite/tmp-onebite-be'])" --output text
chk iam    aws iam list-roles --query "length(Roles[?starts_with(RoleName,'onebite-eks') || contains(RoleName,'-eks-node-group-')])" --output text
if [[ "$TOTAL" == "0" ]]; then
  echo "잔여 리소스 없음"
else
  echo "남은 리소스 $TOTAL 개: 위 항목 확인"
  exit 2
fi
