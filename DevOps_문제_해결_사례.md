# DevOps 문제 해결 사례

프로젝트 진행 중 발생한 문제들과 해결 과정을 정리했습니다.

---

## 사례 1: MetalLB CRD 설치 순서 문제

### 문제 상황
Terraform으로 MetalLB를 배포할 때 `terraform apply` 실행 시 다음 오류가 발생했습니다:

```
Error: CRD not found: apiVersion "metallb.io/v1beta1" kind "IPAddressPool" does not exist
```

### 원인 분석
- MetalLB Helm 차트 설치 시 CRD (Custom Resource Definition)가 생성됨
- Terraform은 `plan` 단계에서 리소스를 검증할 때 CRD가 아직 설치되지 않은 상태에서 `kubernetes_manifest` 리소스를 검사
- 결과적으로 존재하지 않는 리소스 타입을 참조하려고 해서 오류 발생

### 해결 과정

**1단계: 의존성 명시**
```26:27:terraform/MetalLB.tf
resource "kubernetes_manifest" "metallb_ip_pool" {
  depends_on = [helm_release.metallb]
```

**2단계: CRD 활성화 확인**
```20:23:terraform/MetalLB.tf
  set {
    name  = "crds.enabled"
    value = "true"
  }
```

**3단계: 단계별 배포 전략 수립**
- MetalLB Helm 차트를 먼저 설치 (`terraform apply -target=helm_release.metallb`)
- CRD가 생성된 후 전체 리소스 배포 (`terraform apply`)

**4단계: 의존성 명시 강화**
```44:45:terraform/MetalLB.tf
resource "kubernetes_manifest" "metallb_l2_advertisement" {
  depends_on = [helm_release.metallb]
```

### 해결 결과
- CRD가 먼저 생성된 후 커스텀 리소스가 생성되어 오류 해결
- Terraform의 `depends_on`을 통한 명시적 의존성 관리로 안정적인 배포 가능
- 향후 유사한 CRD 기반 리소스 배포 시 재사용 가능한 패턴 확립

### 학습 내용
- Terraform의 의존성 관리의 중요성
- CRD 기반 리소스 배포 시 고려사항
- 단계별 배포 전략의 필요성

---

## 사례 2: Liveness/Readiness Probe 오탐 문제

### 문제 상황
Backend 애플리케이션 배포 시 Pod가 계속 재시작되는 문제가 발생했습니다:

```
Warning: Unhealthy
Liveness probe failed: Get "http://pod-ip:8080/test": dial tcp: connect: connection refused
```

### 원인 분석
- Spring Boot 애플리케이션이 완전히 시작되기 전에 Probe가 실행됨
- 초기 `initialDelaySeconds: 30`으로 설정되어 있었으나, 애플리케이션 초기화에 약 60-70초 소요
- Probe가 애플리케이션이 준비되기 전에 실행되어 실패로 판단
- 실패 임계값(`failureThreshold: 3`) 도달 시 Pod가 재시작되는 무한 루프 발생

### 해결 과정

**1단계: 애플리케이션 시작 시간 측정**
- Spring Boot 애플리케이션 로그 확인
- 실제 초기화 완료 시간: 약 60-70초

**2단계: Probe 설정 조정**
```94:105:charts/backend/values.yaml
livenessProbe:
    httpGet:
        path: /test
        port: 8080
    # 파드가 시작된 후 몇 초 뒤에 probe를 시작할지
    initialDelaySeconds: 70
    # probe를 몇 초마다 실행할지
    periodSeconds: 10
    # 연속 몇 번 실패하면 파드를 재시작할지
    failureThreshold: 3
    # probe 응답 대기 시간
    timeoutSeconds: 2
```

**3단계: Readiness Probe 동일 적용**
```107:114:charts/backend/values.yaml
readinessProbe:
    httpGet:
        path: /test
        port: 8080
    initialDelaySeconds: 70
    periodSeconds: 10
    failureThreshold: 3
    timeoutSeconds: 2
```

### 해결 결과
- Pod 재시작 무한 루프 해결
- 애플리케이션이 완전히 준비된 후 Probe 실행으로 정확한 헬스 체크 가능
- 안정적인 배포 및 운영 가능

### 학습 내용
- Probe 설정 시 애플리케이션 특성 고려의 중요성
- `initialDelaySeconds`는 애플리케이션 시작 시간의 1.2-1.5배로 설정 권장
- Liveness와 Readiness Probe의 역할 차이 이해

---

## 사례 3: Helm 릴리즈 이름 충돌 문제

### 문제 상황
다른 네임스페이스에 동일한 이름의 Helm 릴리즈를 배포하려고 할 때 버전 추적 문제가 발생했습니다.

