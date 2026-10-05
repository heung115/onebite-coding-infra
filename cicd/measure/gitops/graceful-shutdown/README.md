# backend 배포 중 요청 종료

k6의 GET 20·느린 GET 5·POST 5 req/s, 180초·replica 1·종료 유예 30초 조건에서 rollout restart를 각각 3회 비교했다. 기존 preStop 없음의 502는 7·13·16건, preStop sleep 10초는 0·0·0건이었다. `raw/`에 회차별 요청 집계를 보관했다.

`run_one.sh`는 old Pod 로그·EndpointSlice·k6 결과를 같은 시간축으로 기록한다. `run_batch.sh`는 같은 조건에서 preStop 한 변수만 바꿔 반복하고 `analyze.py`로 집계한다. 클러스터 입력은 `KUBECONFIG`, `KUBE_CONTEXT`, `LOADGEN_NODE`이며 기본 namespace는 dev-back이다. `k6-script.js`의 BASE_URL·API_HOST를 대상 ingress에 맞추고 /test·/test/slow·POST 경로를 준비한다. 부하 Job은 k6-shutdown-script ConfigMap을 마운트한다.

이는 Spring backend의 배포 종료 비교이며 EKS Spot nginx probe의 preStop 3초와 별도 조건이다. [검증 범위](../../../../EVIDENCE.md#gitops-배포와-성공-판정)
