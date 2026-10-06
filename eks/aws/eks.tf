locals {
  tags = {
    Project   = "onebite"
    Temporary = "true"
  }

  azs = ["${var.region}a", "${var.region}c"]
}

################################
# VPC: AZ 2개, public/private, NAT 1개
################################
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3"

  name = var.name
  cidr = "10.40.0.0/16"
  azs  = local.azs

  public_subnets  = ["10.40.0.0/20", "10.40.16.0/20"]
  private_subnets = ["10.40.128.0/19", "10.40.160.0/19"]

  enable_nat_gateway     = true
  single_nat_gateway     = true
  one_nat_gateway_per_az = false

  enable_dns_hostnames = true
  enable_dns_support   = true

  # AWS Load Balancer Controller 가 ALB 서브넷을 찾는 태그
  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
  }

  tags = local.tags
}

################################
# EKS 1.35 + m7i-flex.large 온디맨드 2대
# 무료 플랜 계정은 free-tier-eligible 타입만 RunInstances 가 허용된다 (t3.large 는 거부).
# t3.large 와 같은 2 vCPU / 8 GiB / x86_64 인 m7i-flex.large 를 쓴다.
################################
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.26.0"

  name               = var.name
  kubernetes_version = var.kubernetes_version

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  endpoint_public_access       = true
  endpoint_private_access      = true
  endpoint_public_access_cidrs = var.admin_cidrs

  enable_cluster_creator_admin_permissions = true
  authentication_mode                      = "API"

  # 파드 권한은 Pod Identity 로 준다 (IRSA OIDC provider 불필요)
  enable_irsa = false

  # 임시 클러스터: KMS 키(삭제 대기 7일+)와 CloudWatch 로그 비용을 만들지 않는다.
  create_kms_key              = false
  encryption_config           = null
  enabled_log_types           = []
  create_cloudwatch_log_group = false

  addons = {
    vpc-cni = {
      before_compute = true
    }
    eks-pod-identity-agent = {
      before_compute = true
    }
    kube-proxy = {}
    coredns    = {}
    aws-ebs-csi-driver = {
      pod_identity_association = [{
        # 정책 연결이 끝난 뒤 애드온을 만들게 attachment 의 role 값을 참조한다
        # (role.arn 만 쓰면 권한 없이 애드온이 먼저 떠서 CrashLoop → 20분 대기)
        role_arn        = "arn:aws:iam::${var.allowed_account_id}:role/${aws_iam_role_policy_attachment.ebs_csi.role}"
        service_account = "ebs-csi-controller-sa"
      }]
      # CSI 가 만드는 EBS 에도 태그 (destroy 후 태그로 잔여 확인)
      configuration_values = jsonencode({
        controller = {
          extraVolumeTags = local.tags
        }
      })
    }
  }

  eks_managed_node_groups = {
    main = {
      ami_type       = "AL2023_x86_64_STANDARD"
      instance_types = ["m7i-flex.large"]
      capacity_type  = "ON_DEMAND"

      min_size     = 2
      max_size     = 2
      desired_size = 2

      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 30
            volume_type           = "gp3"
            delete_on_termination = true
          }
        }
      }

      tags = local.tags
    }
  }

  tags = local.tags
}
