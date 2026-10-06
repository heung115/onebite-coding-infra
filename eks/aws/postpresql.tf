resource "helm_release" "postgresql" {
  for_each = {
    for ns in var.envs : ns => ns
  }
  # 헬름 차트를 배포할때는 모두 이름이 달라야함.
  # 이름으로 릴리즈를 만드는데 릴리즈 이름이 같으면 버젼 추적이 안되어서 네임 스페이스가 다르더더라도 릴리즈는 달라야함.
  name       = "postgresql-${each.key}"
  repository = "https://charts.bitnami.com/bitnami"
  chart      = "${path.module}/../charts/postgresql/postgresql-16.4.5.tgz"
  namespace  = kubernetes_namespace.env[each.key].metadata[0].name
  version    = "12.5.7"

  values = [
    file("${path.module}/../values/postgresql.yaml"),
    file("${path.module}/../values-eks/postgresql.yaml"),
  ]
  depends_on = [
    kubernetes_namespace.env,
    kubernetes_secret.db,
    helm_release.eks_secrets,
    kubernetes_storage_class_v1.gp3,
  ]

}
