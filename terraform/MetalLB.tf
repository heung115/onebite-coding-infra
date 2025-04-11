resource "helm_release" "metallb" {
  name             = "metallb"
  namespace        = "metallb-system"
  create_namespace = true
  repository       = "https://metallb.github.io/metallb"
  chart            = "metallb"
  version          = "0.13.7"

  values = [
    yamlencode({
      configInline = {}
    })
  ]
  # crd : 커스텀 리소스 정의
  # pod, service같은 표준 리소스가 아닌 새로운 리소스를 추가로 정의할수 있게 해줌.
  # 이 상황에서는 metallb_ip_pool, metallb_l2_advertisement 를 추가로 정의해서 사용하고 있기 때문에
  # 필요하다
  # 근데 terraform apply하면 plan단계에서 설치되지 않은 상태로 검사하다보니 오류가 난다
  # 그래서 target으로 이 리소스를 먼저 설치해야한다. 그후에 전체 apply
  set {
    name  = "crds.enabled"
    value = "true"
  }
}

resource "kubernetes_manifest" "metallb_ip_pool" {
  depends_on = [helm_release.metallb]

  manifest = {
    apiVersion = "metallb.io/v1beta1"
    kind       = "IPAddressPool"
    metadata = {
      name      = "default-pool"
      namespace = "metallb-system"
    }
    spec = {
      addresses = [
        "192.168.0.220-192.168.0.250"
      ]
    }
  }
}

resource "kubernetes_manifest" "metallb_l2_advertisement" {
  depends_on = [helm_release.metallb]

  manifest = {
    apiVersion = "metallb.io/v1beta1"
    kind       = "L2Advertisement"
    metadata = {
      name      = "default"
      namespace = "metallb-system"
    }
    spec = {}
  }
}
