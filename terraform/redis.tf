resource "kubernetes_secret" "redis_auth" {
  for_each = {
    for ns in var.envs : ns => ns
  }
  metadata {
    name      = "redis-secret"
    namespace = each.value
  }

  data = {
    redis-password = "***REMOVED-REDIS-PASSWORD***"
  }

  type = "Opaque"
}

resource "helm_release" "redis" {
  for_each = {
    for ns in var.envs : ns => ns
  }

  name      = "redis-${each.key}"
  chart     = "${path.module}/../charts/redis/redis-20.11.4.tgz"
  namespace = kubernetes_namespace.env[each.key].metadata[0].name
  values = [
    file("${path.module}/../values/redis.yaml")
  ]
}
