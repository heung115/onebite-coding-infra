resource "kubernetes_manifest" "one_bite_dev_certificate" {
  manifest = {
    apiVersion = "cert-manager.io/v1"
    kind       = "Certificate"
    metadata = {
      name      = "one-bite-dev-tls"
      namespace = "prod"
    }
    spec = {
      secretName = "one-bite-dev-tls"
      issuerRef = {
        name = "letsencrypt-cloudflare"
        kind = "ClusterIssuer"
      }
      commonName = "one-bite.dev"
      dnsNames   = ["one-bite.dev"]
    }
  }

  depends_on = [kubernetes_manifest.letsencrypt_cloudflare_issuer]
}
