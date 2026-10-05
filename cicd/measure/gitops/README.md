# rollout restart와 GitOps 배포 비교

`before-deploy/`는 기존 CI의 배포·실패·재빌드 복구, `after-argocd/`는 GitOps 배포·태그 롤백·drift를 관찰한다. `control-rollout-status/`는 CI에 완료 확인을 추가한 대조 조건이다. `rerun-6.5g/`는 같은 자원 조건의 반복 결과이며 기본 설정과 polling/cache/deadline 변경 결과를 분리했다.

스크립트는 `KUBECONFIG`, `BACKEND_SOURCE_DIR`, `GITOPS_SOURCE_DIR`를 입력받는다. 배치에서 서버 부하를 조회할 때는 `HOMESERVER_SSH_ALIAS`를 지정한다. 별도 클러스터나 원격 repo에서 사용할 때는 namespace·Application·repo·branch를 함께 맞춘다.

[검증 결과와 해석](../../../EVIDENCE.md#gitops-배포와-성공-판정)
