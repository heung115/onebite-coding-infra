resource "kubernetes_ingress_v1" "https_ingress" {
  metadata {
    name      = "one-bite-web-https-ingress"
    namespace = "prod"
    annotations = {
      "nginx.ingress.kubernetes.io/rewrite-target" = "/"
      #   "cert-manager.io/cluster-issuer"                 = "letsencrypt-prod"
      "nginx.ingress.kubernetes.io/force-ssl-redirect" = "true"
    }
  }
  depends_on = [kubernetes_namespace.env]
  spec {
    ingress_class_name = "nginx"

    tls {
      hosts       = ["one-bite.dev"]
      secret_name = "one-bite-dev-tls"
    }

    rule {
      host = "one-bite.dev"
      http {
        path {
          path      = "/api"
          path_type = "Prefix"
          backend {
            service {
              name = "backend-prod"
              port {
                number = 8080
              }
            }
          }
        }

        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = "nextjs-prod-web"
              port {
                number = 3000
              }
            }
          }
        }
      }
    }
  }
}
