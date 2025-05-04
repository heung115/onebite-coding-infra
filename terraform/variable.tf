variable "envs" {
  type    = list(string)
  default = ["dev-front", "dev-back", "prod"]
  # default = ["dev-back", "prod"]
  # default = ["dev-back"]

}

variable "domains" {
  type = list(string)
  # default = ["one-bite-df.site", "one-bite-db.site", "one-bite.dev"]
  default = ["one-bite-fe.site", "one-bite-be.site", "one-bite.dev"]

}

resource "kubernetes_secret" "db" {
  for_each = toset(var.envs)

  metadata {
    name      = "db-secret-${each.key}"
    namespace = each.key
  }

  data = {
    DB_URI     = "jdbc:postgresql://postgresql-${each.key}:5432/testdatabase"
    REDIS_HOST = "redis-${each.key}-master"
  }

  type = "Opaque"
}

resource "kubernetes_secret" "cloudflare_api_token" {
  metadata {
    name      = "cloudflare-api-token-secret"
    namespace = "cert-manager"
  }

  data = {
    api-token = "***REMOVED-CLOUDFLARE-TOKEN***"
  }

  type = "Opaque"
}
