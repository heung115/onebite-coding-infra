resource "kubernetes_ingress_v1" "ingress-api" {
  for_each = {
    for env in var.envs : env => var.env_domains_api[env]
  }

  metadata {
    name      = "${each.key}-ingress-api"
    namespace = each.value.namespace
    annotations = {
      # ingress 컨트롤러가 요청 경로를 서비스에 전달하기 전에 재작성하는 규칙
      # 지금은 슬래시 + 그 뒤 경로만 남겨서 전달.
      "nginx.ingress.kubernetes.io/rewrite-target" = "/$2"
    }
  }

  spec {
    ingress_class_name = "nginx"

    rule {
      host = each.value.domain

      http {
        dynamic "path" {
          for_each = each.value.paths
          content {
            path = path.value.path
            # path_type = "ImplementationSpecific"
            path_type = lookup(path.value, "path_type", "ImplementationSpecific")

            backend {
              service {
                name = path.value.service_name
                port {
                  number = path.value.port
                }
              }
            }
          }
        }
      }
    }
  }
  depends_on = [kubernetes_namespace.env]
}
