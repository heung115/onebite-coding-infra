variable "allowed_account_id" {
  description = "이 계정에서만 실행 (terraform.tfvars, Git 제외)"
  type        = string

  validation {
    condition     = can(regex("^[0-9]{8}0497$", var.allowed_account_id))
    error_message = "허용 계정(****0497)이 아닙니다."
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "name" {
  type    = string
  default = "onebite-eks"
}

variable "kubernetes_version" {
  type    = string
  default = "1.35"
}

variable "admin_cidrs" {
  description = "EKS API 공개 엔드포인트 허용 CIDR (작업 PC 공인 IP/32)"
  type        = list(string)
}

# envs, domains 는 기존 한입코딩 variable.tf 의 것을 그대로 쓴다.
