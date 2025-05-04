resource "helm_release" "promtail" {
  name             = "promtail"
  namespace        = "observability"
  repository       = "https://grafana.github.io/helm-charts"
  chart            = "promtail"
  version          = "6.6.2"
  create_namespace = true
  values = [
    file("${path.module}/../values/promtail.yaml")
  ]
}
