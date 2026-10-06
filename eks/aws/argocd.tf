# 2026.09 후속 고도화: GitOps(ArgoCD) 전환
# Terraform 은 플랫폼 구성요소(ArgoCD 자체)만 설치한다.
# 앱(backend 등) 배포는 ArgoCD 가 Git 을 기준으로 담당하고, 같은 helm_release 를 Terraform 과 ArgoCD 가 동시에 관리하지 않게 한다.
resource "helm_release" "argocd" {
  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = "10.9.2"
  timeout          = 900

  values = [file("${path.module}/../values/argocd.yaml")]

  depends_on = [module.eks] # EKS
}
