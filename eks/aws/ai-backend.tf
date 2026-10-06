resource "helm_release" "ai-backend" {
  for_each = {
    for ns in var.envs : ns => ns
  }

  timeout = 300
  name    = "ai-backend-${each.key}"
  chart   = "${path.module}/../charts/backend"
  wait    = true
  atomic  = false

  namespace = kubernetes_namespace.env[each.key].metadata[0].name
  values = [
    file("${path.module}/../charts/backend/values.yaml"),
    file("${path.module}/../values/ai-backend.yaml"),
  file("${path.module}/../values-eks/ai-backend.yaml")]

  set {
    name  = "image.tag"
    value = each.key == "dev-back" ? "dev" : "latest"
  }

  depends_on = [
    kubernetes_namespace.env,
    kubernetes_secret.db,
    helm_release.eks_secrets,
  ]
}
