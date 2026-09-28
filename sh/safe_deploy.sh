#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TF_DIR="${ROOT_DIR}/terraform"
LOG_ROOT_DEFAULT="${ROOT_DIR}/logs/ops"

ENVIRONMENT=""
COMPONENT="all"
TIMEOUT_SECONDS=300
DRY_RUN=0
AUTO_APPROVE=0
SKIP_LINT=0
LOG_ROOT="${LOG_ROOT_DEFAULT}"
ARTIFACT_DIR=""

usage() {
    cat <<'EOF'
Usage:
  safe_deploy.sh --env <dev-front|dev-back|prod> [--component <all|backend|frontend|ai-backend>]
                 [--timeout-seconds 300] [--dry-run] [--auto-approve] [--skip-lint]
                 [--log-root <dir>]

Flow:
  1. terraform validate
  2. helm lint + helm template
  3. terraform plan
  4. terraform apply
  5. kubectl rollout status
  6. failure -> helm rollback to the previously recorded revision + diagnostics

Examples:
  ./sh/safe_deploy.sh --env prod --component backend --auto-approve
  ./sh/safe_deploy.sh --env dev-front --dry-run
EOF
}

log() {
    printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
    log "ERROR: $*"
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

validate_env() {
    case "$1" in
        dev-front|dev-back|prod) ;;
        *) fail "Invalid environment: $1" ;;
    esac
}

validate_component() {
    case "$1" in
        all|backend|frontend|ai-backend) ;;
        *) fail "Invalid component: $1" ;;
    esac
}

selected_components() {
    if [[ "${COMPONENT}" == "all" ]]; then
        printf '%s\n' backend frontend ai-backend
    else
        printf '%s\n' "${COMPONENT}"
    fi
}

release_name() {
    local component="$1"
    local env="$2"
    case "${component}" in
        backend) printf 'backend-%s\n' "${env}" ;;
        frontend) printf 'nextjs-%s\n' "${env}" ;;
        ai-backend) printf 'ai-backend-%s\n' "${env}" ;;
    esac
}

deployment_name() {
    local component="$1"
    local env="$2"
    case "${component}" in
        backend) printf 'backend-%s\n' "${env}" ;;
        frontend) printf 'nextjs-%s-web\n' "${env}" ;;
        ai-backend) printf 'ai-backend-%s\n' "${env}" ;;
    esac
}

terraform_target() {
    local component="$1"
    local env="$2"
    case "${component}" in
        backend) printf 'helm_release.backend["%s"]\n' "${env}" ;;
        frontend) printf 'helm_release.nextjs["%s"]\n' "${env}" ;;
        ai-backend) printf 'helm_release.ai-backend["%s"]\n' "${env}" ;;
    esac
}

chart_dir() {
    local component="$1"
    case "${component}" in
        backend|ai-backend) printf '%s\n' "${ROOT_DIR}/charts/backend" ;;
        frontend) printf '%s\n' "${ROOT_DIR}/charts/nextjs" ;;
    esac
}

lint_value_args() {
    local component="$1"
    case "${component}" in
        backend)
            printf '%s\n' \
                "-f" "${ROOT_DIR}/charts/backend/values.yaml" \
                "-f" "${ROOT_DIR}/values/backend.yaml"
            ;;
        ai-backend)
            printf '%s\n' \
                "-f" "${ROOT_DIR}/charts/backend/values.yaml" \
                "-f" "${ROOT_DIR}/values/ai-backend.yaml"
            ;;
        frontend)
            printf '%s\n' \
                "-f" "${ROOT_DIR}/values/frontend.yaml"
            ;;
    esac
}

run_and_capture() {
    local outfile="$1"
    shift

    {
        printf '$'
        printf ' %q' "$@"
        printf '\n'
        "$@"
    } 2>&1 | tee "${outfile}"
}

capture_previous_revision() {
    local release="$1"
    local namespace="$2"

    if ! helm status "${release}" -n "${namespace}" >/dev/null 2>&1; then
        return 0
    fi

    helm history "${release}" -n "${namespace}" --max 1 2>/dev/null | awk 'NR > 1 { print $1 }' | tail -n 1
}

collect_diagnostics() {
    local component="$1"
    local namespace="$2"
    local deployment="$3"

    run_and_capture "${ARTIFACT_DIR}/kubectl-get-${component}.log" \
        kubectl get pods,svc,ingress -n "${namespace}" -o wide || true
    run_and_capture "${ARTIFACT_DIR}/kubectl-describe-${component}.log" \
        kubectl describe deployment "${deployment}" -n "${namespace}" || true
    run_and_capture "${ARTIFACT_DIR}/kubectl-logs-${component}.log" \
        kubectl logs deployment/"${deployment}" -n "${namespace}" --all-containers --tail=200 || true
}

rollback_component() {
    local component="$1"
    local namespace="$2"
    local release="$3"
    local deployment="$4"
    local previous_revision="$5"

    if [[ -z "${previous_revision}" ]]; then
        log "No previous Helm revision recorded for ${release}; skipping rollback."
        return 0
    fi

    run_and_capture "${ARTIFACT_DIR}/helm-rollback-${component}.log" \
        helm rollback "${release}" "${previous_revision}" -n "${namespace}" --wait --timeout "${TIMEOUT_SECONDS}s"
    run_and_capture "${ARTIFACT_DIR}/kubectl-rollout-after-rollback-${component}.log" \
        kubectl rollout status deployment/"${deployment}" -n "${namespace}" --timeout="${TIMEOUT_SECONDS}s"
}

