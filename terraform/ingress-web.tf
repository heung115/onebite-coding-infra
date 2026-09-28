resource "kubernetes_ingress_v1" "ingress_web" {
  for_each = {
    for env in var.envs : env => var.env_domains_web[env]
  }

  metadata {
    name      = "${each.key}-ingress-web"
    namespace = each.value.namespace
    annotations = {
    }
  }
  depends_on = [kubernetes_namespace.env]

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
}
