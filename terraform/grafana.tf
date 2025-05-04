resource "helm_release" "grafana" {
  name       = "grafana"
  namespace  = "monitoring"
  repository = "https://grafana.github.io/helm-charts"
  chart      = "grafana"
  version    = "8.13.1"
  values = [
    file("${path.module}/../values/grafana.yaml")
  ]
}
