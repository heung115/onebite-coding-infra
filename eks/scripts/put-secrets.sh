#!/usr/bin/env bash
# JSON 입력의 비밀값을 stdin으로 Secrets Manager에 전달한다. 값은 출력하지 않는다.
set -euo pipefail
: "${ONEBITE_SECRET_INPUT:?Set ONEBITE_SECRET_INPUT to a private JSON input file}"
REGION="${AWS_REGION:-ap-northeast-2}"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
[[ "$ACCT" == *0497 ]] || { echo "계정 불일치: 중단" >&2; exit 1; }
export REGION
python3 - "$ONEBITE_SECRET_INPUT" <<'PYINPUT'
import json, os, subprocess, sys
with open(sys.argv[1]) as stream:
    values = json.load(stream)
if not isinstance(values, dict) or not values:
    raise SystemExit("Expected a non-empty object mapping secret names to values")
allowed = {"onebite-postgresql", "onebite-redis", "onebite-ai-backend", "onebite-argocd-repo",
           "backend-dev-back", "backend-dev-front", "backend-prod"}
if set(values) - allowed:
    raise SystemExit("Unexpected secret name")
for name, value in values.items():
    payload = value if isinstance(value, str) else json.dumps(value)
    subprocess.run(["aws", "secretsmanager", "put-secret-value", "--region", os.environ["REGION"],
                    "--secret-id", name, "--secret-string", "file:///dev/stdin"],
                   input=payload, text=True, check=True, stdout=subprocess.DEVNULL)
    print(f"{name}: updated")
PYINPUT
