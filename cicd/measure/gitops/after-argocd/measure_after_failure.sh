#!/usr/bin/env bash
# After(ArgoCD): 깨진 배포 → 감지 → 복구 1회.
#  - 깨진 배포: before 와 같은 System.exit(1) 커밋을 develop 에 push
#  - 감지: ArgoCD Application health 가 Degraded 가 되는 시각 (워크플로우 결론도 기록)
#  - 복구 방식 $2: app  = 앱 코드 git revert 후 push (CI 재빌드, before 와 같은 방식)
#                  gitops = GitOps 저장소의 태그 변경 커밋만 git revert 후 push (재빌드 없음)
set -euo pipefail
MODE="$1"; LABEL="${@: -1}"
OUT="$(cd "$(dirname "$0")" && pwd)/raw/$LABEL"; mkdir -p "$OUT"
source "$(cd "$(dirname "$0")" && pwd)/common.sh"
APPF=src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java
cd "$SRC"; git fetch -q origin; git checkout -q -B develop origin/develop
test -z "$(git status --porcelain --untracked-files=no)"
python3 - "$APPF" <<'PY'
import sys; p=sys.argv[1]; s=open(p).read()
s=s.replace('SpringApplication.run(SpaghettiApplication.class, args);','SpringApplication.run(SpaghettiApplication.class, args);\n\t\tSystem.exit(1); // measure: broken deploy')
open(p,'w').write(s)
PY
git -c user.name=heung115 -c user.email=heung115@users.noreply.github.com commit -q -am "measure: $LABEL broken deploy (app exits on start)"
BAD=$(git rev-parse HEAD); T_BAD_PUSH=$(now); git push -q origin HEAD:develop
RUN_BAD=$(find_run $BAD)
echo "mode=$MODE bad_sha=$BAD t_bad_push=$T_BAD_PUSH run_bad=$RUN_BAD" | tee "$OUT/meta.txt"
# ArgoCD 가 새 태그로 바꾸고 Degraded 로 판정하는 시각
T_BAD_APPLIED=""; T_DEGRADED=""; T_FIRST_FAIL=""
for i in $(seq 1 600); do
  img=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].image}')
  [ -z "$T_BAD_APPLIED" ] && [[ "$img" == *"sha-$BAD"* ]] && T_BAD_APPLIED=$(now)
  if [ -n "$T_BAD_APPLIED" ]; then
    if [ -z "$T_FIRST_FAIL" ]; then
      np=$(kubectl -n $NS get pods -l app.kubernetes.io/instance=$DEP --sort-by=.metadata.creationTimestamp -o name | tail -1)
      rc=$(kubectl -n $NS get $np -o jsonpath='{.status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0)
      [ "${rc:-0}" -ge 1 ] && T_FIRST_FAIL=$(now)
    fi
    # gitops 모드는 감지 시간을 재지 않는다(앱 코드 revert 3회에서 측정). 첫 크래시 확인 즉시 복구 시작
    if [ "$MODE" = "gitops" ] && [ -n "$T_FIRST_FAIL" ]; then break; fi
    h=$(kubectl -n argocd get application $APP -o jsonpath='{.status.health.status}')
    [ "$h" = "Degraded" ] && { T_DEGRADED=$(now); break; }
  fi
  sleep 2
done
gh run watch "$RUN_BAD" -R $REPO --exit-status >/dev/null 2>&1 || true
T_BAD_RUN_DONE=$(gh run view "$RUN_BAD" -R $REPO --json updatedAt --jq .updatedAt | python3 -c "import sys,datetime;print(datetime.datetime.fromisoformat(sys.stdin.read().strip().replace('Z','+00:00')).timestamp())")
gh run view "$RUN_BAD" -R $REPO --json conclusion,createdAt,updatedAt,jobs > "$OUT/run_bad.json"
kubectl -n argocd get application $APP -o json | jq '{sync:.status.sync, health:.status.health, conditions:.status.conditions}' > "$OUT/app_degraded.json"
kubectl -n $NS get pods -l app.kubernetes.io/instance=$DEP -o wide > "$OUT/pods_after_bad.txt"
READY_OLD=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.status.readyReplicas}')
echo "t_bad_applied=$T_BAD_APPLIED t_first_fail=$T_FIRST_FAIL t_degraded=$T_DEGRADED t_bad_run_done=$T_BAD_RUN_DONE bad_conclusion=$(jq -r .conclusion "$OUT/run_bad.json") old_pod_still_ready=$READY_OLD" | tee -a "$OUT/meta.txt"

# 복구
if [ "$MODE" = "app" ]; then
  git revert --no-edit HEAD >/dev/null
  GOOD=$(git rev-parse HEAD); T_FIX_PUSH=$(now); git push -q origin HEAD:develop
  RUN_FIX=$(find_run $GOOD); WANT="sha-$GOOD"
  echo "fix_sha=$GOOD t_fix_push=$T_FIX_PUSH run_fix=$RUN_FIX" | tee -a "$OUT/meta.txt"
else
  # 깨진 배포를 만든 GitOps 커밋(CI 봇, @BAD 7자리)을 되돌린다. 앱 저장소 develop 은 깨진 상태로 둔다.
  cd "$GSRC"; git fetch -q origin; git checkout -q -B main origin/main
  BADC=$(git log --format='%H %s' -n 20 | grep "@${BAD:0:7}" | head -1 | cut -d' ' -f1)
  PREV_TAG=$(git show "$BADC^:envs/dev-back/backend.yaml" | sed -n -E 's/^    tag: "(.*)"/\1/p')
  git -c user.name=heung115 -c user.email=heung115@users.noreply.github.com revert --no-edit "$BADC" >/dev/null
  T_FIX_PUSH=$(now); git push -q origin HEAD:main
  WANT="$PREV_TAG"; RUN_FIX="-"
  echo "gitops_bad_commit=$BADC revert_to=$PREV_TAG t_fix_push=$T_FIX_PUSH" | tee -a "$OUT/meta.txt"
fi
read T_NEWREV T_RECOVERED < <(wait_img_ready "$WANT")
T_HEALTHY=""; for i in $(seq 1 120); do [ "$(app_state)" = "Synced/Healthy" ] && { T_HEALTHY=$(now); break; }; sleep 2; done
echo "t_fix_applied=$T_NEWREV t_recovered=$T_RECOVERED t_app_healthy=$T_HEALTHY" | tee -a "$OUT/meta.txt"
[ "$RUN_FIX" != "-" ] && { gh run watch "$RUN_FIX" -R $REPO --exit-status >/dev/null 2>&1 || true; gh run view "$RUN_FIX" -R $REPO --json conclusion,createdAt,updatedAt,jobs > "$OUT/run_fix.json"; }

# gitops 모드: 앱 저장소 develop 을 정상 코드로 되돌려 다음 측정 준비 (측정 구간 밖)
if [ "$MODE" = "gitops" ]; then
  # [skip ci]: 측정 구간 밖. 재빌드/재배포 없이 앱 저장소 코드만 정상으로 되돌린다(클러스터는 이미 PREV_TAG 로 복구됨)
  cd "$SRC"; git revert --no-edit "$BAD" >/dev/null; git commit -q --amend -m "Revert broken deploy [skip ci] ($LABEL reset)"; RESET=$(git rev-parse HEAD); git push -q origin HEAD:develop
  echo "post_reset_sha=$RESET (outside measurement)" | tee -a "$OUT/meta.txt"
fi
echo "done $LABEL" | tee -a "$OUT/meta.txt"
