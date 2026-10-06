locals {
  tags = {
    Project   = "onebite"
    Temporary = "true"
  }

  cluster_discovery_tags = {
    "karpenter.sh/discovery" = var.cluster_name
  }

  lbc_target_eni_security_group_tags = var.enable_phase3_spot_interruption ? {
    "onebite.io/lbc-target-sg" = var.cluster_name
  } : {}

  ca_asg_tags = merge(local.tags, {
    "k8s.io/cluster-autoscaler/enabled"                          = "true"
    "k8s.io/cluster-autoscaler/${var.cluster_name}"              = "owned"
    "k8s.io/cluster-autoscaler/node-template/label/measure-pool" = "experiment"
    "k8s.io/cluster-autoscaler/node-template/taint/measure/only" = "experiment:NoSchedule"
    "measure-pool"                                               = "experiment"
  })

  experiment_subnet_id = module.vpc.public_subnets[index(var.availability_zones, var.experiment_availability_zone)]
  experiment_ami       = var.experiment_ami_override != null ? var.experiment_ami_override : jsondecode(nonsensitive(data.aws_ssm_parameter.experiment_ami.value))
}

data "aws_ssm_parameter" "experiment_ami" {
  name = "/aws/service/eks/optimized-ami/${var.kubernetes_version}/amazon-linux-2023/x86_64/standard/recommended"
}

module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "6.7.3"

  name = var.cluster_name
  cidr = var.vpc_cidr
  azs  = var.availability_zones

  public_subnets = ["10.40.0.0/20", "10.40.16.0/20"]

  enable_dns_hostnames    = true
  enable_dns_support      = true
  enable_nat_gateway      = false
  map_public_ip_on_launch = true

  public_subnet_tags = merge(local.tags, local.cluster_discovery_tags, {
    "kubernetes.io/role/elb" = "1"
  })

  public_subnet_tags_per_az = {
    (var.experiment_availability_zone) = {
      "onebite:measure-experiment" = "true"
    }
  }

  tags = local.tags
}

module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "21.26.0"

  name               = var.cluster_name
  kubernetes_version = var.kubernetes_version
  vpc_id             = module.vpc.vpc_id
  subnet_ids         = module.vpc.public_subnets

  endpoint_public_access       = true
  endpoint_private_access      = true
  endpoint_public_access_cidrs = var.admin_cidrs

  enable_cluster_creator_admin_permissions = true
  authentication_mode                      = "API"
  enable_irsa                              = false

  create_kms_key              = false
  encryption_config           = null
  enabled_log_types           = []
  create_cloudwatch_log_group = false

  security_group_tags      = merge(local.tags, local.cluster_discovery_tags)
  node_security_group_tags = merge(local.tags, local.cluster_discovery_tags, local.lbc_target_eni_security_group_tags)

  addons = {
    vpc-cni = {
      before_compute = true
      addon_version  = "v1.23.2-eksbuild.1"
      configuration_values = jsonencode({
        env = {
          ENABLE_PREFIX_DELEGATION = "false"
          WARM_PREFIX_TARGET       = "1"
          ADDITIONAL_ENI_TAGS      = jsonencode(local.tags)
        }
      })
    }
    eks-pod-identity-agent = {
      before_compute = true
      addon_version  = "v1.4.0-eksbuild.2"
    }
    kube-proxy = { addon_version = "v1.35.3-eksbuild.29" }
    coredns    = { addon_version = "v1.14.6-eksbuild.4" }
    aws-ebs-csi-driver = {
      addon_version = "v1.66.0-eksbuild.1"
      pod_identity_association = [{
        role_arn        = aws_iam_role.ebs_csi.arn
        service_account = "ebs-csi-controller-sa"
      }]
      configuration_values = jsonencode({
        controller = {
          extraVolumeTags = local.tags
        }
      })
    }
  }

  eks_managed_node_groups = {
    system = {
      name                     = "${var.cluster_name}-system"
      use_name_prefix          = false
      iam_role_name            = "${var.cluster_name}-system-node"
      iam_role_use_name_prefix = false
      ami_type                 = "AL2023_x86_64_STANDARD"
      instance_types           = [var.node_instance_type]
      capacity_type            = "ON_DEMAND"
      min_size                 = 1
      max_size                 = 1
      desired_size             = 1
      labels                   = { "measure-pool" = "system" }

      create_launch_template = true
      launch_template_tags   = local.tags
      tag_specifications     = ["instance", "volume", "network-interface"]
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 30
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
      update_config = { max_unavailable = 1 }
      tags          = local.tags
    }

    experiment = {
      name                           = "${var.cluster_name}-experiment"
      use_name_prefix                = false
      iam_role_name                  = "${var.cluster_name}-experiment-node"
      iam_role_use_name_prefix       = false
      ami_type                       = "AL2023_x86_64_STANDARD"
      ami_release_version            = local.experiment_ami.release_version
      use_latest_ami_release_version = false
      instance_types                 = [var.node_instance_type]
      capacity_type                  = "ON_DEMAND"
      subnet_ids                     = [local.experiment_subnet_id]
      min_size                       = 0
      max_size                       = 2
      desired_size                   = 0
      labels                         = { "measure-pool" = "experiment" }
      taints = {
        experiment = {
          key    = "measure/only"
          value  = "experiment"
          effect = "NO_SCHEDULE"
        }
      }

      create_launch_template = true
      launch_template_tags   = local.tags
      tag_specifications     = ["instance", "volume", "network-interface"]
      block_device_mappings = {
        xvda = {
          device_name = "/dev/xvda"
          ebs = {
            volume_size           = 30
            volume_type           = "gp3"
            encrypted             = true
            delete_on_termination = true
          }
        }
      }
      update_config = { max_unavailable = 1 }
      tags          = local.tags
    }
  }

  tags = merge(local.tags, local.cluster_discovery_tags)
}

resource "aws_ec2_tag" "experiment_subnet" {
  resource_id = local.experiment_subnet_id
  key         = "onebite:measure-experiment"
  value       = "true"
}

data "aws_eks_node_group" "system" {
  cluster_name    = module.eks.cluster_name
  node_group_name = "${var.cluster_name}-system"
  depends_on      = [module.eks]
}

data "aws_eks_node_group" "experiment" {
  cluster_name    = module.eks.cluster_name
  node_group_name = "${var.cluster_name}-experiment"
  depends_on      = [module.eks]
}

resource "aws_autoscaling_group_tag" "system_project_tags" {
  for_each = local.tags

  autoscaling_group_name = data.aws_eks_node_group.system.resources[0].autoscaling_groups[0].name

  tag {
    key                 = each.key
    value               = each.value
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_group_tag" "experiment_tags" {
  for_each = local.ca_asg_tags

  autoscaling_group_name = data.aws_eks_node_group.experiment.resources[0].autoscaling_groups[0].name

  tag {
    key                 = each.key
    value               = each.value
    propagate_at_launch = true
  }
}
