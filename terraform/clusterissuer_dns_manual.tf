resource "kubernetes_manifest" "letsencrypt_cloudflare_issuer" {
  manifest = {
    apiVersion = "cert-manager.io/v1"
    kind       = "ClusterIssuer"
    metadata = {
      name = "letsencrypt-cloudflare"
    }
    spec = {
      acme = {
        email  = "heung115@naver.com"
        server = "https://acme-v02.api.letsencrypt.org/directory"
        privateKeySecretRef = {
          name = "letsencrypt-cloudflare-key"
        }
        solvers = [{
          dns01 = {
            cloudflare = {
              apiTokenSecretRef = {
                name = "cloudflare-api-token-secret"
                key  = "api-token"
              }
            }
          }
        }]
      }
    }
  }

  depends_on = [kubernetes_secret.cloudflare_api_token]
}
