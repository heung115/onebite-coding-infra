resource "kubernetes_cluster_role_binding" "github_deployer_admin" {
  metadata {
    name = "github-deployer-full-access"
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "ClusterRole"
    name      = "cluster-admin" # 이미 존재하는 기본 관리자 권한
  }

  subject {
    kind      = "ServiceAccount"
    name      = "github-deployer"
    namespace = "kube-system"
  }
  depends_on = [kubernetes_service_account.github_deployer]
}
