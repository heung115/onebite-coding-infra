# Prune

전용 Application의 ConfigMap을 선언에서 삭제하고 push 반환부터 NotFound까지 관찰했다. `results.tsv`에 검증한 회차를 보관했다. prune true·allowEmpty 조건의 3회 중앙값은 39.279초였다. 다른 배포 리소스를 삭제하는 시간으로 일반화하지 않는다.

`measure_prune_observe.sh`는 결과를 관찰하고 `common.sh`는 cluster·repo 입력을 공유한다. [전체 결과](../../../EVIDENCE.md#gitops-배포와-성공-판정)