rollback_selected_components() {
    local component namespace release deployment previous_revision
    while IFS='|' read -r component namespace release deployment previous_revision; do
        rollback_component "${component}" "${namespace}" "${release}" "${deployment}" "${previous_revision}"
    done < "${ARTIFACT_DIR}/release-snapshot.txt"

    cat > "${ARTIFACT_DIR}/rollback-follow-up.txt" <<'EOF'
Rollback restored the previous Helm revision, but Terraform still describes the attempted target state.
Before the next terraform apply, revert the faulty image tag or values change and confirm terraform plan is clean.
EOF
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env)
                ENVIRONMENT="${2:-}"
                shift 2
                ;;
            --component)
                COMPONENT="${2:-}"
                shift 2
                ;;
            --timeout-seconds)
                TIMEOUT_SECONDS="${2:-}"
                shift 2
                ;;
            --dry-run)
                DRY_RUN=1
                shift
                ;;
            --auto-approve)
                AUTO_APPROVE=1
                shift
                ;;
            --skip-lint)
                SKIP_LINT=1
                shift
                ;;
            --log-root)
                LOG_ROOT="${2:-}"
                shift 2
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                fail "Unknown argument: $1"
                ;;
        esac
    done
}

main() {
    parse_args "$@"

    [[ -n "${ENVIRONMENT}" ]] || {
        usage
        fail "--env is required"
    }

    validate_env "${ENVIRONMENT}"
    validate_component "${COMPONENT}"

    require_command terraform
    require_command helm
    require_command kubectl

    mkdir -p "${LOG_ROOT}"
    ARTIFACT_DIR="${LOG_ROOT}/deploy-$(date '+%Y%m%d-%H%M%S')-${ENVIRONMENT}-${COMPONENT}"
    mkdir -p "${ARTIFACT_DIR}"

    log "Artifacts: ${ARTIFACT_DIR}"
    log "Environment: ${ENVIRONMENT}"
    log "Component: ${COMPONENT}"

    run_and_capture "${ARTIFACT_DIR}/terraform-validate.log" \
        terraform -chdir="${TF_DIR}" validate

    if [[ "${SKIP_LINT}" -eq 0 ]]; then
        while IFS= read -r current_component; do
            mapfile -t value_args < <(lint_value_args "${current_component}")
            local_chart_dir="$(chart_dir "${current_component}")"
            local_release="$(release_name "${current_component}" "${ENVIRONMENT}")"

            run_and_capture "${ARTIFACT_DIR}/helm-lint-${current_component}.log" \
                helm lint "${local_chart_dir}" "${value_args[@]}"
            run_and_capture "${ARTIFACT_DIR}/helm-template-${current_component}.log" \
                helm template "${local_release}" "${local_chart_dir}" "${value_args[@]}" > /dev/null
        done < <(selected_components)
    fi

    target_args=()
    while IFS= read -r current_component; do
        target_args+=("-target=$(terraform_target "${current_component}" "${ENVIRONMENT}")")
    done < <(selected_components)

    run_and_capture "${ARTIFACT_DIR}/terraform-plan.log" \
        terraform -chdir="${TF_DIR}" plan "${target_args[@]}"

    if [[ "${DRY_RUN}" -eq 1 ]]; then
        log "Dry run complete."
        exit 0
    fi

    : > "${ARTIFACT_DIR}/release-snapshot.txt"
    while IFS= read -r current_component; do
        namespace="${ENVIRONMENT}"
        release="$(release_name "${current_component}" "${ENVIRONMENT}")"
        deployment="$(deployment_name "${current_component}" "${ENVIRONMENT}")"
        previous_revision="$(capture_previous_revision "${release}" "${namespace}")"
        printf '%s|%s|%s|%s|%s\n' \
            "${current_component}" "${namespace}" "${release}" "${deployment}" "${previous_revision}" \
            >> "${ARTIFACT_DIR}/release-snapshot.txt"
    done < <(selected_components)

    apply_cmd=(terraform -chdir="${TF_DIR}" apply)
    if [[ "${AUTO_APPROVE}" -eq 1 ]]; then
        apply_cmd+=(-auto-approve)
    fi
    apply_cmd+=("${target_args[@]}")

    if ! run_and_capture "${ARTIFACT_DIR}/terraform-apply.log" "${apply_cmd[@]}"; then
        log "Terraform apply failed; collecting diagnostics and rolling back."
        while IFS='|' read -r current_component namespace release deployment previous_revision; do
            collect_diagnostics "${current_component}" "${namespace}" "${deployment}"
        done < "${ARTIFACT_DIR}/release-snapshot.txt"
        rollback_selected_components
        fail "Deployment failed during terraform apply."
    fi

    if ! while IFS='|' read -r current_component namespace release deployment previous_revision; do
        run_and_capture "${ARTIFACT_DIR}/kubectl-rollout-${current_component}.log" \
            kubectl rollout status deployment/"${deployment}" -n "${namespace}" --timeout="${TIMEOUT_SECONDS}s"
    done < "${ARTIFACT_DIR}/release-snapshot.txt"; then
        log "Rollout verification failed; collecting diagnostics and rolling back."
        while IFS='|' read -r current_component namespace release deployment previous_revision; do
            collect_diagnostics "${current_component}" "${namespace}" "${deployment}"
        done < "${ARTIFACT_DIR}/release-snapshot.txt"
        rollback_selected_components
        fail "Deployment failed during rollout verification."
    fi

    while IFS='|' read -r current_component namespace release deployment previous_revision; do
        run_and_capture "${ARTIFACT_DIR}/kubectl-post-deploy-${current_component}.log" \
            kubectl get deployment,svc,ingress -n "${namespace}" -o wide
        run_and_capture "${ARTIFACT_DIR}/helm-history-${current_component}.log" \
            helm history "${release}" -n "${namespace}"
    done < "${ARTIFACT_DIR}/release-snapshot.txt"

    log "Deployment completed successfully."
}

main "$@"
