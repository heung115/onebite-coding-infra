#!/usr/bin/env python3
"""배포 종료 결과 정리. 사용: ./analyze.py raw/<label> [raw/<label> ...]"""
import json, re, sys, datetime, statistics, pathlib

def ms(iso):
    return int(datetime.datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp() * 1000)

def one(d):
    d = pathlib.Path(d)
    meta = dict(l.split('=', 1) for l in (d / 'meta.txt').read_text().split('\n') if '=' in l)
    old = meta['old_pod']
    summ = json.loads((d / 'summary.json').read_text())
    fails = []
    for m in re.finditer(r'msg="FAIL (.*?)" source=console', (d / 'k6.log').read_text()):
        fails.append(json.loads(m.group(1).replace('\\"', '"')))
    # 1초 간격 관찰 기록
    t_del = t_term = t_ep_gone = None
    for line in (d / 'timeline.jsonl').read_text().splitlines():
        r = json.loads(line)
        op = [p for p in r['pods'] if p['n'] == old]
        oe = [e for e in r['eps'] if e['n'] == old]
        if t_del is None and op and op[0]['del']:
            # deletionTimestamp = 삭제 요청 시각 + grace 초 (강제 종료 예정 시각). 요청 시각으로 되돌린다
            t_del = ms(op[0]['del']) - int(meta['grace']) * 1000
        if t_term is None and oe and oe[0]['terminating']:
            t_term = r['ts']                    # EndpointSlice 에 terminating 으로 보인 첫 관찰
        if t_ep_gone is None and t_del is not None and not oe:
            t_ep_gone = r['ts']                 # EndpointSlice 에서 빠진 첫 관찰
    # 옛 파드의 Spring 종료 로그
    t_shut = t_shut_done = None
    for line in (d / 'old-pod.log').read_text(errors='replace').splitlines():
        ts = line.split(' ', 1)[0]
        if 'Commencing graceful shutdown' in line and t_shut is None:
            t_shut = ms(ts[:23] + 'Z')
        if 'Graceful shutdown complete' in line and t_shut_done is None:
            t_shut_done = ms(ts[:23] + 'Z')
    ing = (d / 'ingress.log').read_text(errors='replace')
    kinds = {
        'connection_refused': len(re.findall(r'connect\(\) failed \(111: Connection refused\)', ing)),
        'prematurely_closed': len(re.findall(r'upstream prematurely closed connection', ing)),
    }
    tot_f = sum(summ[e]['failed'] for e in ('get_fast', 'get_slow', 'post'))
    tot_n = sum(summ[e]['total'] for e in ('get_fast', 'get_slow', 'post'))
    ft = [f['t'] for f in fails]
    # 기준 0초: 옛 파드 Spring 이 종료를 시작한 시각. 로그가 없으면 API 서버 삭제 시각(초 단위)
    base = t_shut if t_shut is not None else t_del
    rel = lambda t: None if (t is None or base is None) else round((t - base) / 1000, 3)
    return {
        'label': meta['label'], 'valid': meta.get('valid'), 'prestop': meta['prestop'], 'grace': meta['grace'],
        'time_base': 'spring_shutdown_start' if t_shut is not None else 'pod_deletion_timestamp',
        'failed': tot_f, 'total': tot_n, 'rate_pct': round(100 * tot_f / tot_n, 3),
        'by_ep': {e: summ[e] for e in ('get_fast', 'get_slow', 'post')},
        'status_codes': sorted({f['status'] for f in fails}),
        'dropped_iterations': summ.get('dropped_iterations'),
        # 아래 시각은 옛 파드 Spring 이 SIGTERM 을 받아 종료를 시작한 시각(0초) 기준
        'fail_window_s': [rel(min(ft)), rel(max(ft))] if ft else None,
        'endpoint_terminating_seen_s': rel(t_term),
        'endpoint_removed_seen_s': rel(t_ep_gone),
        'spring_shutdown_done_s': rel(t_shut_done),
        'pod_deletion_ts_s': rel(t_del),
        'nginx_error_lines': kinds,
    }

rows = [one(a) for a in sys.argv[1:]]
for r in rows:
    print(json.dumps(r, ensure_ascii=False))
v = [r for r in rows if r['valid'] == 'true']
if len(v) >= 2:
    rates = [r['rate_pct'] for r in v]
    fails = [r['failed'] for r in v]
    print(json.dumps({'runs': len(v), 'failed_median': statistics.median(fails), 'failed_range': [min(fails), max(fails)],
                      'rate_pct_median': statistics.median(rates), 'rate_pct_range': [min(rates), max(rates)]}, ensure_ascii=False))
