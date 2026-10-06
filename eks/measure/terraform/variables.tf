variable "allowed_account_id" {
  description = "AWS account guard; supplied from the verified caller identity by scripts/tf.sh."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{8}0497$", var.allowed_account_id))
    error_message = "Terraform is restricted to the AWS account ending in 0497."
  }
}

variable "region" {
  description = "The temporary experiment is restricted to Seoul."
  type        = string
  default     = "ap-northeast-2"

  validation {
    condition     = var.region == "ap-northeast-2"
    error_message = "This experiment must run in ap-northeast-2."
  }
}

variable "cluster_name" {
  type    = string
  default = "onebite-eks-measure"
}

variable "kubernetes_version" {
  type    = string
  default = "1.35"
}

variable "admin_cidrs" {
  description = "Public IPv4 CIDR allowed to reach the EKS API; set to the operator egress IP /32."
  type        = list(string)

  validation {
    condition     = length(var.admin_cidrs) > 0 && alltrue([for cidr in var.admin_cidrs : can(cidrnetmask(cidr))])
    error_message = "Supply at least one valid IPv4 CIDR for the EKS API endpoint."
  }
}

variable "availability_zones" {
  type    = list(string)
  default = ["ap-northeast-2a", "ap-northeast-2c"]

  validation {
    condition     = length(var.availability_zones) == 2 && alltrue([for az in var.availability_zones : startswith(az, "ap-northeast-2")])
    error_message = "Use exactly two Seoul availability zones."
  }
}

variable "experiment_availability_zone" {
  description = "Single AZ used by both autoscaler conditions for experiment workers."
  type        = string
  default     = "ap-northeast-2a"

  validation {
    condition     = contains(var.availability_zones, var.experiment_availability_zone)
    error_message = "The experiment AZ must be one of availability_zones."
  }
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "node_instance_type" {
  type    = string
  default = "m7i-flex.large"
}

variable "experiment_ami_override" {
  description = "Optional exact EKS optimized AMI and release used to reproduce a prior measurement batch."
  type = object({
    image_id        = string
    release_version = string
  })
  default = null

  validation {
    condition = var.experiment_ami_override == null ? true : (
      can(regex("^ami-[0-9a-f]+$", var.experiment_ami_override.image_id)) &&
      length(trimspace(var.experiment_ami_override.release_version)) > 0
    )
    error_message = "An AMI override must include an AMI ID and release version."
  }
}

variable "enable_phase3_spot_interruption" {
  description = "Create isolated Karpenter interruption queues, EventBridge rules, and a tag-scoped FIS template for Phase 3."
  type        = bool
  default     = false
}

variable "phase3_spot_experiment_tag" {
  description = "Unique batch tag used to constrain Phase 3 FIS targets."
  type        = string
  default     = "phase3-spot-20261002-v3"

  validation {
    condition     = can(regex("^phase3-spot-[0-9]{8}-v[0-9]+$", var.phase3_spot_experiment_tag))
    error_message = "Use a unique Phase 3 batch tag such as phase3-spot-20261002-v4."
  }
}
