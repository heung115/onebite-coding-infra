# CI/CD·GitOps·EKS 코드와 검증 결과

한입코딩의 이미지 빌드·전달, GitOps 배포, EKS 확장과 Spot 종료 대응을 구현 코드와 측정 결과로 연결한다. 홈서버 기반 설치·백업·TLS·관측·AI API는 이 문서의 범위에서 제외한다.

## 구현을 읽는 순서

| 사례 | 구현·검증 코드 | 결과 자료 |
| --- | --- | --- |
| 재현 가능한 layered JAR·JRE | [Gradle archive 설정](cicd/backend/build.gradle), [JRE Dockerfile](cicd/backend/dockerfile.ci-runner-gradle), [빌드 workflow](cicd/backend/.github/workflows/docker-build.yaml) | [이미지 전달 집계](cicd/measure/image-delivery/raw/eks-delivery-summary.json) |
| runner Gradle 캐시·직접 Buildx push | [빌드 workflow](cicd/backend/.github/workflows/docker-build.yaml), [반복 측정 스크립트](cicd/backend/.github/scripts/measure-ci-b2-runner-gradle.py), [Registry 경로 진단](cicd/backend/.github/scripts/diagnose-docker-hub-layer-reuse.py) | [같은 job warm 집계](cicd/measure/image-delivery/raw/ci-b2-runner-gradle-20261003/warm/run-37127514976/ci-b2-measure-runner-warm-37127514976/warm-summary.json), [독립 warm 집계](cicd/measure/image-delivery/raw/ci-b2-runner-gradle-independent-20261003/independent-warm-summary.json), [직접 push 5회 집계](cicd/measure/image-delivery/raw/ci-b2-direct-buildx-warm-20261004/aggregate-results.json) |
| 불변 SHA 배포·Argo CD 소유권 | [GitOps 갱신 workflow](cicd/backend/.github/workflows/docker-build.yaml), [Application](gitops/apps), [backend chart](gitops/charts/backend), [환경 선언](gitops/envs), [플랫폼 선언](gitops/platform), [플랫폼 Terraform](cicd/platform/terraform), [플랫폼 values](cicd/platform/values) | [GitOps 집계](cicd/measure/gitops/rerun-6.5g/opt-summary.json) |
| backend 배포 중 502·종료 순서 | [종료 측정·분석 코드](cicd/measure/gitops/graceful-shutdown) | [종료 순서·측정 정의](#gitops-배포와-성공-판정) |
| Self-Heal·Prune·PostSync | [Self-Heal](cicd/measure/self-heal), [Prune](cicd/measure/prune), [PostSync](cicd/measure/postsync) | [동작별 결과·측정 정의](#gitops-배포와-성공-판정) |
| CI→GitOps→EKS 연결 | [CI workflow](cicd/backend/.github/workflows/docker-build.yaml), [EKS GitOps 선언](gitops/eks), [EKS Terraform](eks/aws), [EKS chart](eks/charts), [EKS values](eks/values), [환경별 EKS values](eks/values-eks), [EKS CI/CD 설정](eks/cicd), [관리 스크립트](eks/scripts) | [통합 결과·검증 범위](#gitops-배포와-성공-판정) |
| CA·Karpenter 확장 | [측정 Terraform](eks/measure/terraform), [비교 manifest](eks/measure/manifests), [실행·분석 스크립트](eks/measure/scripts) | [paired 분석 JSON](eks/measure/results/20260930T042807Z-a-paired/a-paired-analysis.json) |
| Fixed·Flexible 용량 선택 | [용량 manifest](eks/measure/manifests/karpenter-phase2.yaml), [집계 스크립트](eks/measure/scripts/summarize-phase2.py) | [용량 집계 JSON](eks/measure/results/20260930-phase2-v2-design/fixed-validation/benchmark/phase2-summary.json) |
| Spot 종료·HTTP 요청 실패 | [Spot manifest](eks/measure/manifests/karpenter-phase3-spot.yaml), [HTTP workload](eks/measure/manifests/spot-recovery-app.yaml), [preStop 3초 설정](eks/measure/results/20261003-phase3-prestop3-repro-n10/config/spot-recovery-app-prestop3.yaml), [FIS 설정](eks/measure/terraform/spot-interruption.tf), [통신 경로](eks/measure/terraform/phase3-v5-network.tf), [측정 스크립트](eks/measure/scripts/spot-interruption-trial.py) | [15회 비교 JSON](eks/measure/results/20261003-phase3-prestop3-repro-n10/analysis/prestop3-repro-n15-comparison.json) |

## 이미지와 CI

소스만 변경해도 dependency 레이어가 달라지는 문제와, digest가 같은 기존 blob을 새 runner에서 다시 보내는 문제를 나눠 분석했다.

- dependency 파일 91개의 내용 hash는 같았지만 mtime이 달랐다. Gradle archive의 파일 시각·순서를 고정하고 추출한 파일과 부모 디렉터리의 mtime도 정규화했다.
- runner에서 Gradle 캐시를 복원해 JAR를 만들고 Docker는 레이어 패키징을 담당하도록 나눴다. runtime에는 JRE를 사용했다.
- Registry HEAD 200과 동일 dependency digest를 확인한 뒤 `docker load`→`docker push` 경로를 Buildx 직접 push로 변경했다. registry의 정확한 협상 원인은 확정하지 않았으며, 실제 전송 여부는 TCP payload로도 확인했다.

| 지표 | 변경 전 → 변경 후 | 표본·측정 정의 |
| --- | --- | --- |
| 소스 변경 신규 압축 blob | 약 62.9MB → 196,091B | manifest·push 로그; 네트워크 전송량과 구분 |
| 전체 압축 이미지 | 277.607MB → 175.149MB | 원본 JDK 이미지와 layered JRE 이미지 |
| EKS warm pull 중앙값 | 8.347초 → 2.858초 | 후보별 warm 3회; cold/warm 포함 총 18회 HTTP 200·재시작 0 |
| warm bootJar 중앙값 | 60.300초 → 5.136초 | 각 5회; 변경 후는 캐시 복원 뒤 같은 job에서 source-only pulse 반복 |
| 독립 warm runner의 Gradle 시작→push 중앙값 | 57.851초 → 39.109초 | load→push / 직접 push 각각 새 runner 5회 |
| 위 구간 최소–최대 | 54.103–63.982초 → 32.751–45.330초 | 서로 다른 source pulse·hosted runner 변동 포함 |
| 명령 구간 outbound TCP payload | 174,256,342B → 중앙값 350,532B | 전자는 경로 진단 1회, 후자는 직접 push 5회; egress443 payload |

39.109초는 독립 runner의 첫 warm 빌드 대표값이다. 같은 job 내 반복 17.791초와 cold 100.202초는 별도 조건이다. 57.851→39.109초는 중앙값 기준 32.4% 낮게 관측됐지만 완전히 동일한 입력의 짝비교는 아니다. 신규 blob byte·TCP payload·전체 job 시간도 서로 다른 지표다. Cold pull과 Spring startup의 우열은 확정하지 않았다.

직접 근거: [독립 warm 집계](cicd/measure/image-delivery/raw/ci-b2-runner-gradle-independent-20261003/independent-warm-summary.json), [직접 Buildx 집계](cicd/measure/image-delivery/raw/ci-b2-direct-buildx-warm-20261004/aggregate-results.json).

## GitOps 배포와 성공 판정

CI는 이미지 생성·push·Git 선언 갱신을 담당하고, Argo CD는 클러스터 안에서 적용한다. SHA 태그로 실행 버전을 식별하고 이전 SHA로 롤백한다. backend 3환경의 Helm 관리 주체를 이관하면서 Pod UID·restartCount·Deployment revision·Secret hash가 유지되는지 확인했다. External Secrets의 Merge 정책으로 기존 Secret을 유지했다.

| 지표 | 결과 | 표본·측정 정의 |
| --- | --- | --- |
| 실패 배포 롤백 중앙값 | 196.5초 → 36.4초 | 각 3회, 롤백 커밋 push→Ready; 기존 정상 Pod 유지 |
| commit→apply 중앙값 | 131.6초 → 32.9초 | 각 3회, polling·revision cache 설정 조정 |
| CI Kubernetes API 접속 설정 | 3개 → 0개 | 해당 workflow의 K8S_SERVER/CA/TOKEN 제거; GitOps 쓰기 deploy key 유지 |
| 깨진 배포 apply→Degraded | 601.5초 → 181.1초 | 실패 상태 표시 대기; 정상 배포 완료 시간과 구분 |
| 정상 배포 전체 push→Ready | 195.8초 → 248.5초 | 전체 배포 시간은 증가 |
| Self-Heal replicas 원복 | 3/3회, 중앙값 2.027초 | scale API 응답→replicas 1 최초 관찰; 0.5초 polling |
| Prune ConfigMap 삭제 | 3/3회, 중앙값 39.279초 | push 반환→NotFound 최초 관찰; 전용 Application의 allowEmpty 조건 |
| PostSync 성공·실패 판정 | HTTP 200·404 각각 3/3회 | Hook·operation 결과 확인; HTTP 404에도 Application Healthy 유지 |

Self-Heal은 선언된 replicas 변경을 되돌렸지만 추가 env까지 모두 제거하지는 않았다. Prune은 전용 Application과 ConfigMap으로 검증했다. PostSync operation 완료 중앙값은 정상 21.928초·실패 32.435초이며, trigger acknowledgement→terminal 상태 관찰값이다. 정상 첫 회차는 source 연결로 시작했으므로 순수 Git polling 비교로 쓰지 않는다.

CI→GitOps→EKS 통합에서는 SHA 선언·실행 image digest·Pod Ready·Service GET `/test` HTTP 200을 확인했다. 표본은 1회이며 반복 성능 값이 아니다. PostgreSQL·Redis·ExternalSecret 기동을 확인했고 OAuth·AI 업무 API·외부 ALB 경로까지의 검증은 포함하지 않는다.

backend 종료 비교에서는 ingress-nginx 경유 30 req/s를 180초 동안 보내고 `rollout restart`를 조건별 3회 실행했다. preStop 없음의 502는 7·13·16건(중앙값 13건), `sleep 10`은 0·0·0건이었다. rollout restart→옛 Pod 제거 중앙값은 77.4초→90.5초로 증가했다. replica 1·grace 30초 조건이며 preStop 10초가 최소값인지는 측정하지 않았다. 이 비교는 아래 Spot HTTP probe의 preStop 3초와 별개다.

## EKS 확장과 용량

| 비교 | 결과 | 통제 조건·표본 |
| --- | --- | --- |
| CA 요청→전체 Pod Ready | 중앙값 63.090초, 50.237–75.326초 | 유효 12회 |
| Karpenter 요청→전체 Pod Ready | 중앙값 34.203초, 33.708–36.090초 | 유효 11회; 약 46% 단축 |
| 완전 유효 paired 비교 | 11쌍 모두 Karpenter가 빠름, 짝별 차이 중앙값 -28.344초 | 동일 AZ/subnet·AL2023 AMI·m7i-flex.large·On-Demand, worker 0→2대 |
| 일반·CPU 중심 requests의 시간당 EC2 용량 요율 | $0.23542 → $0.20160, -14.37% | Fixed/Flexible 각각 3회 |
| 메모리 중심 requests의 시간당 EC2 용량 요율 | $0.23542 → $0.24780, +5.26% | Fixed/Flexible 각각 3회 |

확장 비교는 동일 Pod 2개를 서로 다른 노드 2대에 배치하고 순서를 무작위화했다. amiFamily 누락 1회는 실패 기록을 남기고 제외했다. 용량 비교는 3개 requests profile·2개 조건·각 3회, 총 18회다. Fixed는 m7i-flex.large 2대, Flexible은 일반·CPU에 c7i.xlarge 1대, 메모리에 m7i.xlarge 1대를 사용했다. family·size·packing·노드 수가 함께 달라지는 선택 결과이며 서울 리전 On-Demand EC2 요율 기준이다. 실제 청구액·Spot 할인·EKS control plane·system node·스토리지·네트워크 비용은 이 요율에 포함하지 않는다.

## Spot 종료 중 HTTP 요청

system node→Spot Pod IP:80 timeout을 Security Group 규칙으로 해결한 뒤, FIS Spot interruption과 preStop 시간을 비교했다. 노드 준비 시간과 HTTP 실패를 별도 지표로 관찰했다.

| preStop | 유효 회차 | 요청 | 실패 |
| --- | ---: | ---: | ---: |
| 없음 | 5 | 6,000 | 70 |
| 2초 | 4 | 4,800 | 1 |
| 3초 | 15 | 18,001 | 0 |
| 5초 | 15 | 18,000 | 0 |

직접 근거: [3초 15회 비교](eks/measure/results/20261003-phase3-prestop3-repro-n10/analysis/prestop3-repro-n15-comparison.json), [5초 15회 집계](eks/measure/results/20261003-phase3-prestop5-repro-n10/analysis/phase3-prestop5-n15-summary.json).

nginx HTTP probe 2 replicas·ALB GET `/`, PDB minAvailable 1·종료 유예 30초·10 req/s 조건이다. Spot Warning 기준 [-30초,+90초)의 동일 120초 창으로 분석했다. 3초는 최초 5회와 동일 조건 추가 10회를 합산했으며 대체 Pod Ready·Ready replicas 2개 회복 중앙값은 각각 36초였다. 2초의 준비 gate 실패 1회는 HTTP 비교에서 제외했다. 3초는 검증한 조건 중 실패가 관측되지 않은 가장 짧은 후보이며 Spring 업무 API의 무중단을 뜻하지 않는다.

## 공개 자료의 범위

구현 코드·측정 스크립트·최종 보고서·작은 집계 JSON을 보관한다. 인증 정보, Terraform state/plan, kubeconfig, 실제 Secret 값, packet capture, 대용량 이벤트 스트림과 빌드 산출물은 공개 자료에 포함하지 않는다. 환경 설정은 Secret 참조와 입력 변수로 연결한다.
