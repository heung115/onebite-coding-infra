#!/usr/bin/env bash
# onebite EKS 동작 확인 (읽기만). KUBECONFIG 는 전용 파일만 쓴다.
set -uo pipefail
KCFG="$HOME/.kube/onebite-eks.kubeconfig"
k() { KUBECONFIG="$KCFG" kubectl "$@"; }
ENVS=(dev-front dev-back prod)
FAIL=0
ok()  { echo "  OK   $*"; }
bad() { echo "  FAIL $*"; FAIL=1; }

echo "[노드]"
k get nodes -o custom-columns=NAME:.metadata.name,STATUS:.status.conditions[-1].type,TYPE:.metadata.labels.node\.kubernetes\.io/instance-type,ZONE:.metadata.labels.topology\.kubernetes\.io/zone --no-headers

echo "[External Secrets]"
while read -r ns name st; do
  [[ "$st" == "True" ]] && ok "$ns/$name" || bad "$ns/$name Ready=$st"
done < <(k get externalsecrets.external-secrets.io -A -o jsonpath='{range .items[*]}{.metadata.namespace} {.metadata.name} {.status.conditions[?(@.type=="Ready")].status}{"\n"}{end}')

echo "[ArgoCD]"
while read -r name sync health; do
  [[ "$sync" == "Synced" && "$health" == "Healthy" ]] && ok "$name $sync/$health" || bad "$name $sync/$health"
done < <(k -n argocd get applications.argoproj.io -o jsonpath='{range .items[*]}{.metadata.name} {.status.sync.status} {.status.health.status}{"\n"}{end}')

echo "[파드]"
for ns in "${ENVS[@]}"; do
  NOTREADY="$(k -n "$ns" get pods --no-headers 2>/dev/null | awk '{split($2,a,"/"); if (a[1]!=a[2] || $3!="Running") print}')"
  TOTAL="$(k -n "$ns" get pods --no-headers 2>/dev/null | wc -l | tr -d ' ')"
  [[ -z "$NOTREADY" && "$TOTAL" -gt 0 ]] && ok "$ns ${TOTAL}개 Running" || { bad "$ns"; echo "$NOTREADY" | sed 's/^/       /'; }
done

echo "[PVC / EBS gp3]"
k get pvc -A --no-headers -o custom-columns=NS:.metadata.namespace,NAME:.metadata.name,STATUS:.status.phase,SC:.spec.storageClassName,SIZE:.status.capacity.storage \
  | while read -r ns n st sc sz; do [[ "$st" == "Bound" && "$sc" == "gp3" ]] && ok "$ns/$n $sz" || bad "$ns/$n $st $sc"; done

echo "[ALB]"
ALB="$(k -n prod get ingress prod-ingress-web -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null)"
if [[ -z "$ALB" ]]; then bad "ALB 주소 없음"; else
  ok "$ALB"
  for pair in dev-front:one-bite-fe.site dev-back:one-bite-be.site prod:one-bite.dev; do
    env="${pair%%:*}"; host="${pair#*:}"
    WEB="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "Host: $host" "http://$ALB/")"
    API="$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H "Host: $host" "http://$ALB/api/test")"
    [[ "$WEB" =~ ^(200|30[0-9])$ ]] && ok "$env web / → $WEB" || bad "$env web / → $WEB"
    [[ "$API" =~ ^(200|401|403)$ ]] && ok "$env api /api/test → $API" || bad "$env api /api/test → $API"
  done
fi

echo
[[ "$FAIL" == 0 ]] && echo "전체 정상" || { echo "확인 필요 항목 있음"; exit 1; }
