#!/usr/bin/env bash
set -eo pipefail

if [[ "$#" -lt 2 ]]; then
  printf 'Usage: %s LOG_FILE [NAME=VALUE ...] COMMAND [ARG ...]\n' "$0" >&2
  exit 2
fi

LOG_FILE="$1"
shift
mkdir -p "$(dirname "$LOG_FILE")"

assignments=()
while [[ "$#" -gt 0 && "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]]; do
  assignments+=("$1")
  shift
done
if [[ "$#" -eq 0 ]]; then
  printf 'A command is required after environment assignments.\n' >&2
  exit 2
fi

{
  printf '[%s] $' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ "${#assignments[@]}" -gt 0 ]]; then
    for assignment in "${assignments[@]}"; do
      printf ' %q' "$assignment"
    done
  fi
  printf ' %q' "$@"
  printf '\n'
} | tee -a "$LOG_FILE"

for assignment in "${assignments[@]}"; do
  export "$assignment"
done

set +e
"$@" 2>&1 | tee -a "$LOG_FILE"
command_status=${PIPESTATUS[0]}
set -e
printf '[exit_code=%s]\n' "$command_status" | tee -a "$LOG_FILE"
exit "$command_status"
