resource "helm_release" "backend" {
  for_each = {
    for ns in var.envs : ns => ns
  }

  name      = "backend-${each.key}"
  chart     = "${path.module}/../charts/backend"
  namespace = kubernetes_namespace.env[each.key].metadata[0].name
  values = [
    file("${path.module}/../charts/backend/values.yaml"),
  file("${path.module}/../values/backend.yaml")]

  set {
    name  = "image.tag"
    value = each.key == "dev-back" ? "dev" : "latest"
  }
}

