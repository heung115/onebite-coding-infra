# Argo CD·External Secrets 플랫폼

Terraform은 플랫폼 차트를 설치하고 애플리케이션은 GitOps 선언으로 관리한다. `values/argocd.yaml`의 reconciliation 30초·jitter 0·revision cache 30초가 commit 반영 대기를 줄이는 설정이다. backend의 실패 상태 표시에는 chart의 progressDeadlineSeconds를 별도로 사용했다.

`terraform/`의 Helm release는 기존 플랫폼 Terraform root에서 사용할 구성이다. EKS provider와 플랫폼 전체 root는 [EKS 코드](../../eks/aws)에 있다.
