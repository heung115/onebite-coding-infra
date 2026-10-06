# onebite-eks: AWS 계층 (VPC, EKS, 노드, IAM, Secrets Manager)
# 홈서버(onebite-infra-homeserver) 와 state 를 공유하지 않는다. 이 폴더의 local state 만 쓴다.
terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.66.0"
    }
    # 홈서버 terraform 과 같은 버전 (기존 set {} 문법 그대로 사용)
    helm = {
      source  = "hashicorp/helm"
      version = "2.17.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "2.37.1"
    }
  }
}

provider "aws" {
  region = var.region

  # 다른 계정 자격증명으로는 plan/apply 자체가 실패한다.
  allowed_account_ids = [var.allowed_account_id]

  default_tags {
    tags = local.tags
  }
}
