variable "envs" {
  type    = list(string)
  default = ["dev-front", "dev-back", "prod"]
  # default = ["dev-back", "prod"]
  # default = ["dev-back"]

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

variable "env_domains" {
  type = map(object({
    namespace = string
    domain    = string
    paths = list(object({
      service_name = string
      path         = string
      port         = number
    }))
  }))

  default = {
    dev-front = {
      namespace = "dev-front"
      domain    = "one-bite-df.duckdns.org"
      paths = [
        {
          service_name = "nextjs-dev-front-web"
          path         = "/"
          port         = 3000
        },
        {
          service_name = "backend-dev-front"
          path         = "/api"
          port         = 8080
        }
      ]
    }

    dev-back = {
      namespace = "dev-back"
      domain    = "one-bite-db.duckdns.org"
      paths = [
        {
          service_name = "nextjs-dev-back-web"
          path         = "/"
          port         = 3000
        },
        {
          service_name = "backend-dev-back"
          path         = "/api"
          port         = 8080
        }
      ]
    }

    prod = {
      namespace = "prod"
      domain    = "one-bite.duckdns.org"
      paths = [
        {
          service_name = "nextjs-prod-web"
          path         = "/"
          port         = 3000
        },
        {
          service_name = "backend-prod"
          path         = "/api"
          port         = 8080
        }
      ]
    }
  }
}
