# GitOps 배포 선언

`apps/`는 backend 3환경 Application, `envs/`는 공통·환경별 values와 ExternalSecret이다. Terraform은 Argo CD·ESO 플랫폼을 관리하고 Argo CD는 backend를 관리한다. 동일 Helm release를 두 관리자가 동시에 소유하지 않는다.

`eks/`에는 AWS Secrets Manager 연동과 EKS 배포 선언을 보관했다. `eks/cicd/`는 최적화 CI가 만든 SHA image를 배포하는 선언이다. Application의 multi-source values 참조와 실제 Secret 값 없는 SecretStore·ExternalSecret 구성을 확인할 수 있다.

Application은 선언된 GitOps 원격 저장소와 main/eks 브랜치를 조회한다. 다른 저장소에서 사용할 때는 repoURL·targetRevision·path·values 경로를 함께 맞춘다.

[검증 결과](../EVIDENCE.md#gitops-배포와-성공-판정)
