locals {
  env_domain_map = zipmap(var.envs, var.domains)
}


resource "helm_release" "backend" {
  for_each = {
    for ns in var.envs : ns => ns
  }

  timeout = 600
  name    = "backend-${each.key}"
  chart   = "${path.module}/../charts/backend"
  wait    = true
  atomic  = true

  namespace = kubernetes_namespace.env[each.key].metadata[0].name
  values = [
    file("${path.module}/../charts/backend/values.yaml"),
  file("${path.module}/../values/backend.yaml")]

  set {
    name  = "image.tag"
    value = each.key == "dev-back" ? "dev" : "latest"
  }

  set {
    name  = "db.secretName"
    value = "db-secret-${each.key}"
  }

  set {
    name  = "secret.GOOGLE_REDIRECT_URI"
    value = "http://${local.env_domain_map[each.key]}/login/oauth"
  }
  depends_on = [
    kubernetes_namespace.env,
    kubernetes_secret.db
  ]
}

