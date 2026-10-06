#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if [[ "${1:-}" != "--execute" ]]; then
  printf 'Usage: %s --execute [measure/results/<session-dir>]\n' "$0" >&2
  printf 'This runner starts benchmark Pods and EC2 workers; use only after the separate Phase 2 approval.\n' >&2
  exit 2
fi
if [[ "${ALLOW_PHASE2_EXECUTION:-}" != "1" ]]; then
  printf 'Set ALLOW_PHASE2_EXECUTION=1 only after the separate Phase 2 approval.\n' >&2
  exit 3
fi
shift
SESSION_DIR="${1:-$ROOT/measure/results/20260930-phase2-instance-selection}"
if [[ "$SESSION_DIR" != /* ]]; then SESSION_DIR="$ROOT/$SESSION_DIR"; fi
case "$SESSION_DIR" in
  "$ROOT"/measure/results/*) ;;
  *) printf 'Results must stay under measure/results/.\n' >&2; exit 4 ;;
esac

ORDER_FILE="$SESSION_DIR/run-order.json"
PROFILE_FILE="$SESSION_DIR/profile-requests.json"
PRICE_FILE="$SESSION_DIR/candidate-inventory-and-pricing.json"
COMMAND_LOG="$SESSION_DIR/commands.log"
if [[ ! -f "$ORDER_FILE" || ! -f "$PROFILE_FILE" || ! -f "$PRICE_FILE" ]]; then
  printf 'The preregistered order, calibrated requests and price inventory are required.\n' >&2
  exit 5
fi
if [[ ! -f "$HOME/.kube/onebite-eks.kubeconfig" ]]; then
  printf 'Dedicated kubeconfig is missing: %s\n' "$HOME/.kube/onebite-eks.kubeconfig" >&2
  exit 6
fi
if compgen -G "$SESSION_DIR/phase2-*.json" >/dev/null; then
  printf 'Refusing to overwrite or mix prior Phase 2 trial outputs.\n' >&2
  exit 7
fi

mkdir -p "$SESSION_DIR"
python3 - "$ORDER_FILE" <<'PY' > "$SESSION_DIR/.phase2-order.tsv"
import json, re, sys
data=json.load(open(sys.argv[1],encoding='utf-8'))
if data.get('batch_id') != '20260930-phase2-v2-design' or len(data.get('runs',[])) != 18:
    raise SystemExit('expected exactly 18 preregistered runs')
counts={}
pair_conditions={}
for item in data['runs']:
    run_id=item.get('run_id','')
    pair=item.get('pair_id','')
    profile=item.get('profile','')
    condition=item.get('condition','')
    match=re.fullmatch(r'(.+)-(\d{2})',pair)
    if not match or match.group(1) != profile:
        raise SystemExit(f'invalid preregistered pair/profile: {item}')
    trial=int(match.group(2))
    if trial not in {1,2,3} or run_id != f'{pair}-{condition}' or condition not in {'fixed','flexible'}:
        raise SystemExit(f'invalid preregistered run identity: {item}')
    key=(item['profile'],item['condition'])
    counts[key]=counts.get(key,0)+1
    pair_conditions.setdefault(pair,[]).append(condition)
if len(counts)!=6 or set(counts.values())!={3}:
    raise SystemExit('expected three runs for each profile and condition')
if len(pair_conditions)!=9 or any(sorted(conditions)!=['fixed','flexible'] for conditions in pair_conditions.values()):
    raise SystemExit('expected nine matched pairs with one Fixed and one Flexible run each')
for index,item in enumerate(data['runs'],1):
    trial=int(item['pair_id'].rsplit('-',1)[1])
    print('\t'.join(map(str,(index,item['pair_id'],item['profile'],item['condition'],trial))))
PY
if [[ "$?" -ne 0 ]]; then
  rm -f "$SESSION_DIR/.phase2-order.tsv"
  exit 8
fi

failures=0
attempted=0
while IFS=$'\t' read -r order_index pair profile condition trial; do
  [[ -n "$order_index" ]] || continue
  attempted=$((attempted + 1))
  output="$SESSION_DIR/phase2-${profile}-${condition}-$(printf '%02d' "$trial").json"
  set +e
  "$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" ALLOW_PHASE2_EXECUTION=1 \
    python3 "$ROOT/measure/scripts/collect-phase2-trial.py" \
      --execute --session-dir "$SESSION_DIR" --condition "$condition" --profile "$profile" \
      --trial "$trial" --pair "$pair" --order-index "$order_index"
  status=$?
  set -e
  if [[ "$status" -ne 0 ]]; then
    failures=$((failures + 1))
    cleanup_ok="$(python3 - "$output" <<'PY'
import json,sys
try: print('true' if json.load(open(sys.argv[1],encoding='utf-8')).get('cleanup_succeeded') else 'false')
except Exception: print('false')
PY
)"
    if [[ "$cleanup_ok" != "true" ]]; then
      printf 'Stopping schedule after run %s because its cleanup/precondition did not complete.\n' "$order_index" | tee -a "$COMMAND_LOG"
      break
    fi
  fi
done < "$SESSION_DIR/.phase2-order.tsv"
rm -f "$SESSION_DIR/.phase2-order.tsv"

"$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" python3 "$ROOT/measure/scripts/summarize-phase2.py" "$SESSION_DIR"
printf 'Phase 2 attempted_runs=%s failed_attempts=%s\n' "$attempted" "$failures" | tee -a "$COMMAND_LOG"
[[ "$failures" -eq 0 ]]