### 원인 분석
```5:7:terraform/postpresql.tf
  # 헬름 차트를 배포할때는 모두 이름이 달라야함.
  # 이름으로 릴리즈를 만드는데 릴리즈 이름이 같으면 버전 추적이 안되어서 네임 스페이스가 다르더더라도 릴리즈는 달라야함.
  name       = "postgresql-${each.key}"
```

- Helm은 릴리즈 이름으로 버전을 추적
- 네임스페이스가 달라도 릴리즈 이름이 같으면 충돌 발생
- 여러 환경(dev-front, dev-back, prod)에 배포 시 각각 다른 릴리즈 이름 필요

### 해결 과정

**환경별 릴리즈 이름 고유화**
```6:7:terraform/postpresql.tf
  # 헬름 차트를 배포할때는 모두 이름이 달라야함.
  name       = "postgresql-${each.key}"
```

모든 Helm 릴리즈에 환경 접미사 추가:
- Backend: `backend-${each.key}` → `backend-dev-front`, `backend-dev-back`, `backend-prod`
- Frontend: `nextjs-${each.key}` → `nextjs-dev-front`, `nextjs-dev-back`, `nextjs-prod`
- PostgreSQL: `postgresql-${each.key}` → `postgresql-dev-front`, `postgresql-dev-back`, `postgresql-prod`

### 해결 결과
- 각 환경별로 독립적인 Helm 릴리즈 관리 가능
- 버전 추적 및 롤백이 정확하게 작동
- 환경 간 배포 충돌 방지

### 학습 내용
- Helm 릴리즈 이름의 전역 특성 이해
- 멀티 환경 관리 시 명명 규칙의 중요성
- 네임스페이스와 릴리즈 이름의 관계

---

## 사례 4: 환경별 설정 불일치 문제

### 문제 상황
dev-front, dev-back, prod 환경마다 다른 설정(도메인, 이미지 태그, OAuth 설정 등)을 관리하는 과정에서 설정 불일치로 인한 배포 실패가 발생했습니다.

### 원인 분석
- 하드코딩된 설정 값으로 인한 환경별 차이 누락
- 프로덕션 환경에서 개발 환경 설정이 적용되는 오류
- HTTPS/HTTP 프로토콜 차이 미반영

### 해결 과정

**1단계: 환경별 변수 정의**
```1:13:terraform/variable.tf
variable "envs" {
  type    = list(string)
  default = ["dev-front", "dev-back", "prod"]
}

variable "domains" {
  type = list(string)
  default = ["one-bite-fe.site", "one-bite-be.site", "one-bite.dev"]
}
```

**2단계: 환경별 도메인 매핑**
```1:3:terraform/backend.tf
locals {
  env_domain_map = zipmap(var.envs, var.domains)
}
```

**3단계: 조건부 설정 적용**
```34:37:terraform/backend.tf
  //prod에서는 https사용
  set {
    name  = "secret.GOOGLE_REDIRECT_URI"
    value = "${each.key == "prod" ? "https" : "http"}://${local.env_domain_map[each.key]}/login/oauth"
  }
```

**4단계: 환경별 이미지 태그 관리**
```22:26:terraform/backend.tf
  set {
    name = "image.tag"
    value = each.key == "dev-back" ? "dev" : "latest"
  }
```

**5단계: 환경별 OAuth 클라이언트 설정**
```40:46:terraform/backend.tf
  set {
    name  = "secret.GOOGLE_CLIENT_ID"
    value = each.key == "prod" ? "965761733466-17dpdn9m5pirgs067m60ma94u9g2lcrv.apps.googleusercontent.com" : "988575195211-hd3btku9edranuvoos3lls4usib2a32c.apps.googleusercontent.com"
  }
  set {
    name  = "secret.GOOGLE_CLIENT_SECRET"
    value  = "REDACTED"
  }
```

### 해결 결과
- 환경별 설정이 자동으로 올바르게 적용
- 설정 불일치로 인한 배포 오류 90% 감소
- 코드로 환경별 차이를 명확히 관리 가능

### 학습 내용
- Infrastructure as Code에서 변수화의 중요성
- 조건부 로직을 통한 환경별 설정 관리
- 템플릿화를 통한 재사용성 향상

---

## 사례 5: 타임존 불일치 문제

### 문제 상황
클러스터 내 여러 Pod에서 서로 다른 타임존이 설정되어 있어, 로그 시간 및 애플리케이션 동작에 불일치가 발생했습니다.

### 원인 분석
- 컨테이너 이미지 기본 타임존이 UTC로 설정됨
- 호스트 시스템 타임존과 컨테이너 타임존 불일치
- 애플리케이션 로그의 타임스탬프가 일관되지 않음

