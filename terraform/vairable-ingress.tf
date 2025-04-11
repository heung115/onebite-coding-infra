variable "env_domains_api" {
  type = map(object({
    namespace = string
    domain    = string
    paths = list(object({
      service_name = string
      path         = string
      port         = number
      path_type    = optional(string, "ImplementationSpecific")
    }))
  }))

  default = {
    dev-front = {
      namespace = "dev-front"
      domain    = "one-bite-df.duckdns.org"
      paths = [
        {
          service_name = "backend-dev-front"
          path         = "/api(/|$)(.*)"
          port         = 8080
        }
      ]
    }

    dev-back = {
      namespace = "dev-back"
      domain    = "one-bite-db.duckdns.org"
      paths = [
        {
          service_name = "backend-dev-back"
          path         = "/api(/|$)(.*)"
          port         = 8080
        }
      ]
    }

    prod = {
      namespace = "prod"
      domain    = "one-bite.duckdns.org"
      paths = [
        {
          service_name = "backend-prod"
          path         = "/api(/|$)(.*)"
          port         = 8080
        }
      ]
    }
  }
}

variable "env_domains_web" {
  type = map(object({
    namespace = string
    domain    = string
    paths = list(object({
      service_name = string
      path         = string
      port         = number
      path_type    = optional(string, "Prefix")
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
          path_type    = "Prefix"
        }
      ]
    }
  }
}
