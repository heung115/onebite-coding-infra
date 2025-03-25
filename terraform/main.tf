provider "helm" {    
    kubernetes {
      config_path = "~/.kube/config"
    }
}

provider "kubernetes" {
    config_path = "~/.kube/config"
}

resource "kubernetes_namespace" "backend" {
  metadata {
    name = "backend"
  }
}
variable "envs" {
  type    = list(string)
  default = ["dev-front","dev-back", "prod"]
}

resource "kubernetes_namespace" "env" {
  for_each = toset(var.envs)
  metadata {
    name = each.value
  }
}
resource "helm_release" "backend" {
  name = "backend"
  chart = "${path.module}/../charts/backend"
  namespace = kubernetes_namespace.backend.metadata[0].name
  values = [
    file("${path.module}/../charts/backend/values.yaml"),
    file("${path.module}/../values/backend.yaml")]
}