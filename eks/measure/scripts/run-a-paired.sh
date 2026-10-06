#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RESULT_DIR="${1:-}"
if [[ -z "$RESULT_DIR" ]]; then
  printf 'Usage: %s measure/results/<session-dir>\n' "$0" >&2
  exit 2
fi
if [[ "$RESULT_DIR" != /* ]]; then
  RESULT_DIR="$ROOT/$RESULT_DIR"
fi
case "$RESULT_DIR" in
  "$ROOT"/measure/results/*) ;;
  *) printf 'Results must be under measure/results/.\n' >&2; exit 2 ;;
esac

mkdir -p "$RESULT_DIR"
PAIR_ORDER="$RESULT_DIR/a-pair-order.json"
COMMAND_LOG="$RESULT_DIR/commands.log"
if [[ -e "$PAIR_ORDER" ]] || compgen -G "$RESULT_DIR/a-*.json" >/dev/null; then
  printf 'Refusing to overwrite or mix an existing A run.\n' >&2
  exit 2
fi

python3 - "$PAIR_ORDER" <<'PY'
import json, random, secrets, sys
from datetime import datetime, timezone
from pathlib import Path
seed = secrets.randbits(64)
randomizer = random.Random(seed)
first_modes = ["ca"] * 6 + ["karpenter"] * 6
randomizer.shuffle(first_modes)
remaining_modes = ["ca"] * 4 + ["karpenter"] * 4
randomizer.shuffle(remaining_modes)
first_modes.extend(remaining_modes)
pairs = [{"pair": i, "first": first, "order": [first, "karpenter" if first == "ca" else "ca"]}
         for i, first in enumerate(first_modes, 1)]
Path(sys.argv[1]).write_text(json.dumps({
    "created_at_utc": datetime.now(timezone.utc).isoformat(),
    "seed": seed,
    "method": "first 12 pairs balanced 6/6 and randomized; remaining 8 balanced 4/4 and randomized; total 10/10",
    "pairs": pairs,
}, indent=2) + "\n", encoding="utf-8")
PY

declare -i ca_trials=0
declare -i karpenter_trials=0
failures=0
stop_after_12=0
while IFS=$'\t' read -r pair first second; do
  for condition in "$first" "$second"; do
    case "$condition" in
      ca) ca_trials=$((ca_trials + 1)); trial="$ca_trials" ;;
      karpenter) karpenter_trials=$((karpenter_trials + 1)); trial="$karpenter_trials" ;;
      *) printf 'Unexpected condition in pair order: %s\n' "$condition" >&2; exit 3 ;;
    esac
    output="$RESULT_DIR/a-${condition}-$(printf '%02d' "$trial").json"
    set +e
    "$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" "$ROOT/measure/scripts/select-autoscaler.sh" "$condition"
    select_status=$?
    printf 'pair=%s condition=%s select_exit=%s\n' "$pair" "$condition" "$select_status" | tee -a "$COMMAND_LOG"
    "$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" python3 "$ROOT/measure/scripts/collect-trial.py" \
      --app scale-probe --replicas 2 --expected-workers 2 --pretrial-workers 0 \
      --condition "$condition" --trial "$trial" --pair "$pair" --pair-first "$first" --output "$output"
    trial_status=$?
    set -e
    if [[ "$select_status" -ne 0 || "$trial_status" -ne 0 ]]; then
      failures=$((failures + 1))
    fi
  done
  if [[ "$pair" -eq 12 ]]; then
    "$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" python3 "$ROOT/measure/scripts/evaluate-a-pairs.py" "$RESULT_DIR" --phase interim
    if grep -q '"stop_after_12": true' "$RESULT_DIR/a-interim-decision.json"; then
      stop_after_12=1
      printf 'interim decision: criteria passed; final sample is 12 pairs\n' | tee -a "$COMMAND_LOG"
      break
    fi
    printf 'interim decision: criteria not passed; continue pre-randomized pairs 13–20\n' | tee -a "$COMMAND_LOG"
  fi
done < <(python3 - "$PAIR_ORDER" <<'PY'
import json, sys
for pair in json.load(open(sys.argv[1], encoding="utf-8"))["pairs"]:
    print(f'{pair["pair"]}\t{pair["first"]}\t{pair["order"][1]}')
PY
)

"$ROOT/measure/scripts/record-command.sh" "$COMMAND_LOG" python3 "$ROOT/measure/scripts/evaluate-a-pairs.py" "$RESULT_DIR" --phase final

python3 - "$RESULT_DIR" "$failures" "$stop_after_12" <<'PY'
import json, sys
from datetime import datetime, timezone
from pathlib import Path
directory = Path(sys.argv[1])
trials = []
for path in sorted(directory.glob("a-*.json")):
    if not path.name.startswith(("a-ca-", "a-karpenter-")):
        continue
    data = json.loads(path.read_text(encoding="utf-8"))
    trials.append({"file": path.name, "condition": data.get("condition"), "pair": data.get("pair"),
                   "succeeded": data.get("succeeded", False), "error": data.get("error")})
summary = {"finished_at_utc": datetime.now(timezone.utc).isoformat(),
           "attempted_runs": len(trials), "attempted_pairs": len({item.get("pair") for item in trials}),
           "stopped_after_12_pairs": bool(int(sys.argv[3])),
           "failed_runs": int(sys.argv[2]), "trials": trials}
(directory / "a-paired-execution.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print(json.dumps({"attempted_runs": len(trials), "attempted_pairs": summary["attempted_pairs"],
                  "stopped_after_12_pairs": summary["stopped_after_12_pairs"],
                  "failed_runs": int(sys.argv[2])}, indent=2))
PY

[[ "$failures" -eq 0 ]]
