# 한입코딩 인프라

Terraform·Helm 기반 서비스 구성과 CI/CD·GitOps·EKS 구현 코드를 관리한다.

- [CI/CD·EKS 코드와 검증 결과](EVIDENCE.md): 사례별 구현 파일, 측정 수치와 조건
- [백엔드 빌드·CI](cicd/backend): reproducible JAR·JRE·직접 Buildx push
- [GitOps 배포 선언](gitops): 환경별 Application·ExternalSecret·SHA image
- [EKS·Karpenter](eks): 플랫폼·확장·용량·Spot 종료 대응


> 이 저장소는 원래 프라이빗으로 운영하던 인프라 코드를 포트폴리오 공개용으로 퍼블릭 전환한 것입니다. 커밋 히스토리(날짜, 메시지)는 그대로 보존했고, 코드에 평문으로 있던 API 키·비밀번호 등 시크릿만 제거했습니다.

## 로컬 실행

Docker Desktop이 켜져 있고 `one-bite-ai`, `one-bite-fe`, `spaghetti-be`, `spaghetti-infra`가 형제 폴더로 있는 상태에서 프로젝트 최상위 폴더에서 실행합니다.

```sh
docker compose -f spaghetti-infra/compose.local.yaml up --build -d
```

실행 주소:

- 웹: <http://localhost:3000/onboarding>
- API 문서: <http://localhost:8080/swagger-ui/index.html>

Compose가 PostgreSQL과 Redis 준비를 확인한 뒤 백엔드를 시작합니다. 데이터베이스는 Docker 볼륨에 보존됩니다. 기본 포트는 로컬 컴퓨터에서만 접근할 수 있습니다.

기본 설정은 외부 자격 증명 없이 실행하도록 되어 있어 Google 로그인과 Gemini AI 호출은 동작하지 않습니다. 실제 AI 연동이 필요하면 `ONEBITE_GEMINI_API_KEY`와 `ONEBITE_GEMINI_MODEL`을 셸 환경에 명시적으로 설정한 다음 스택을 다시 올리세요. `.env` 파일이나 키를 저장소에 추가하지 마세요.

종료:

```sh
docker compose -f spaghetti-infra/compose.local.yaml down
```

로컬 데이터까지 초기화하려면 `down -v`를 사용합니다. 이 명령은 이 Compose 프로젝트의 데이터베이스 볼륨을 삭제합니다.

이 구성은 로컬 컨테이너 실행용이며 Terraform을 적용하거나 클라우드 리소스를 변경하지 않습니다.
