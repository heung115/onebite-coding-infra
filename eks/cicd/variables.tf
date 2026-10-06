variable "allowed_account_id" {
  description = "AWS account allowed to create the CI/CD cluster"
  type        = string
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "admin_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint"
  type        = list(string)
}

variable "kubernetes_version" {
  type    = string
  default = "1.35"
}

locals {
  name = "onebite-cicd"
  tags = {
    Project   = "onebite-cicd"
    Temporary = "true"
  }
  azs = ["${var.region}a", "${var.region}c"]
}
