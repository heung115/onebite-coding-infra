# Self-Heal

`measure_self_heal.sh`는 선언된 replicas 1을 수동으로 변경한 뒤 복원 시각을 관찰한다. `results.tsv`의 3회에서 중앙값은 2.027초였다. 0.5초 polling의 최초 관측 시각이며 Git에 없는 추가 env까지 복원된다고 확대하지 않는다.

공통 입력은 `KUBECONFIG`, `BACKEND_SOURCE_DIR`, `GITOPS_SOURCE_DIR`다. [전체 결과](../../../EVIDENCE.md#gitops-배포와-성공-판정)
