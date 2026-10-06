#!/usr/bin/env bash
set -uo pipefail

REGION="ap-northeast-2"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)"
if [[ "${ACCOUNT_ID: -4}" != "0497" || "${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}" != "$REGION" ]]; then
  printf 'Refusing cost capture: caller must be account ****0497 with ap-northeast-2 configured.\n' >&2
  exit 2
fi
if [[ "$#" -ne 1 ]]; then
  printf 'Usage: %s OUTPUT_JSON\n' "$0" >&2
  exit 2
fi

OUTPUT="$1"
mkdir -p "$(dirname "$OUTPUT")"
START="$(date -u +%Y-%m-01)"
END="$(python3 - <<'PY'
from datetime import datetime, timedelta, timezone
print((datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat())
PY
)"

FREE_TIER_OUTPUT="$(aws freetier get-free-tier-usage --region us-east-1 --output json 2>&1)"
FREE_TIER_STATUS=$?
PLAN_OUTPUT="$(aws freetier get-account-plan-state --region us-east-1 --output json 2>&1)"
PLAN_STATUS=$?
if [[ "$PLAN_STATUS" -eq 0 ]]; then
  PLAN_OUTPUT="$(jq 'del(.accountId)' <<<"$PLAN_OUTPUT")"
fi
COST_OUTPUT="$(aws ce get-cost-and-usage --region us-east-1 --time-period "Start=$START,End=$END" --granularity DAILY --metrics UnblendedCost --output json 2>&1)"
COST_STATUS=$?

jq -n \
  --arg capturedAtUTC "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg accountSuffix "0497" \
  --arg region "$REGION" \
  --arg start "$START" \
  --arg endExclusive "$END" \
  --arg freeTierOutput "$FREE_TIER_OUTPUT" \
  --argjson freeTierExitCode "$FREE_TIER_STATUS" \
  --arg accountPlanOutput "$PLAN_OUTPUT" \
  --argjson accountPlanExitCode "$PLAN_STATUS" \
  --arg costExplorerOutput "$COST_OUTPUT" \
  --argjson costExplorerExitCode "$COST_STATUS" \
  '{capturedAtUTC:$capturedAtUTC,accountSuffix:$accountSuffix,region:$region,costExplorerPeriod:{start:$start,endExclusive:$endExclusive},freeTierUsage:{exitCode:$freeTierExitCode,rawOutput:$freeTierOutput},accountPlan:{exitCode:$accountPlanExitCode,rawOutput:$accountPlanOutput},costExplorer:{exitCode:$costExplorerExitCode,rawOutput:$costExplorerOutput}}' \
  > "$OUTPUT"

printf 'Cost status saved to %s (Free Tier usage exit %s; account plan exit %s; Cost Explorer exit %s).\n' "$OUTPUT" "$FREE_TIER_STATUS" "$PLAN_STATUS" "$COST_STATUS"
