resource "kubernetes_namespace" "env" {
  for_each = toset(var.envs)
  metadata {
    name = each.value
  }
  depends_on = [module.eks] # EKS
}
