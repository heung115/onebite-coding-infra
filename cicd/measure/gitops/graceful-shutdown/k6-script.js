// 배포 중 실패 요청 측정. 실패 = 5xx 또는 연결 실패(status 0).
// 4xx 는 앱이 받아서 처리한 응답이라 실패로 세지 않는다.
import http from 'k6/http';
import { Rate } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://ingress-nginx-controller.ingress-nginx.svc.cluster.local';
const HOST = __ENV.API_HOST || 'devback-api.heung.shop';
const DURATION = __ENV.DURATION || '180s';
const failed = new Rate('failed_5xx_or_conn');

export const options = {
  discardResponseBodies: true,
  scenarios: {
    // 빠른 GET: 원본 코드에 있는 /test
    get_fast: { executor: 'constant-arrival-rate', rate: 20, timeUnit: '1s', duration: DURATION, preAllocatedVUs: 10, maxVUs: 50, exec: 'getFast' },
    // 1.5초 처리 지연을 주는 /test/slow 요청
    get_slow: { executor: 'constant-arrival-rate', rate: 5, timeUnit: '1s', duration: DURATION, preAllocatedVUs: 15, maxVUs: 50, exec: 'getSlow' },
    // POST: nginx 가 기본값으로 다른 upstream 에 재시도하지 않는 요청
    post: { executor: 'constant-arrival-rate', rate: 5, timeUnit: '1s', duration: DURATION, preAllocatedVUs: 5, maxVUs: 30, exec: 'postReq' },
  },
  // 엔드포인트별 집계를 요약에 남기기 위한 형식상 threshold (판정에 쓰지 않음)
  thresholds: {
    'failed_5xx_or_conn{ep:get_fast}': ['rate>=0'],
    'failed_5xx_or_conn{ep:get_slow}': ['rate>=0'],
    'failed_5xx_or_conn{ep:post}': ['rate>=0'],
  },
};

function record(ep, res) {
  const bad = res.status === 0 || res.status >= 500;
  failed.add(bad, { ep });
  if (bad) {
    console.log('FAIL ' + JSON.stringify({ t: Date.now(), ep, status: res.status, err: res.error || '' }));
  }
}

function params(ep, extraHeaders) {
  return { headers: Object.assign({ Host: HOST }, extraHeaders || {}), tags: { ep }, timeout: '10s' };
}

export function setup() {
  console.log('START ' + Date.now());
}
export function getFast() { record('get_fast', http.get(BASE + '/api/test', params('get_fast'))); }
export function getSlow() { record('get_slow', http.get(BASE + '/api/test/slow', params('get_slow'))); }
export function postReq() { record('post', http.post(BASE + '/api/test', '{}', params('post', { 'Content-Type': 'application/json' }))); }

export function handleSummary(data) {
  const out = { end: Date.now() };
  for (const ep of ['get_fast', 'get_slow', 'post']) {
    const m = data.metrics['failed_5xx_or_conn{ep:' + ep + '}'];
    out[ep] = m ? { failed: m.values.passes, total: m.values.passes + m.values.fails } : null;
  }
  const d = data.metrics.dropped_iterations;
  out.dropped_iterations = d ? d.values.count : 0;
  return { stdout: 'SUMMARY ' + JSON.stringify(out) + '\n' };
}
