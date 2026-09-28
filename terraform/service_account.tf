resource "kubernetes_service_account" "github_deployer" {
  metadata {
    name      = "github-deployer"
    namespace = "kube-system"
  }
}
