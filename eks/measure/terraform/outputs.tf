output "cluster_name" {
  value = module.eks.cluster_name
}

output "region" {
  value = var.region
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "public_subnets" {
  value = module.vpc.public_subnets
}

output "experiment_ami_id" {
  value = local.experiment_ami.image_id
}

output "experiment_availability_zone" {
  value = var.experiment_availability_zone
}

output "phase3_spot_availability_zones" {
  value = var.availability_zones
}

output "node_groups" {
  value = {
    system     = "${var.cluster_name}-system"
    experiment = "${var.cluster_name}-experiment"
  }
}

output "phase3_karpenter_interruption_queue_name" {
  value = try(aws_sqs_queue.karpenter_interruption[0].name, null)
}

output "phase3_karpenter_interruption_queue_url" {
  value = try(aws_sqs_queue.karpenter_interruption[0].url, null)
}

output "phase3_event_audit_queue_url" {
  value = try(aws_sqs_queue.phase3_event_audit[0].url, null)
}

output "phase3_fis_experiment_template_id" {
  value = try(aws_fis_experiment_template.phase3_spot_interruption[0].id, null)
}
