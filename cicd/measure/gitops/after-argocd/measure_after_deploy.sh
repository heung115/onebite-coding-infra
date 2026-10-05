#!/usr/bin/env bash
# After(ArgoCD): 정상 배포 1회. before 와 같은 트리거(파일 1줄 변경 커밋 → develop push), 같은 측정 구간(push → 새 파드 Ready).
set -euo pipefail
LABEL="${@: -1}"
OUT="$(cd "$(dirname "$0")" && pwd)/raw/$LABEL"; mkdir -p "$OUT"
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
cd "$SRC"; git fetch -q origin; git checkout -q -B develop origin/develop
echo "$LABEL $(date +%s)" > .measure-trigger; git add .measure-trigger
git -c user.name=heung115 -c user.email=heung115@users.noreply.github.com commit -q -m "measure: $LABEL"
SHA=$(git rev-parse HEAD); S7=${SHA:0:7}; T_PUSH=$(now); git push -q origin HEAD:develop
RUN=$(find_run $SHA)
echo "push_sha=$SHA t_push=$T_PUSH run_id=$RUN" | tee "$OUT/meta.txt"
read T_NEWREV T_READY < <(wait_img_ready "sha-$SHA")
echo "t_newrev=$T_NEWREV t_ready=$T_READY app_state=$(app_state)" | tee -a "$OUT/meta.txt"
T_GITOPS=$(gitops_commit_ts $S7)
echo "t_gitops_commit=$T_GITOPS" | tee -a "$OUT/meta.txt"
gh run watch "$RUN" -R $REPO --exit-status >/dev/null 2>&1 || true
gh run view "$RUN" -R $REPO --json status,conclusion,createdAt,updatedAt,jobs > "$OUT/run.json"
gh run view "$RUN" -R $REPO --log > "$OUT/run.log" 2>/dev/null || true
NEWPOD=$(kubectl -n $NS get pods -l app.kubernetes.io/instance=$DEP --sort-by=.metadata.creationTimestamp -o name | tail -1)
kubectl -n $NS get $NEWPOD -o json > "$OUT/newpod.json"; kubectl -n $NS logs $NEWPOD --tail=400 > "$OUT/newpod.log" 2>&1 || true
sed -i '' -E 's/(Using generated security password: )[^ ]+/\1[REDACTED]/' "$OUT/newpod.log"
kubectl -n argocd get application $APP -o json | jq '{sync:.status.sync, health:.status.health, op:.status.operationState|{phase,startedAt,finishedAt,message}}' > "$OUT/app.json"
echo "done $LABEL" | tee -a "$OUT/meta.txt"
