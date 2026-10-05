#!/usr/bin/env bash
# 1회 측정: 부하를 거는 중에 원본 CI 와 같은 'kubectl rollout restart' 로 배포하고 실패 요청을 센다.
# 사용: ./run_one.sh <label>
set -euo pipefail
cd "$(dirname "$0")"
: "${KUBECONFIG:?Set KUBECONFIG}"
: "${KUBE_CONTEXT:?Set KUBE_CONTEXT}"
K=(kubectl --kubeconfig "$KUBECONFIG" --context "$KUBE_CONTEXT")
NS="${NAMESPACE:-dev-back}"; DEP=backend-dev-back; LBL="$1"; SEL=app.kubernetes.io/instance=$DEP
OUT="raw/$LBL"; mkdir -p "$OUT"
now(){ python3 -c 'import time;print(int(time.time()*1000))'; }
iso(){ python3 -c 'import datetime;print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))'; }

# 0. 정상 상태 확인: 파드 1개 Ready, 종료 중인 파드 없음
for i in $(seq 1 150); do
  n=$("${K[@]}" -n $NS get pods -l $SEL -o json | jq '.items | length')
  r=$("${K[@]}" -n $NS get deploy $DEP -o jsonpath='{.status.readyReplicas}')
  [ "$n" = "1" ] && [ "$r" = "1" ] && break
  sleep 2
done
[ "$n" = "1" ] && [ "$r" = "1" ] || { echo "not steady" >&2; exit 1; }
"${K[@]}" -n $NS get deploy $DEP -o json > "$OUT/deploy-before.json"
OLD=$("${K[@]}" -n $NS get pods -l $SEL -o jsonpath='{.items[0].metadata.name}')
PRESTOP=$(jq -c '.spec.template.spec.containers[0].lifecycle.preStop // "none"' "$OUT/deploy-before.json")
GRACE=$(jq -r '.spec.template.spec.terminationGracePeriodSeconds' "$OUT/deploy-before.json")
{ echo "label=$LBL"; echo "old_pod=$OLD"; echo "prestop=$PRESTOP"; echo "grace=$GRACE"; } > "$OUT/meta.txt"
# 옛 파드 로그는 파드가 사라지면 못 읽으므로 처음부터 따라가며 저장 (Spring 종료 로그 시각 확인용)
"${K[@]}" -n $NS logs -f --timestamps "$OLD" > "$OUT/old-pod.log" 2>&1 &
LOGPID=$!

# 1. k6 Job 시작 (k6 이미지가 캐시된 worker 노드에 고정)
T_LOG=$(iso)
cat <<EOF | "${K[@]}" apply -f - > /dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: k6-$LBL
  namespace: $NS
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      nodeSelector:
        kubernetes.io/hostname: ${LOADGEN_NODE:?Set LOADGEN_NODE to the node with the cached k6 image}
      containers:
        - name: k6
          image: grafana/k6:latest
          imagePullPolicy: IfNotPresent
          args: ["run", "--quiet", "/scripts/script.js"]
          env:
            - name: DURATION
              value: "180s"
          volumeMounts:
            - name: s
              mountPath: /scripts
      volumes:
        - name: s
          configMap:
            name: k6-shutdown-script
EOF
for i in $(seq 1 60); do
  "${K[@]}" -n $NS logs job/k6-$LBL 2>/dev/null | grep -q 'START' && break
  sleep 1
done
T_K6=$(now); echo "t_k6_start=$T_K6" >> "$OUT/meta.txt"
sleep 15

# 2. 배포: 원본 CI 와 같은 명령
T_RESTART=$(now); "${K[@]}" -n $NS rollout restart deploy/$DEP > /dev/null
echo "t_restart=$T_RESTART" >> "$OUT/meta.txt"

# 3. 1초 간격 기록: 파드 상태 + EndpointSlice 조건. 옛 파드가 사라질 때까지
while :; do
  ts=$(now)
  p=$("${K[@]}" -n $NS get pods -l $SEL -o json | jq -c '[.items[] | {n: .metadata.name, del: (.metadata.deletionTimestamp // null), ready: ([.status.conditions[]? | select(.type=="Ready") | .status][0])}]')
  e=$("${K[@]}" -n $NS get endpointslices -l kubernetes.io/service-name=$DEP -o json | jq -c '[.items[].endpoints[]? | {n: .targetRef.name, ready: .conditions.ready, serving: .conditions.serving, terminating: .conditions.terminating}]')
  echo "{\"ts\":$ts,\"pods\":$p,\"eps\":$e}" >> "$OUT/timeline.jsonl"
  "${K[@]}" -n $NS get pod "$OLD" > /dev/null 2>&1 || break
  sleep 1
done
T_GONE=$(now); echo "t_old_gone=$T_GONE" >> "$OUT/meta.txt"
kill $LOGPID 2>/dev/null || true

# 4. k6 종료 대기 후 원본 로그 저장
"${K[@]}" -n $NS wait --for=condition=complete job/k6-$LBL --timeout=300s > /dev/null
"${K[@]}" -n $NS logs job/k6-$LBL > "$OUT/k6.log"
"${K[@]}" -n ingress-nginx logs deploy/ingress-nginx-controller --since-time="$T_LOG" > "$OUT/ingress.log"
grep 'SUMMARY' "$OUT/k6.log" | sed 's/^SUMMARY //' > "$OUT/summary.json"
END=$(jq -r .end "$OUT/summary.json")
# 유효성: 옛 파드가 k6 종료 5초 전까지 사라져야 배포 구간 전체가 부하 안에 들어간 것
if [ $((END - T_GONE)) -lt 5000 ]; then echo "valid=false (old pod outlived load)" >> "$OUT/meta.txt"; else echo "valid=true" >> "$OUT/meta.txt"; fi
echo "done $LBL $(cat "$OUT/summary.json")"
