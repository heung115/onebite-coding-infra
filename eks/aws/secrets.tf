# Secrets Manager 시크릿 "그릇"만 만든다. 값은 Terraform state 에 남지 않도록
# scripts/put-secrets.sh 가 원본 values/*.yaml 에서 읽어 put-secret-value 로 넣는다.
# 이름은 GitOps ExternalSecret 의 extract key(backend-<env>)와 맞춘다.
locals {
  secret_names = concat(
    [for e in var.envs : "backend-${e}"],
    [
      "onebite-postgresql",  # postgresql auth
      "onebite-redis",       # redis-password
      "onebite-ai-backend",  # GEMINI_API_KEY 등
      "onebite-argocd-repo", # ArgoCD 가 GitOps 저장소를 읽는 deploy key
    ]
  )
}

resource "aws_secretsmanager_secret" "this" {
  for_each = toset(local.secret_names)

  name        = each.key
  description = "onebite EKS temporary"

  # destroy 즉시 삭제 (복구 대기 없음, 같은 이름 재생성 가능)
  recovery_window_in_days = 0
}

# 백엔드 이미지(private GHCR)를 같은 계정 ECR 로 복사해 노드 IAM(ECR ReadOnly)으로 받는다.
# GHCR 토큰을 클러스터에 넣지 않기 위함. scripts/sync-backend-image.sh 가 복사한다.
resource "aws_ecr_repository" "backend" {
  name                 = "onebite/tmp-onebite-be"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}
