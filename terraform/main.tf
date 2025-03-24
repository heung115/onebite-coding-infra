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

resource "helm_release" "backend" {
  name = "backend"
  chart = "${path.module}/../charts/backend"
  namespace = kubernetes_namespace.backend.metadata[0].name
  values = [
    file("${path.module}/../charts/backend/values.yaml"),
    file("${path.module}/values/backend.yaml")]
}