resource "helm_release" "nextjs" {
  for_each = {
    for ns in var.envs : ns => ns
  }

  name      = "nextjs-${each.key}"
  chart     = "${path.module}/../charts/nextjs"
  namespace = kubernetes_namespace.env[each.key].metadata[0].name

  values = [
    file("${path.module}/../values/frontend.yaml")
  ]

  set {
    name  = "web.image"
    value = "heung115/spaghetti-fe:${each.key == "dev-front" ? "dev" : "latest"}"
  }
}
