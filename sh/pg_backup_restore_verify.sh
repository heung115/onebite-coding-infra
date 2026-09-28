#!/usr/bin/env bash

set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
POSTGRES_CHART="${ROOT_DIR}/charts/postgresql/postgresql-16.4.5.tgz"
VALUES_FILE="${ROOT_DIR}/values/postgresql.yaml"
LOG_ROOT_DEFAULT="${ROOT_DIR}/logs/ops"

ENVIRONMENT=""
RESTORE_NAMESPACE=""
RESTORE_RELEASE=""
TIMEOUT_SECONDS=300
CLEANUP=0
LOG_ROOT="${LOG_ROOT_DEFAULT}"
ARTIFACT_DIR=""

usage() {
    cat <<'EOF'
Usage:
  pg_backup_restore_verify.sh --env <dev-front|dev-back|prod>
                              [--restore-namespace <ns>] [--restore-release <release>]
                              [--timeout-seconds 300] [--cleanup] [--log-root <dir>]

Flow:
  1. Dump PostgreSQL from the running source pod
  2. Restore into a separate namespace via Helm
  3. Verify service/endpoints and non-system table count
  4. Optionally delete the restore namespace

Examples:
  ./sh/pg_backup_restore_verify.sh --env prod
  ./sh/pg_backup_restore_verify.sh --env dev-front --cleanup
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

find_primary_pod() {
    local namespace="$1"
    local release="$2"
    kubectl get pods -n "${namespace}" \
        -l "app.kubernetes.io/instance=${release}" \
        --field-selector=status.phase=Running \
        -o jsonpath='{.items[0].metadata.name}'
}

postgres_shell() {
    local pod="$1"
    local namespace="$2"
    shift 2

    kubectl exec "${pod}" -n "${namespace}" -- sh -lc "$*"
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --env)
                ENVIRONMENT="${2:-}"
                shift 2
                ;;
            --restore-namespace)
                RESTORE_NAMESPACE="${2:-}"
                shift 2
                ;;
            --restore-release)
                RESTORE_RELEASE="${2:-}"
                shift 2
                ;;
            --timeout-seconds)
                TIMEOUT_SECONDS="${2:-}"
                shift 2
                ;;
            --cleanup)
                CLEANUP=1
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

    require_command kubectl
    require_command helm

    SOURCE_NAMESPACE="${ENVIRONMENT}"
    SOURCE_RELEASE="postgresql-${ENVIRONMENT}"
    RESTORE_NAMESPACE="${RESTORE_NAMESPACE:-restore-check-${ENVIRONMENT}}"
    RESTORE_RELEASE="${RESTORE_RELEASE:-postgresql-restore-${ENVIRONMENT}}"

    mkdir -p "${LOG_ROOT}"
    ARTIFACT_DIR="${LOG_ROOT}/pg-restore-$(date '+%Y%m%d-%H%M%S')-${ENVIRONMENT}"
    mkdir -p "${ARTIFACT_DIR}"

    log "Artifacts: ${ARTIFACT_DIR}"
    log "Source release: ${SOURCE_RELEASE}"
    log "Restore namespace: ${RESTORE_NAMESPACE}"

    run_and_capture "${ARTIFACT_DIR}/kubectl-source-pods.log" \
        kubectl get pods,svc -n "${SOURCE_NAMESPACE}" -l "app.kubernetes.io/instance=${SOURCE_RELEASE}" -o wide

    SOURCE_POD="$(find_primary_pod "${SOURCE_NAMESPACE}" "${SOURCE_RELEASE}")"
    [[ -n "${SOURCE_POD}" ]] || fail "Could not find a running source PostgreSQL pod."

    BACKUP_FILE="${ARTIFACT_DIR}/${SOURCE_RELEASE}.sql"
    log "Creating backup dump: ${BACKUP_FILE}"
    postgres_shell "${SOURCE_POD}" "${SOURCE_NAMESPACE}" \
        'export PGPASSWORD="${POSTGRES_PASSWORD:-${POSTGRESQL_PASSWORD:-}}";
         pg_dump -U "${POSTGRES_USER:-${POSTGRESQL_USERNAME:-postgres}}" \
                 -d "${POSTGRES_DATABASE:-${POSTGRESQL_DATABASE:-postgres}}" \
                 --clean --if-exists --no-owner --no-privileges' \
        > "${BACKUP_FILE}"

    run_and_capture "${ARTIFACT_DIR}/restore-namespace.log" \
        kubectl get namespace "${RESTORE_NAMESPACE}" || true

    run_and_capture "${ARTIFACT_DIR}/helm-restore-install.log" \
        helm upgrade --install "${RESTORE_RELEASE}" "${POSTGRES_CHART}" \
            -n "${RESTORE_NAMESPACE}" \
            --create-namespace \
            -f "${VALUES_FILE}" \
            --wait \
            --timeout "${TIMEOUT_SECONDS}s"

    run_and_capture "${ARTIFACT_DIR}/kubectl-restore-ready.log" \
        kubectl wait --for=condition=Ready pod \
            -n "${RESTORE_NAMESPACE}" \
            -l "app.kubernetes.io/instance=${RESTORE_RELEASE}" \
            --timeout="${TIMEOUT_SECONDS}s"

    RESTORE_POD="$(find_primary_pod "${RESTORE_NAMESPACE}" "${RESTORE_RELEASE}")"
    [[ -n "${RESTORE_POD}" ]] || fail "Could not find a running restore PostgreSQL pod."

    log "Restoring dump into ${RESTORE_RELEASE}"
    if ! kubectl exec -i "${RESTORE_POD}" -n "${RESTORE_NAMESPACE}" -- sh -lc \
        'export PGPASSWORD="${POSTGRES_PASSWORD:-${POSTGRESQL_PASSWORD:-}}";
         psql -U "${POSTGRES_USER:-${POSTGRESQL_USERNAME:-postgres}}" \
              -d "${POSTGRES_DATABASE:-${POSTGRESQL_DATABASE:-postgres}}"' \
        < "${BACKUP_FILE}" \
        > "${ARTIFACT_DIR}/psql-restore.log" 2>&1; then
        fail "Restore failed. See ${ARTIFACT_DIR}/psql-restore.log"
    fi

    run_and_capture "${ARTIFACT_DIR}/kubectl-restore-services.log" \
        kubectl get pods,svc,endpoints -n "${RESTORE_NAMESPACE}" -o wide

    run_and_capture "${ARTIFACT_DIR}/pg-isready.log" \
        kubectl exec "${RESTORE_POD}" -n "${RESTORE_NAMESPACE}" -- sh -lc \
            'export PGPASSWORD="${POSTGRES_PASSWORD:-${POSTGRESQL_PASSWORD:-}}";
             pg_isready -h 127.0.0.1 \
                        -U "${POSTGRES_USER:-${POSTGRESQL_USERNAME:-postgres}}" \
                        -d "${POSTGRES_DATABASE:-${POSTGRESQL_DATABASE:-postgres}}"'

    USER_TABLE_COUNT="$(
        kubectl exec "${RESTORE_POD}" -n "${RESTORE_NAMESPACE}" -- sh -lc \
            'export PGPASSWORD="${POSTGRES_PASSWORD:-${POSTGRESQL_PASSWORD:-}}";
             psql -U "${POSTGRES_USER:-${POSTGRESQL_USERNAME:-postgres}}" \
                  -d "${POSTGRES_DATABASE:-${POSTGRESQL_DATABASE:-postgres}}" \
                  -tAc "select count(*) from pg_tables where schemaname not in ('\''pg_catalog'\'','\''information_schema'\'');"' \
            | tr -d '[:space:]'
    )"

    if [[ -z "${USER_TABLE_COUNT}" ]]; then
        fail "Could not read restored table count."
    fi

    kubectl exec "${RESTORE_POD}" -n "${RESTORE_NAMESPACE}" -- sh -lc \
        'export PGPASSWORD="${POSTGRES_PASSWORD:-${POSTGRESQL_PASSWORD:-}}";
         psql -U "${POSTGRES_USER:-${POSTGRESQL_USERNAME:-postgres}}" \
              -d "${POSTGRES_DATABASE:-${POSTGRESQL_DATABASE:-postgres}}" \
              -Atc "select schemaname || '\''.'\'' || tablename from pg_tables where schemaname not in ('\''pg_catalog'\'','\''information_schema'\'') order by 1;"' \
        > "${ARTIFACT_DIR}/restored-tables.txt"

    if [[ "${USER_TABLE_COUNT}" -eq 0 ]]; then
        fail "Restore completed but no user tables were found."
    fi

    {
        printf 'source_release=%s\n' "${SOURCE_RELEASE}"
        printf 'restore_release=%s\n' "${RESTORE_RELEASE}"
        printf 'restore_namespace=%s\n' "${RESTORE_NAMESPACE}"
        printf 'user_table_count=%s\n' "${USER_TABLE_COUNT}"
        printf 'backup_file=%s\n' "${BACKUP_FILE}"
    } > "${ARTIFACT_DIR}/summary.txt"

    if [[ "${CLEANUP}" -eq 1 ]]; then
        run_and_capture "${ARTIFACT_DIR}/cleanup.log" \
            kubectl delete namespace "${RESTORE_NAMESPACE}" --wait=true
    fi

    log "Backup and restore verification completed successfully."
}

main "$@"