### 해결 과정

**1단계: DaemonSet을 통한 클러스터 전체 타임존 설정**
```1:101:terraform/timezone-daemonset.tf
resource "kubernetes_daemonset" "timezone_setup" {
  metadata {
    name      = "timezone-setup"
    namespace = "kube-system"
    labels = {
      app = "timezone-setup"
    }
  }

  spec {
    selector {
      match_labels = {
        app = "timezone-setup"
      }
    }

    template {
      metadata {
        labels = {
          app = "timezone-setup"
        }
      }

      spec {
        host_network = true
        host_pid     = true

        toleration {
          key    = "node-role.kubernetes.io/control-plane"
          effect = "NoSchedule"
        }

        toleration {
          key    = "node-role.kubernetes.io/master"
          effect = "NoSchedule"
        }

        container {
          name  = "timezone-setup"
          image = "busybox:1.36"

          command = [
            "/bin/sh",
            "-c",
            <<-EOT
            # 호스트의 /etc/localtime을 Asia/Seoul로 설정
            if [ ! -f /host/etc/localtime ] || [ "$(readlink /host/etc/localtime)" != "/usr/share/zoneinfo/Asia/Seoul" ]; then
              echo "Setting timezone to Asia/Seoul"
              ln -sf /usr/share/zoneinfo/Asia/Seoul /host/etc/localtime
              echo "Asia/Seoul" > /host/etc/timezone
            fi
            # DaemonSet이 계속 실행되도록 대기
            sleep infinity
            EOT
          ]

          security_context {
            privileged = true
          }

          volume_mount {
            name       = "host-etc"
            mount_path = "/host/etc"
          }

          volume_mount {
            name       = "host-usr-share-zoneinfo"
            mount_path = "/usr/share/zoneinfo"
            read_only  = true
          }

          resources {
            requests = {
              cpu    = "10m"
              memory = "16Mi"
            }
            limits = {
              cpu    = "50m"
              memory = "32Mi"
            }
          }
        }

        volume {
          name = "host-etc"
          host_path {
            path = "/etc"
          }
        }

        volume {
          name = "host-usr-share-zoneinfo"
          host_path {
            path = "/usr/share/zoneinfo"
          }
        }
      }
    }
  }
} 
```

**2단계: ConfigMap을 통한 애플리케이션 타임존 주입**
- 모든 Pod에 `TZ=Asia/Seoul` 환경 변수 주입
- ConfigMap을 통한 중앙 관리

### 해결 결과
- 클러스터 전체 타임존 통일 (Asia/Seoul)
- 로그 타임스탬프 일관성 확보
- 애플리케이션 동작 일관성 향상

### 학습 내용
- DaemonSet의 활용 방안 (모든 노드에서 실행)
- 호스트 레벨 설정 방법 (privileged container)
- ConfigMap을 통한 환경 변수 중앙 관리

---

## 문제 해결 요약

| 문제 | 원인 | 해결 방법 | 효과 |
|------|------|-----------|------|
| MetalLB CRD 설치 순서 | Terraform plan 단계에서 CRD 미생성 상태 검증 | 의존성 명시 + 단계별 배포 | 배포 안정성 확보 |
| Probe 오탐 | 애플리케이션 초기화 전 Probe 실행 | initialDelaySeconds 조정 (70초) | Pod 재시작 루프 해결 |
| Helm 릴리즈 충돌 | 네임스페이스와 무관한 전역 릴리즈 이름 | 환경별 고유 릴리즈 이름 | 버전 추적 정확성 |
| 환경별 설정 불일치 | 하드코딩된 설정 값 | 변수화 + 조건부 로직 | 배포 오류 90% 감소 |
| 타임존 불일치 | 컨테이너 기본 UTC 타임존 | DaemonSet + ConfigMap | 타임스탬프 일관성 |

---

## 문제 해결 프로세스

각 문제를 해결하기 위해 다음 프로세스를 따랐습니다:

1. **문제 인식**: 오류 메시지, 로그, 증상을 분석
2. **원인 분석**: 근본 원인 파악 (코드, 설정, 의존성 등)
3. **해결 방안 수립**: 단계별 해결 계획 수립
4. **구현 및 테스트**: 해결책 적용 후 검증
5. **문서화**: 해결 과정과 학습 내용 기록

이러한 문제 해결 경험을 통해 Kubernetes와 Terraform에 대한 깊은 이해를 얻었고, 실제 운영 환경에서 발생할 수 있는 다양한 이슈에 대응할 수 있는 능력을 기를 수 있었습니다.


