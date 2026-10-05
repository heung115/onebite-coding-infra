#!/usr/bin/env bash
# 기존 방식(GitHub Actions -> kubectl rollout restart) 배포 1회 측정
# 사용: ./measure_before_deploy.sh <run-label> [commit-message-suffix]
set -euo pipefail
LABEL="$1"; SUFFIX="${2:-}"
REPO=heung115/tmp-onebite-be
SRC="${BACKEND_SOURCE_DIR:?Set BACKEND_SOURCE_DIR to the backend checkout}"
NS=dev-back; DEP=backend-dev-back
OUT="$(cd "$(dirname "$0")" && pwd)/raw/$LABEL"; mkdir -p "$OUT"
: "${KUBECONFIG:?Set KUBECONFIG for the target cluster}"
now(){ python3 -c 'import time;print(f"{time.time():.3f}")'; }

old_rs=$(kubectl -n $NS get rs -l app.kubernetes.io/instance=$DEP -o json | jq -r '[.items[]|select(.spec.replicas>0)][0].metadata.name')
old_rev=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')

cd "$SRC"
git fetch -q origin
git checkout -q -B develop origin/develop
# 빈 커밋(--allow-empty)은 이 저장소에서 워크플로우가 트리거되지 않아서 파일 변경 1줄로 push
echo "$LABEL $(date +%s)" > .measure-trigger
git add .measure-trigger
git -c user.name=heung115 -c user.email=heung115@users.noreply.github.com commit -q -m "measure: $LABEL $SUFFIX"
SHA=$(git rev-parse HEAD)
T_PUSH=$(now)
git push -q origin HEAD:develop
echo "push_sha=$SHA t_push=$T_PUSH old_rev=$old_rev old_rs=$old_rs" | tee "$OUT/meta.txt"

# 1) GitHub run 찾기
RUN=""
for i in $(seq 1 60); do
  RUN=$(gh api "repos/$REPO/actions/runs?head_sha=$SHA" --jq '.workflow_runs[0].id // empty' 2>/dev/null || true)
  [ -n "$RUN" ] && break; sleep 3
done
echo "run_id=$RUN" | tee -a "$OUT/meta.txt"

# 2) 클러스터: 새 revision 생성 -> 새 파드 Ready 시각 (워크플로우 종료와 무관하게 관찰)
T_NEWREV=""; T_READY=""; NEWPOD=""
for i in $(seq 1 480); do
  rev=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}' 2>/dev/null || echo "$old_rev")
  if [ -z "$T_NEWREV" ] && [ "$rev" != "$old_rev" ]; then T_NEWREV=$(now); fi
  if [ -n "$T_NEWREV" ]; then
    st=$(kubectl -n $NS get deploy $DEP -o json 2>/dev/null | jq -r '"\(.status.updatedReplicas // 0) \(.status.readyReplicas // 0) \(.status.replicas // 0) \(.spec.replicas)"')
    set -- $st
    if [ "$1" = "$4" ] && [ "$2" = "$4" ] && [ "$3" = "$4" ]; then T_READY=$(now); break; fi
  fi
  sleep 2
done
NEWPOD=$(kubectl -n $NS get pods -l app.kubernetes.io/instance=$DEP --sort-by=.metadata.creationTimestamp -o name | tail -1)
kubectl -n $NS get $NEWPOD -o json > "$OUT/newpod.json"
kubectl -n $NS logs $NEWPOD --tail=400 > "$OUT/newpod.log" 2>&1 || true
echo "t_newrev=$T_NEWREV t_ready=$T_READY newpod=$NEWPOD" | tee -a "$OUT/meta.txt"

# 3) 워크플로우 종료 대기 + 원본 로그/잡 타이밍 저장
gh run watch "$RUN" -R $REPO --exit-status >/dev/null 2>&1 || true
gh run view "$RUN" -R $REPO --json status,conclusion,createdAt,updatedAt,jobs > "$OUT/run.json"
gh run view "$RUN" -R $REPO --log > "$OUT/run.log" 2>/dev/null || true
echo "done $LABEL" | tee -a "$OUT/meta.txt"
