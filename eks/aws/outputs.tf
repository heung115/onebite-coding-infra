output "region" {
  value = var.region
}

output "cluster_name" {
  value = module.eks.cluster_name
}

output "vpc_id" {
  value = module.vpc.vpc_id
}

output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --region ${var.region} --name ${module.eks.cluster_name} --kubeconfig ~/.kube/onebite-eks.kubeconfig --alias onebite-eks"
}

output "secret_names" {
  value = [for s in aws_secretsmanager_secret.this : s.name]
}

output "backend_ecr_url" {
  value = aws_ecr_repository.backend.repository_url
}
