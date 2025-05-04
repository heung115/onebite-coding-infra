resource "helm_release" "loki" {
  name       = "loki"
  namespace  = "observability"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "loki-stack"
  #   version          = "2.9.1"
  create_namespace = true
  values = [
    file("${path.module}/../values/loki.yaml")
  ]
}
