# EKS: ~/.kube/config 대신 EKS 모듈 출력값 + aws eks get-token 으로 접속한다.
# 홈서버 kubeconfig 를 가리킬 경로 자체가 없다.
provider "helm" {
  # 이 Mac 의 전역 helm repo 목록/캐시(~/Library/...)와 분리된 프로젝트 전용 경로
  repository_config_path = "${path.module}/../.helm/repositories.yaml"
  repository_cache       = "${path.module}/../.helm/cache"

  kubernetes {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
    }
  }
}

provider "kubernetes" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name, "--region", var.region]
  }
}

# EKS: ingress-nginx(+MetalLB) 대신 AWS Load Balancer Controller 가 ALB 를 만든다.
resource "helm_release" "aws_lbc" {
  name       = "aws-load-balancer-controller"
  chart      = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  version    = "3.5.0"
  namespace  = "kube-system"

  wait = true

  values = [
    yamlencode({
      clusterName = module.eks.cluster_name
      region      = var.region
      vpcId       = module.vpc.vpc_id
      serviceAccount = {
        create = true
        name   = "aws-load-balancer-controller"
      }
      # LoadBalancer 타입 Service 는 쓰지 않는다. 컨트롤러 준비 전 Service 생성이 웹훅에 막히지 않게 끈다.
      enableServiceMutatorWebhook = false
      # ALB, Target Group, SG 에도 태그 → destroy 후 태그로 잔여 확인
      defaultTags = local.tags
      # ingressClassName = "alb" 인 Ingress 공통 설정: 세 환경 Ingress 6개가 공개 ALB 1개를 같이 쓴다.
      ingressClassParams = {
        create = true
        spec = {
          group      = { name = "onebite" }
          scheme     = "internet-facing"
          targetType = "ip"
        }
      }
    })
  ]

  depends_on = [module.eks, aws_eks_pod_identity_association.lbc]
}

# EKS 에는 기본 StorageClass 가 없다. postgres/redis PVC 가 gp3 EBS 를 받게 기본값으로 둔다.
resource "kubernetes_storage_class_v1" "gp3" {
  metadata {
    name = "gp3"
    annotations = {
      "storageclass.kubernetes.io/is-default-class" = "true"
    }
  }

  storage_provisioner    = "ebs.csi.aws.com"
  reclaim_policy         = "Delete"
  volume_binding_mode    = "WaitForFirstConsumer"
  allow_volume_expansion = true

  parameters = {
    type      = "gp3"
    encrypted = "true"
  }

  depends_on = [module.eks]
}

# EKS: 앱 시크릿은 Secrets Manager → External Secrets 로 주입한다 (ClusterSecretStore + ExternalSecret).
resource "helm_release" "eks_secrets" {
  name      = "onebite-secrets"
  chart     = "${path.module}/../charts/eks-secrets"
  namespace = "external-secrets"

  values = [
    yamlencode({
      region = var.region
      envs   = var.envs
    })
  ]

  depends_on = [
    helm_release.external_secrets,
    kubernetes_namespace.env,
  ]
}

# EKS: ArgoCD 가 GitOps eks 브랜치를 보게 연결한다 (저장소 키 + 루트 Application).
# backend 는 홈서버와 같이 ArgoCD 가 배포한다.
resource "helm_release" "eks_argocd" {
  name      = "onebite-argocd"
  chart     = "${path.module}/../charts/eks-argocd"
  namespace = "argocd"

  depends_on = [
    helm_release.argocd,
    helm_release.eks_secrets,
    helm_release.postgresql,
    helm_release.redis,
    helm_release.ai-backend,
    kubernetes_secret.db,
    kubernetes_config_map.timezone_prod,
    kubernetes_config_map.timezone_dev_front,
    kubernetes_config_map.timezone_dev_back,
  ]
}
