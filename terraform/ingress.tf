resource "kubernetes_ingress_v1" "ingress" {
  for_each = {
    for env in var.envs : env => var.env_domains[env]
  }

  metadata {
    name      = "${each.key}-ingress"
    namespace = each.value.namespace
    annotations = {
      "nginx.ingress.kubernetes.io/rewrite-target" = "/"
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
            path      = path.value.path
            path_type = "Prefix"

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
}
