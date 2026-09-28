#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_ROOT_DEFAULT="${ROOT_DIR}/logs/ops"

TARGET_ENV="prod"
WARN_DAYS=14
LOG_ROOT="${LOG_ROOT_DEFAULT}"
ARTIFACT_DIR=""

usage() {
    cat <<'EOF'
Usage:
  check_tls_and_ingress.sh [--env <dev-front|dev-back|prod|all>] [--warn-days 14] [--log-root <dir>]

Checks:
  - cert-manager Certificate readiness and expiry threshold (prod)
  - HTTP -> HTTPS redirect (prod)
  - HTTPS endpoint response (prod) / HTTP endpoint response (dev)
  - Ingress path and backend wiring from Kubernetes API

Examples:
  ./sh/check_tls_and_ingress.sh
  ./sh/check_tls_and_ingress.sh --env all --warn-days 21
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
        dev-front|dev-back|prod|all) ;;
        *) fail "Invalid environment: $1" ;;
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

domain_for_env() {
    case "$1" in
        dev-front) printf 'one-bite-fe.site\n' ;;
        dev-back) printf 'one-bite-be.site\n' ;;
        prod) printf 'one-bite.dev\n' ;;
    esac
}

certificate_for_env() {
    case "$1" in
        prod) printf 'one-bite-dev-tls\n' ;;
        *) printf '\n' ;;
    esac
}

days_until_iso8601() {
    local not_after="$1"
    python3 - "$not_after" <<'PY'
from datetime import datetime, timezone
import sys

raw = sys.argv[1]
dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
now = datetime.now(timezone.utc)
delta = dt - now
print(delta.days)
PY
}

http_code() {
    curl -ksS -o /dev/null -w '%{http_code}' "$1"
}

check_env() {
    local env="$1"
    local namespace="$env"
    local domain certificate path_file failures=0

    domain="$(domain_for_env "${env}")"
    certificate="$(certificate_for_env "${env}")"
    path_file="${ARTIFACT_DIR}/${env}-ingress-paths.txt"

    log "Checking ${env} (${domain})"

    run_and_capture "${ARTIFACT_DIR}/${env}-kubectl-ingress.log" \
        kubectl get ingress -n "${namespace}" -o wide

    {
        kubectl get ingress -n "${namespace}" -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{range .spec.rules[*]}{.host}{":"}{range .http.paths[*]}{.path}{" -> "}{.backend.service.name}{":"}{.backend.service.port.number}{";"}{end}{"\n"}{end}{end}'
        printf '\n'
    } > "${path_file}"

    if ! grep -q '/api' "${path_file}"; then
        log "Missing /api ingress path for ${env}"
        failures=$((failures + 1))
    fi

    if ! grep -q '/' "${path_file}"; then
        log "Missing / ingress path for ${env}"
        failures=$((failures + 1))
    fi

    if [[ -n "${certificate}" ]]; then
        local ready_status not_after days_left http_status https_status redirect_location

        ready_status="$(kubectl get certificate "${certificate}" -n "${namespace}" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}')"
        not_after="$(kubectl get certificate "${certificate}" -n "${namespace}" -o jsonpath='{.status.notAfter}')"

        {
            printf 'certificate=%s\n' "${certificate}"
            printf 'ready=%s\n' "${ready_status}"
            printf 'not_after=%s\n' "${not_after}"
        } > "${ARTIFACT_DIR}/${env}-certificate.txt"

        if [[ "${ready_status}" != "True" ]]; then
            log "Certificate ${certificate} is not Ready."
            failures=$((failures + 1))
        fi

        if [[ -z "${not_after}" ]]; then
            log "Certificate ${certificate} is missing notAfter."
            failures=$((failures + 1))
        else
            days_left="$(days_until_iso8601 "${not_after}")"
            printf 'days_left=%s\n' "${days_left}" >> "${ARTIFACT_DIR}/${env}-certificate.txt"
            if (( days_left <= WARN_DAYS )); then
                log "Certificate ${certificate} expires in ${days_left} days."
                failures=$((failures + 1))
            fi
        fi

        http_status="$(curl -sS -o /dev/null -w '%{http_code}' "http://${domain}/")"
        redirect_location="$(curl -sS -o /dev/null -w '%{redirect_url}' "http://${domain}/")"
        https_status="$(http_code "https://${domain}/")"

        {
            printf 'http_status=%s\n' "${http_status}"
            printf 'redirect_location=%s\n' "${redirect_location}"
            printf 'https_status=%s\n' "${https_status}"
        } > "${ARTIFACT_DIR}/${env}-https-check.txt"

        if [[ ! "${http_status}" =~ ^30[1278]$ ]] || [[ "${redirect_location}" != https://* ]]; then
            log "Expected HTTP to HTTPS redirect for ${domain}."
            failures=$((failures + 1))
        fi

        if [[ ! "${https_status}" =~ ^(200|301|302|307|308|401|403|404)$ ]]; then
            log "Unexpected HTTPS status for ${domain}: ${https_status}"
            failures=$((failures + 1))
        fi
    else
        local http_status api_status

        http_status="$(http_code "http://${domain}/")"
        api_status="$(http_code "http://${domain}/api")"

        {
            printf 'http_status=%s\n' "${http_status}"
            printf 'api_status=%s\n' "${api_status}"
        } > "${ARTIFACT_DIR}/${env}-http-check.txt"

        if [[ ! "${http_status}" =~ ^(200|301|302|307|308|401|403|404)$ ]]; then
            log "Unexpected HTTP status for ${domain}: ${http_status}"
            failures=$((failures + 1))
        fi

        if [[ ! "${api_status}" =~ ^(200|301|302|307|308|401|403|404|405)$ ]]; then
            log "Unexpected API status for ${domain}/api: ${api_status}"
            failures=$((failures + 1))
        fi
    fi

    return "${failures}"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env)
                TARGET_ENV="${2:-}"
                shift 2
                ;;
            --warn-days)
                WARN_DAYS="${2:-}"
                shift 2
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

    validate_env "${TARGET_ENV}"
    require_command kubectl
    require_command curl
    require_command python3

    mkdir -p "${LOG_ROOT}"
    ARTIFACT_DIR="${LOG_ROOT}/tls-check-$(date '+%Y%m%d-%H%M%S')-${TARGET_ENV}"
    mkdir -p "${ARTIFACT_DIR}"

    log "Artifacts: ${ARTIFACT_DIR}"

    envs=()
    if [[ "${TARGET_ENV}" == "all" ]]; then
        envs=(dev-front dev-back prod)
    else
        envs=("${TARGET_ENV}")
    fi

    failures=0
    for env in "${envs[@]}"; do
        if ! check_env "${env}"; then
            failures=$((failures + 1))
        fi
    done

    if (( failures > 0 )); then
        fail "TLS/Ingress checks failed for ${failures} environment(s)."
    fi

    log "TLS/Ingress checks passed."
}

main "$@"
