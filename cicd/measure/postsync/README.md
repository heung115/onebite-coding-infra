# PostSync smoke 확인

`manifests/`의 Job은 HTTP smoke 결과를 PostSync Hook 결과로 반환한다. `measure_postsync_observe.sh`는 Application health, Hook, operation 결과를 구분해 기록하고 `results.tsv`에 성공·실패 회차를 보관했다.

HTTP 200·404 각각 3회 결과를 비교했다. HTTP 404로 Hook/operation이 실패했어도 Application Healthy는 유지돼 Healthy만으로 smoke 성공을 판정하지 않았다. [표본과 측정 구간](../../../EVIDENCE.md#gitops-배포와-성공-판정)
