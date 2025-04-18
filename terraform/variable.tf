variable "envs" {
  type    = list(string)
  default = ["dev-front", "dev-back", "prod"]
  # default = ["dev-back", "prod"]
  # default = ["dev-back"]

}

variable "domains" {
  type    = list(string)
  default = ["one-bite-df.duckdns.org", "one-bite-db.duckdns.org", "one-bite.duckdns.org"]
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
