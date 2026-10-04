# 백엔드 이미지 빌드와 CI

Spring Boot 애플리케이션 저장소에서 사용하는 Gradle·Docker·GitHub Actions 설정이다. 이 폴더의 `.github/`, Dockerfile, Gradle archive 설정을 백엔드 저장소 루트에 배치한다. 애플리케이션 소스와 Gradle wrapper는 백엔드 저장소에서 관리한다.

1. runner에서 `gradle/actions/setup-gradle`로 캐시를 복원하고 `bootJar`를 생성한다.
2. layered JAR를 추출하고 파일·디렉터리 mtime을 고정한다.
3. JRE 이미지의 dependency/application 레이어를 분리한다.
4. Buildx에서 SHA 태그 이미지를 직접 push하고 manifest digest를 기록한다.
5. GitOps 저장소의 해당 환경 image tag를 커밋한다.

`docker-build.yaml`은 최종 배포 경로이며, `ci-b2-*`와 `diagnose-*` workflow는 비교 측정과 registry 전송 분석에 사용했다. `DOCKER_PASSWORD`와 `GITOPS_DEPLOY_KEY`는 GitHub Secrets로 주입한다. GitOps 원격 저장소·브랜치·values 경로는 workflow에 명시돼 있다.

측정 결과와 조건은 [증거 지도](../../EVIDENCE.md#이미지와-ci)에 연결했다.
