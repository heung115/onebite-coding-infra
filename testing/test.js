import http from 'k6/http';
import { check, sleep } from 'k6';

// 1) 시나리오: 점진적으로 VU 올렸다 내리기
export let options = {
  scenarios: {
    load_test: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '30s', target: 100 },   // 워밍업
        { duration: '1m',  target: 500 },   // 중간 부하
        { duration: '1m',  target: 1000 },  // 더 높은 부하
        { duration: '1m',  target: 1500 },  // 한계 탐색
        { duration: '30s', target: 0   },   // 쿨다운
      ],
      gracefulRampDown: '30s',
    },
  },
  // 2) Thresholds: 실패 기준 설정
  thresholds: {
    http_req_failed:      ['rate<0.01'],     // 실패율 1% 이내
    http_req_duration:    ['p(95)<1000'],    // p95 1초 이내
    checks:               ['rate>0.99'],      // 체크 성공률 99% 이상
  },
};

const BASE_URL = __ENV.HOST || 'http://one-bite-fe.site';

export default function () {
  const res = http.get(`${BASE_URL}/api/test`);
  check(res, {
    'status is 200': (r) => r.status === 200,
  });
  sleep(1);
}
