resource "kubernetes_ingress_v1" "ingress-api" {
  for_each = {
    for env in var.envs : env => var.env_domains_api[env]
  }

  metadata {
    name      = "${each.key}-ingress-api"
    namespace = each.value.namespace
    annotations = {
      # EKS: nginx rewrite-target("/$2") 대신 ALB URL rewrite 로 /api 접두사를 떼고 전달
      "alb.ingress.kubernetes.io/transforms.${each.value.paths[0].service_name}" = jsonencode([{
        type = "url-rewrite"
        urlRewriteConfig = {
          rewrites = [{ regex = "^/api/?(.*)$", replace = "/$1" }]
        }
      }])
      "alb.ingress.kubernetes.io/healthcheck-path" = "/test"
      # /api 규칙이 / 규칙보다 먼저 평가되게 web(20)보다 낮은 order
      "alb.ingress.kubernetes.io/group.order" = "10"
    }
  }

  spec {
    ingress_class_name = "alb"

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
  depends_on = [kubernetes_namespace.env, helm_release.aws_lbc]
}
