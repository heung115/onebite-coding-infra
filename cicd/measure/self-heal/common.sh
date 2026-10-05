REPO=heung115/tmp-onebite-be; GREPO=heung115/tmp-onebite-gitops
SRC="${BACKEND_SOURCE_DIR:?Set BACKEND_SOURCE_DIR to the backend checkout}"; GSRC="${GITOPS_SOURCE_DIR:?Set GITOPS_SOURCE_DIR to the GitOps checkout}"
NS=dev-back; DEP=backend-dev-back; APP=backend-dev-back
: "${KUBECONFIG:?Set KUBECONFIG for the target cluster}"
now(){ python3 -c 'import time;print(f"{time.time():.3f}")'; }
find_run(){ local s="$1" r=""; for i in $(seq 1 60); do r=$(gh api "repos/$REPO/actions/runs?head_sha=$s" --jq '.workflow_runs[0].id // empty' 2>/dev/null || true); [ -n "$r" ] && { echo "$r"; return; }; sleep 3; done; }
# GitOps 저장소에 CI 봇이 남긴 태그 변경 커밋(해당 앱 SHA 포함) 시각
gitops_commit_ts(){ local s7="$1" c=""; for i in $(seq 1 120); do c=$(gh api "repos/$GREPO/commits?per_page=5" --jq ".[] | select(.commit.message|contains(\"@$s7\")) | .commit.committer.date" 2>/dev/null | head -1 || true); [ -n "$c" ] && { python3 -c "import datetime;print(datetime.datetime.fromisoformat('$c'.replace('Z','+00:00')).timestamp())"; return; }; sleep 2; done; }
wait_img_ready(){ # $1=image tag substring, $2=after ts ; prints t_newrev t_ready
  local tag="$1" T_NEWREV="" T_READY=""
  for i in $(seq 1 600); do
    img=$(kubectl -n $NS get deploy $DEP -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)
    if [ -z "$T_NEWREV" ] && [[ "$img" == *"$tag"* ]]; then T_NEWREV=$(now); fi
    if [ -n "$T_NEWREV" ]; then
      st=$(kubectl -n $NS get deploy $DEP -o json 2>/dev/null | jq -r '"\(.status.updatedReplicas // 0) \(.status.readyReplicas // 0) \(.status.replicas // 0) \(.spec.replicas) \(.status.observedGeneration) \(.metadata.generation)"')
      set -- $st
      if [ "$1" = "$4" ] && [ "$2" = "$4" ] && [ "$3" = "$4" ] && [ "$5" = "$6" ]; then T_READY=$(now); break; fi
    fi
    sleep 2
  done
  echo "$T_NEWREV $T_READY"
}
app_state(){ kubectl -n argocd get application $APP -o jsonpath='{.status.sync.status}/{.status.health.status}' 2>/dev/null; }
