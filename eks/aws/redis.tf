# EKS: redis-secret 은 코드 안 평문 대신 Secrets Manager(onebite-redis) → ExternalSecret 이 만든다.
# (charts/eks-secrets/templates/external-secrets.yaml)

resource "helm_release" "redis" {
  for_each = {
    for ns in var.envs : ns => ns
  }

  name      = "redis-${each.key}"
  chart     = "${path.module}/../charts/redis/redis-20.11.4.tgz"
  namespace = kubernetes_namespace.env[each.key].metadata[0].name
  values = [
    file("${path.module}/../values/redis.yaml"),
    file("${path.module}/../values-eks/redis.yaml"),
  ]
  depends_on = [
    kubernetes_namespace.env,
    kubernetes_secret.db,
    helm_release.eks_secrets,
    kubernetes_storage_class_v1.gp3,
  ]
}
