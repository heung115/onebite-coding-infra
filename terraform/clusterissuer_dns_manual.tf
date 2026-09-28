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
          selector = {
            dnsNames = ["one-bite.dev"]
          }
          dns01 = {
            cloudflare = {
              apiTokenSecretRef = {
                name = "cloudflare-api-token-secret"
                key  = "api-token"
              },
              zoneID = "d98ab990b43be1f20eea5c756cfe6dc3"
            }
          }
        }]
      }
    }
  }

  depends_on = [kubernetes_secret.cloudflare_api_token]
}
