#!/usr/bin/env bash
# 측정 배치를 세션과 분리해서 실행. 사용: ./run_batch.sh <batch-name> <cmd...>
# 각 줄: "<label> <script> [args]"
B="$1"; shift
LOG="$(cd "$(dirname "$0")" && pwd)/raw/batch-$B.log"
cd "$(dirname "$0")"
while IFS= read -r line; do
  [ -z "$line" ] && continue
  set -- $line
  lbl="$1"; shift
  echo "$(date '+%F %T') start $lbl" >> "$LOG"
  sleep 20
  if "$@" "$lbl" > "raw/$lbl.out" 2>&1; then echo "$(date '+%F %T') ok $lbl" >> "$LOG"; else echo "$(date '+%F %T') FAIL $lbl rc=$?" >> "$LOG"; fi
done < "raw/batch-$B.plan"
echo "$(date '+%F %T') BATCH-DONE" >> "$LOG"
