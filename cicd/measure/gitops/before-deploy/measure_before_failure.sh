#!/usr/bin/env bash
# 기존 방식: 깨진 배포(앱이 시작 직후 종료)를 push 했을 때
#  - 워크플로우 결론(conclusion)
#  - 클러스터에서 새 파드가 실패 상태가 되는 시각
#  - (감지 수단이 없으므로) 사람이 알 수 있는 신호가 CI에 있는지
# 를 기록하고, 원래 코드로 되돌리는 push 로 복구 시간까지 잰다.
# 사용: ./measure_before_failure.sh <label>
set -euo pipefail
LABEL="$1"
REPO=heung115/tmp-onebite-be; SRC="${BACKEND_SOURCE_DIR:?Set BACKEND_SOURCE_DIR to the backend checkout}"
NS=dev-back; DEP=backend-dev-back
OUT="$(cd "$(dirname "$0")" && pwd)/raw/$LABEL"; mkdir -p "$OUT"
: "${KUBECONFIG:?Set KUBECONFIG for the target cluster}"
now(){ python3 -c 'import time;print(f"{time.time():.3f}")'; }
APP=src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java
find_run(){ local s="$1" r=""; for i in $(seq 1 60); do r=$(gh api "repos/$REPO/actions/runs?head_sha=$s" --jq '.workflow_runs[0].id // empty' 2>/dev/null || true); [ -n "$r" ] && { echo "$r"; return; }; sleep 3; done; }

cd "$SRC"
# 원격 develop 최신 상태에서 시작 (측정 스크립트끼리 브랜치가 어긋나지 않게)
git fetch -q origin
git checkout -q -B develop origin/develop
test -z "$(git status --porcelain --untracked-files=no)"
# 1) 깨진 배포: 컴파일은 되지만 시작 직후 System.exit(1)
python3 - "$APP" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read()
s=s.replace('SpringApplication.run(SpaghettiApplication.class, args);','SpringApplication.run(SpaghettiApplication.class, args);\n\t\tSystem.exit(1); // measure: broken deploy')
open(p,'w').write(s)
PY
git -c user.name=heung115 -c user.email=heung115@users.noreply.github.com commit -q -am "measure: $LABEL broken deploy (app exits on start)"
BAD=$(git rev-parse HEAD); T_BAD_PUSH=$(now); git push -q origin HEAD:develop
RUN_BAD=$(find_run $BAD)
echo "bad_sha=$BAD t_bad_push=$T_BAD_PUSH run_bad=$RUN_BAD" | tee "$OUT/meta.txt"
gh run watch "$RUN_BAD" -R $REPO --exit-status >/dev/null 2>&1 || true
T_BAD_RUN_DONE=$(now)
gh run view "$RUN_BAD" -R $REPO --json conclusion,createdAt,updatedAt,jobs > "$OUT/run_bad.json"
gh run view "$RUN_BAD" -R $REPO --log > "$OUT/run_bad.log" 2>/dev/null || true
echo "t_bad_run_done=$T_BAD_RUN_DONE bad_conclusion=$(jq -r .conclusion "$OUT/run_bad.json")" | tee -a "$OUT/meta.txt"

# 2) 클러스터에서 새 파드가 실패(재시작/CrashLoop)하는 첫 시각
T_FIRST_FAIL=""; for i in $(seq 1 240); do
  np=$(kubectl -n $NS get pods -l app.kubernetes.io/instance=$DEP --sort-by=.metadata.creationTimestamp -o name | tail -1)
  rc=$(kubectl -n $NS get $np -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)
  if [ "${rc:-0}" -ge 1 ]; then T_FIRST_FAIL=$(now); break; fi; sleep 2; done
kubectl -n $NS get pods -l app.kubernetes.io/instance=$DEP -o wide > "$OUT/pods_after_bad.txt"
kubectl -n $NS logs $np --previous --tail=50 > "$OUT/badpod_prev.log" 2>&1 || true
READY_OLD=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.status.readyReplicas}')
echo "t_first_fail=$T_FIRST_FAIL badpod=$np old_pod_still_ready=$READY_OLD" | tee -a "$OUT/meta.txt"

# 3) 복구: 기존 방식에서 할 수 있는 유일한 방법 = 코드 revert 후 다시 push (CI 재빌드)
git revert --no-edit HEAD >/dev/null
GOOD=$(git rev-parse HEAD); T_FIX_PUSH=$(now); git push -q origin HEAD:develop
RUN_FIX=$(find_run $GOOD)
echo "fix_sha=$GOOD t_fix_push=$T_FIX_PUSH run_fix=$RUN_FIX" | tee -a "$OUT/meta.txt"
T_RECOVERED=""; for i in $(seq 1 480); do
  st=$(kubectl -n $NS get deploy $DEP -o json | jq -r '"\(.status.updatedReplicas // 0) \(.status.readyReplicas // 0) \(.status.replicas // 0) \(.status.observedGeneration) \(.metadata.generation)"')
  set -- $st
  np=$(kubectl -n $NS get pods -l app.kubernetes.io/instance=$DEP --sort-by=.metadata.creationTimestamp -o name | tail -1)
  started=$(kubectl -n $NS get $np -o jsonpath='{.metadata.creationTimestamp}')
  if [ "$1" = 1 ] && [ "$2" = 1 ] && [ "$3" = 1 ] && [ "$4" = "$5" ] && \
     python3 -c "import sys,datetime;t=datetime.datetime.fromisoformat('$started'.replace('Z','+00:00')).timestamp();sys.exit(0 if t>$T_FIX_PUSH else 1)"; then T_RECOVERED=$(now); break; fi
  sleep 2; done
gh run watch "$RUN_FIX" -R $REPO --exit-status >/dev/null 2>&1 || true
gh run view "$RUN_FIX" -R $REPO --json conclusion,createdAt,updatedAt,jobs > "$OUT/run_fix.json"
echo "t_recovered=$T_RECOVERED" | tee -a "$OUT/meta.txt"
echo "done $LABEL" | tee -a "$OUT/meta.txt"
