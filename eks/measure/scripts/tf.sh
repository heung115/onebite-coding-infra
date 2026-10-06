#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
expected_region="ap-northeast-2"
configured_region="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"

if [[ "$configured_region" != "$expected_region" ]]; then
  printf 'Refusing Terraform: configured region is %s, expected %s.\n' "${configured_region:-unset}" "$expected_region" >&2
  exit 2
fi

account_id="$(aws sts get-caller-identity --query Account --output text)"
if [[ "${account_id: -4}" != "0497" ]]; then
  printf 'Refusing Terraform: caller account does not end in 0497.\n' >&2
  exit 3
fi

public_ip="$(curl --ipv4 --fail --silent --show-error --max-time 8 https://checkip.amazonaws.com | tr -d '[:space:]')"
if ! python3 - "$public_ip" <<'PY'
import ipaddress
import sys

try:
    ipaddress.IPv4Address(sys.argv[1])
except ipaddress.AddressValueError:
    raise SystemExit(1)
PY
then
  printf 'Could not determine a valid public IPv4 address for the EKS API allow-list.\n' >&2
  exit 5
fi
export TF_VAR_admin_cidrs="[\"${public_ip}/32\"]"

if [[ "${1:-}" == "apply" || "${1:-}" == "destroy" ]]; then
  if [[ "${ALLOW_TERRAFORM_MUTATIONS:-}" != "1" ]]; then
    printf 'Terraform %s is gated. Set ALLOW_TERRAFORM_MUTATIONS=1 only after the user approves the reviewed plan.\n' "$1" >&2
    exit 4
  fi
fi

export AWS_REGION="$expected_region"
export TF_VAR_allowed_account_id="$account_id"
exec terraform -chdir="$root/measure/terraform" "$@"
