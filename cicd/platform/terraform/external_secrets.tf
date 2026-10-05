# 시크릿을 Git 밖에서 주입 (External Secrets Operator)
# 로컬(홈서버)에서는 클러스터 내부 Kubernetes Secret 을 저장소로 쓰는 provider 로 시작하고,
# EKS 이전 시 SecretStore 만 AWS Secrets Manager 로 바꾼다.
resource "helm_release" "external_secrets" {
  name             = "external-secrets"
  namespace        = "external-secrets"
  create_namespace = true
  repository       = "https://charts.external-secrets.io"
  chart            = "external-secrets"
  version          = "2.11.0"
  timeout          = 600

  set {
    name  = "installCRDs"
    value = "true"
  }
}
