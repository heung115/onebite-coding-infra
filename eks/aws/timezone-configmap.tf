# 모든 네임스페이스에 시간대 ConfigMap 생성
resource "kubernetes_config_map" "timezone_default" {
  depends_on = [module.eks] # EKS
  metadata {
    name      = "timezone-config"
    namespace = "default"
  }

  data = {
    TZ = "Asia/Seoul"
  }
}

resource "kubernetes_config_map" "timezone_prod" {
  depends_on = [kubernetes_namespace.env] # EKS
  metadata {
    name      = "timezone-config"
    namespace = "prod"
  }

  data = {
    TZ = "Asia/Seoul"
  }
}

resource "kubernetes_config_map" "timezone_dev_front" {
  depends_on = [kubernetes_namespace.env] # EKS
  metadata {
    name      = "timezone-config"
    namespace = "dev-front"
  }

  data = {
    TZ = "Asia/Seoul"
  }
}

resource "kubernetes_config_map" "timezone_dev_back" {
  depends_on = [kubernetes_namespace.env] # EKS
  metadata {
    name      = "timezone-config"
    namespace = "dev-back"
  }

  data = {
    TZ = "Asia/Seoul"
  }
}
