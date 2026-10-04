#!/usr/bin/env python3
"""Measure cold and source-only warm image pulls on fresh, identical EKS workers."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).parent
RAW = ROOT / 'raw'
KUBECONFIG = '/tmp/onebite-cicd-image-delivery-kubeconfig'
REGION = 'ap-northeast-2'
CLUSTER = 'onebite-cicd'
NODEGROUP = 'onebite-cicd-image-delivery-exp'
NAMESPACE = 'image-delivery-exp-cicd-20261003'
REPOSITORY = 'heung115/spaghetti-be'
NODEGROUP_LABEL = 'eks.amazonaws.com/nodegroup'
AWS_BIN = shutil.which('aws') or os.path.expanduser('~/.local/bin/aws')
POSTGRES_HOST = None
ORDER = [
    (1, 'b0'), (1, 'b1r'), (1, 'b2'),
    (2, 'b1r'), (2, 'b2'), (2, 'b0'),
    (3, 'b2'), (3, 'b0'), (3, 'b1r'),
]
IMAGE_TAGS = {
    'b0': ('delivery-b0-baseline-local', 'delivery-b0-pulse-{pulse}'),
    'b1r': ('delivery-eks-b1r-baseline', 'delivery-eks-b1r-pulse-{pulse}'),
    'b2': ('delivery-eks-b2-baseline', 'delivery-eks-b2-pulse-{pulse}'),
}
NOW = lambda: dt.datetime.now(dt.timezone.utc)


def run(args, *, check=True, input_text=None, capture=True):
    env = dict(os.environ, KUBECONFIG=KUBECONFIG, AWS_REGION=REGION)
    env['PATH'] = f"{Path(AWS_BIN).parent}:{env.get('PATH', '')}"
    p = subprocess.run(args, text=True, input=input_text, stdout=subprocess.PIPE if capture else None,
                       stderr=subprocess.STDOUT if capture else None, env=env)
    if check and p.returncode:
        raise RuntimeError(f"Command failed ({p.returncode}): {' '.join(args)}\n{p.stdout or ''}")
    return p


def kubectl(*args, **kwargs):
    return run(['kubectl', *args], **kwargs)


def json_cmd(args):
    return json.loads(run(args).stdout)


def aws(*args):
    return run([AWS_BIN, *args, '--region', REGION, '--output', 'json'])


def parse_time(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00'))


def delta_ms(a, b):
    x, y = parse_time(a), parse_time(b)
    return round((y - x).total_seconds() * 1000, 1) if x and y else None


def get_group_desired():
    return json_cmd([AWS_BIN, 'eks', 'describe-nodegroup', '--cluster-name', CLUSTER,
                     '--nodegroup-name', NODEGROUP, '--region', REGION, '--query', 'nodegroup.scalingConfig'])['desiredSize']


def set_desired(value):
    aws('eks', 'update-nodegroup-config', '--cluster-name', CLUSTER, '--nodegroup-name', NODEGROUP,
        '--scaling-config', f'minSize=0,maxSize=1,desiredSize={value}')
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        state = json_cmd([AWS_BIN, 'eks', 'describe-nodegroup', '--cluster-name', CLUSTER,
                          '--nodegroup-name', NODEGROUP, '--region', REGION])['nodegroup']
        if state['status'] == 'ACTIVE' and state['scalingConfig']['desiredSize'] == value:
            return
        time.sleep(3)
    raise TimeoutError(f'Node group did not reach desiredSize={value}')


def group_nodes():
    items = json_cmd(['kubectl', 'get', 'nodes', '-l', f'{NODEGROUP_LABEL}={NODEGROUP}', '-o', 'json'])['items']
    return items


def wait_node(previous_instance_id=None):
    deadline = time.monotonic() + 480
    while time.monotonic() < deadline:
        nodes = group_nodes()
        for node in nodes:
            if node['status'].get('conditions', []) and next(
                    (c['status'] == 'True' for c in node['status']['conditions'] if c['type'] == 'Ready'), False):
                instance_id = node['spec']['providerID'].split('/')[-1]
                if instance_id != previous_instance_id:
                    return node, instance_id
        time.sleep(3)
    raise TimeoutError('No new Ready experiment node appeared')


def wait_nodes_gone(old_instance_id):
    details = json_cmd([AWS_BIN, 'autoscaling', 'describe-auto-scaling-instances', '--instance-ids', old_instance_id,
                        '--region', REGION])['AutoScalingInstances']
    if details:
        instance = details[0]
        if instance.get('LifecycleState') == 'Terminating:Wait':
            completed = run([AWS_BIN, 'autoscaling', 'complete-lifecycle-action',
                             '--lifecycle-hook-name', 'Terminate-LC-Hook',
                             '--auto-scaling-group-name', instance['AutoScalingGroupName'],
                             '--lifecycle-action-result', 'CONTINUE', '--instance-id', old_instance_id,
                             '--region', REGION, '--output', 'json'], check=False)
            (RAW / f'eks-{old_instance_id}-terminate-hook.log').write_text(completed.stdout or '')
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        nodes = group_nodes()
        ids = {n['spec'].get('providerID', '').split('/')[-1] for n in nodes}
        instance_state_result = run([AWS_BIN, 'ec2', 'describe-instances', '--instance-ids', old_instance_id,
                                     '--region', REGION, '--query', 'Reservations[0].Instances[0].State.Name',
                                     '--output', 'text'], check=False)
        instance_state = (instance_state_result.stdout or '').strip()
        if instance_state == 'terminated' and old_instance_id in ids:
            stale_node = next(n for n in nodes if n['spec'].get('providerID', '').split('/')[-1] == old_instance_id)
            kubectl('delete', 'node', stale_node['metadata']['name'], '--wait=false', check=False)
        if instance_state == 'terminated' and not ids:
            return
        time.sleep(3)
    raise TimeoutError(f'Experiment node did not disappear: {old_instance_id}')


def ensure_postgres(node_name):
    global POSTGRES_HOST
    pod = {
        'apiVersion': 'v1', 'kind': 'Pod',
        'metadata': {'name': 'image-delivery-postgres', 'namespace': NAMESPACE, 'labels': {'app': 'image-delivery-postgres'}},
        'spec': {
            'restartPolicy': 'Always', 'terminationGracePeriodSeconds': 5,
            'nodeName': node_name,
            'nodeSelector': {'node.kubernetes.io/instance-type': 'm7i-flex.large', 'kubernetes.io/arch': 'amd64'},
            'tolerations': [{'key': 'measure/only', 'operator': 'Equal', 'value': 'experiment', 'effect': 'NoSchedule'}],
            'containers': [{
                'name': 'postgres', 'image': 'postgres:17-alpine', 'imagePullPolicy': 'IfNotPresent',
                'ports': [{'name': 'postgres', 'containerPort': 5432}],
                'env': [{'name': 'POSTGRES_USER', 'value': 'measure'}, {'name': 'POSTGRES_PASSWORD', 'value': 'measure'},
                        {'name': 'POSTGRES_DB', 'value': 'postgres'}],
                'readinessProbe': {'exec': {'command': ['sh', '-c', 'pg_isready -U measure -d postgres']},
                                   'initialDelaySeconds': 2, 'periodSeconds': 2, 'timeoutSeconds': 1, 'failureThreshold': 30},
                'resources': {'requests': {'cpu': '100m', 'memory': '256Mi'}, 'limits': {'cpu': '500m', 'memory': '1Gi'}},
                'volumeMounts': [{'name': 'postgres-data', 'mountPath': '/var/lib/postgresql/data'}],
            }],
            'volumes': [{'name': 'postgres-data', 'emptyDir': {}}],
        },
    }
    service = {'apiVersion': 'v1', 'kind': 'Service',
               'metadata': {'name': 'image-delivery-postgres', 'namespace': NAMESPACE},
               'spec': {'selector': {'app': 'image-delivery-postgres'},
                        'ports': [{'name': 'postgres', 'port': 5432, 'targetPort': 'postgres'}]}}
    pod_path, svc_path = RAW / 'eks-postgres-pod.yaml', RAW / 'eks-postgres-service.yaml'
    pod_path.write_text(json.dumps(pod, indent=2) + '\n')
    svc_path.write_text(json.dumps(service, indent=2) + '\n')
    for path in (svc_path, pod_path):
        result = kubectl('apply', '-f', str(path))
        (RAW / f'{path.stem}-apply.log').write_text(result.stdout)
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        state = pod_json('image-delivery-postgres')
        conditions = state.get('status', {}).get('conditions', [])
        if any(c['type'] == 'Ready' and c['status'] == 'True' for c in conditions):
            service_state = json_cmd(['kubectl', 'get', 'service', 'image-delivery-postgres',
                                      '-n', NAMESPACE, '-o', 'json'])
            POSTGRES_HOST = service_state['spec']['clusterIP']
            (RAW / 'eks-postgres-pod.json').write_text(json.dumps(state, indent=2) + '\n')
            (RAW / 'eks-postgres-service.json').write_text(json.dumps(service_state, indent=2) + '\n')
            (RAW / 'eks-postgres-application.log').write_text(
                kubectl('logs', 'image-delivery-postgres', '-n', NAMESPACE, '--timestamps').stdout)
            return
        time.sleep(2)
    raise TimeoutError('Ephemeral Postgres did not become Ready')


def create_trial_database(name):
    result = kubectl('exec', '-n', NAMESPACE, 'image-delivery-postgres', '--',
                     'createdb', '-U', 'measure', '-O', 'measure', name)
    (RAW / f'eks-database-{name}-create.log').write_text(result.stdout)


def drop_trial_database(name):
    result = kubectl('exec', '-n', NAMESPACE, 'image-delivery-postgres', '--',
                     'dropdb', '-U', 'measure', '--if-exists', name, check=False)
    (RAW / f'eks-database-{name}-drop.log').write_text(result.stdout or '')


def make_pod(name, image, candidate, trial, phase, database_name, node_name=None):
    pod = {
        'apiVersion': 'v1', 'kind': 'Pod',
        'metadata': {'name': name, 'namespace': NAMESPACE,
                     'labels': {'app': 'image-delivery-exp', 'candidate': candidate,
                                'trial': str(trial), 'phase': phase}},
        'spec': {
            'restartPolicy': 'Always', 'terminationGracePeriodSeconds': 1,
            'tolerations': [{'key': 'measure/only', 'operator': 'Equal', 'value': 'experiment', 'effect': 'NoSchedule'}],
            'containers': [{
                'name': 'backend', 'image': f'{REPOSITORY}:{image}', 'imagePullPolicy': 'Always',
                'ports': [{'name': 'http', 'containerPort': 8080}],
                'env': [
                    {'name': 'DB_URI', 'value': f'jdbc:postgresql://{POSTGRES_HOST}:5432/{database_name}'},
                    {'name': 'DB_USERNAME', 'value': 'measure'}, {'name': 'DB_PASSWORD', 'value': 'measure'},
                    {'name': 'REDIS_HOST', 'value': '127.0.0.1'}, {'name': 'REDIS_PORT', 'value': '6379'},
                    {'name': 'REDIS_PASS', 'value': 'measure'}, {'name': 'JWT_SECRET', 'value': 'image-delivery-experiment-jwt-secret-32bytes'},
                    {'name': 'AI_SERVER_URL', 'value': 'http://127.0.0.1:9999'}, {'name': 'AI_API_KEY', 'value': 'measure'},
                    {'name': 'SPRING_JPA_HIBERNATE_DDL_AUTO', 'value': 'update'},
                ],
                'readinessProbe': {'httpGet': {'path': '/test', 'port': 'http'}, 'initialDelaySeconds': 70,
                                   'periodSeconds': 10, 'failureThreshold': 3, 'timeoutSeconds': 2},
                'livenessProbe': {'httpGet': {'path': '/test', 'port': 'http'}, 'initialDelaySeconds': 70,
                                  'periodSeconds': 10, 'failureThreshold': 3, 'timeoutSeconds': 2},
                'resources': {'requests': {'cpu': '500m', 'memory': '1Gi'},
                              'limits': {'cpu': '1500m', 'memory': '2Gi'}},
            }],
        },
    }
    if node_name:
        pod['spec']['nodeName'] = node_name
    else:
        pod['spec']['nodeSelector'] = {NODEGROUP_LABEL: NODEGROUP,
                                       'node.kubernetes.io/instance-type': 'm7i-flex.large',
                                       'kubernetes.io/arch': 'amd64'}
    return pod


def pod_json(name):
    return json_cmd(['kubectl', 'get', 'pod', name, '-n', NAMESPACE, '-o', 'json'])


def events_for(name):
    events = json_cmd(['kubectl', 'get', 'events', '-n', NAMESPACE, '-o', 'json'])['items']
    return [e for e in events if e.get('regarding', e.get('involvedObject', {})).get('name') == name]


def event_time(event):
    return event.get('eventTime') or event.get('firstTimestamp') or event.get('series', {}).get('lastObservedTime') or event.get('lastTimestamp')


def open_http(port):
    try:
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=1) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except Exception:
        return None


def collect_pod(candidate, trial, phase, image, node_name, run_number):
    name = f'{candidate}-r{trial}-{phase}'
    database_name = f'app_{candidate}_r{trial}_{phase}'
    create_trial_database(database_name)
    manifest = make_pod(name, image, candidate, trial, phase, database_name,
                        node_name if phase == 'warm' else None)
    yaml_path = RAW / f'eks-{name}.yaml'
    yaml_path.write_text(json.dumps(manifest, indent=2) + '\n')
    creation_wall = NOW().isoformat()
    created = kubectl('apply', '-f', str(yaml_path))
    (RAW / f'eks-{name}-apply.log').write_text(created.stdout)
    forward_path = RAW / f'eks-{name}-port-forward.log'
    forward_env = dict(os.environ, KUBECONFIG=KUBECONFIG, AWS_REGION=REGION)
    forward_env['PATH'] = f"{Path(AWS_BIN).parent}:{forward_env.get('PATH', '')}"
    forward_file = None
    forward = None
    http_at = None
    http_code = None
    app_started_at = None
    ready_at = None
    start = time.monotonic()
    latest_pod = None
    latest_logs = ''
    while time.monotonic() - start < 300:
        latest_pod = pod_json(name)
        logs_result = kubectl('logs', name, '-n', NAMESPACE, '--timestamps', check=False)
        latest_logs = logs_result.stdout or ''
        if 'Started SpaghettiApplication' in latest_logs:
            match = re.search(r'\b(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z)\s+INFO .*Started SpaghettiApplication', latest_logs)
            app_started_at = match.group(1) if match else None
        statuses = latest_pod.get('status', {}).get('containerStatuses', [])
        if statuses and statuses[0].get('restartCount', 0) > 0:
            previous = kubectl('logs', name, '-n', NAMESPACE, '--previous', '--timestamps', check=False)
            previous_logs = previous.stdout or ''
            (RAW / f'eks-{name}-previous-application.log').write_text(previous_logs)
            if previous_logs:
                latest_logs = previous_logs
            break
        if statuses and statuses[0].get('state', {}).get('running'):
            started = statuses[0]['state']['running'].get('startedAt')
        else:
            started = None
        if app_started_at and (forward is None or forward.poll() is not None):
            if forward_file:
                forward_file.close()
            forward_file = forward_path.open('a')
            forward = subprocess.Popen(['kubectl', '--kubeconfig', KUBECONFIG, 'port-forward', '-n', NAMESPACE,
                                        f'pod/{name}', '18080:8080'], stdout=forward_file,
                                       stderr=subprocess.STDOUT, text=True, env=forward_env)
        if started and http_code != 200:
            code = open_http(18080)
            if code == 200:
                http_code, http_at = code, NOW().isoformat()
        conditions = latest_pod.get('status', {}).get('conditions', [])
        if any(c['type'] == 'Ready' and c['status'] == 'True' for c in conditions):
            ready_at = next(c.get('lastTransitionTime') for c in conditions if c['type'] == 'Ready' and c['status'] == 'True')
        if app_started_at and http_code == 200 and ready_at:
            break
        if latest_pod.get('status', {}).get('phase') in ('Failed', 'Succeeded'):
            break
        time.sleep(1)
    if forward:
        forward.terminate()
        try:
            forward.wait(timeout=5)
        except subprocess.TimeoutExpired:
            forward.kill()
    if forward_file:
        forward_file.close()
    latest_pod = pod_json(name)
    latest_logs = kubectl('logs', name, '-n', NAMESPACE, '--timestamps', check=False).stdout or latest_logs
    event_data = events_for(name)
    event_path = RAW / f'eks-{name}-events.json'
    event_path.write_text(json.dumps(event_data, indent=2) + '\n')
    pod_path = RAW / f'eks-{name}-pod.json'
    pod_path.write_text(json.dumps(latest_pod, indent=2) + '\n')
    (RAW / f'eks-{name}-application.log').write_text(latest_logs)
    reasons = {}
    for event in event_data:
        reason = event.get('reason')
        if reason in ('Pulling', 'Pulled', 'Failed', 'BackOff'):
            message = event.get('note', event.get('message'))
            duration = re.search(r'\bin ([0-9.]+)s \(', message or '') if reason == 'Pulled' else None
            reasons.setdefault(reason, []).append({'time': event_time(event),
                'first_timestamp': event.get('firstTimestamp'), 'last_timestamp': event.get('lastTimestamp'),
                'event_time': event.get('eventTime'), 'count': event.get('count'),
                'reported_pull_seconds': float(duration.group(1)) if duration else None, 'message': message})
    pulling = next((x['time'] for x in reasons.get('Pulling', []) if x['time']), None)
    pulled = next((x['time'] for x in reasons.get('Pulled', []) if x['time']), None)
    statuses = latest_pod.get('status', {}).get('containerStatuses', [])
    container_started = statuses[0].get('state', {}).get('running', {}).get('startedAt') if statuses else None
    record = {
        'candidate': candidate, 'trial': trial, 'run_number': run_number, 'phase': phase, 'tag': image,
        'image': f'{REPOSITORY}:{image}', 'pod': name, 'database': database_name, 'pod_creation_client_time': creation_wall,
        'pod_creation_timestamp': latest_pod.get('metadata', {}).get('creationTimestamp'),
        'node': node_name, 'node_instance_type': 'm7i-flex.large', 'node_architecture': 'amd64',
        'pull_start_event': pulling, 'pull_complete_event': pulled,
        'pull_elapsed_ms_from_kubernetes_events': delta_ms(pulling, pulled),
        'pull_events': reasons, 'container_started_at': container_started,
        'spring_application_started_at': app_started_at, 'pod_ready_at': ready_at,
        'http_status_root': http_code, 'http_200_observed_at': http_at,
        'pod_phase': latest_pod.get('status', {}).get('phase'),
        'container_state': statuses[0].get('state') if statuses else None,
        'restart_count': statuses[0].get('restartCount') if statuses else None,
        'application_start_from_container_ms': delta_ms(container_started, app_started_at),
        'ready_from_container_start_ms': delta_ms(container_started, ready_at),
        'ready_from_spring_start_ms': delta_ms(app_started_at, ready_at),
        'pod_json': str(pod_path), 'events_json': str(event_path), 'application_log': str(RAW / f'eks-{name}-application.log'),
        'success': bool(container_started and app_started_at and http_code == 200 and ready_at),
    }
    (RAW / f'eks-{name}-measurement.json').write_text(json.dumps(record, indent=2) + '\n')
    if not record['success']:
        raise RuntimeError(f'Pod measurement incomplete for {name}: {json.dumps({k: record[k] for k in ["pod_phase", "pull_events", "container_started_at", "spring_application_started_at", "http_status_root", "pod_ready_at"]})}')
    return record


def main():
    if get_group_desired() != 0 or group_nodes():
        raise RuntimeError('Experiment node group must start at desiredSize=0 with no registered nodes')
    ns_result = kubectl('create', 'namespace', NAMESPACE, check=False)
    if ns_result.returncode:
        raise RuntimeError(f'Refusing to reuse existing experiment namespace: {ns_result.stdout}')
    (RAW / 'eks-delivery-namespace.log').write_text(ns_result.stdout)
    (RAW / 'eks-delivery-trial-order.json').write_text(json.dumps({
        'cluster': CLUSTER, 'region': REGION, 'node_group': NODEGROUP,
        'initial_nodegroup_desired_size': 0, 'node_type': 'm7i-flex.large', 'architecture': 'amd64',
        'round_robin_order': [{'round': r, 'candidate': c, 'pulse': r} for r, c in ORDER],
        'probes': {'path': '/test', 'initialDelaySeconds': 70, 'periodSeconds': 10, 'failureThreshold': 3, 'timeoutSeconds': 2},
        'resources': {'requests': {'cpu': '500m', 'memory': '1Gi'}, 'limits': {'cpu': '1500m', 'memory': '2Gi'}},
        'warm_pull': 'source-only pulse image pulled on the same fresh node after baseline image run',
    }, indent=2) + '\n')
    trials_path = RAW / 'eks-delivery-trials.json'
    records = json.loads(trials_path.read_text()) if trials_path.exists() else []
    previous_instance_id = None
    run_number = len(records) // 2
    try:
        for trial, candidate in ORDER:
            completed_phases = {r.get('phase') for r in records
                                if r.get('candidate') == candidate and r.get('trial') == trial and r.get('success')}
            if completed_phases == {'cold', 'warm'}:
                continue
            run_number += 1
            print(json.dumps({'state': 'scale-up', 'run': run_number, 'trial': trial, 'candidate': candidate}), flush=True)
            set_desired(1)
            node, instance_id = wait_node(previous_instance_id)
            previous_instance_id = instance_id
            node_name = node['metadata']['name']
            ensure_postgres(node_name)
            node_record = {'name': node_name, 'instance_id': instance_id,
                           'creation_timestamp': node['metadata'].get('creationTimestamp'),
                           'instance_type': node['metadata']['labels'].get('node.kubernetes.io/instance-type'),
                           'architecture': node['metadata']['labels'].get('kubernetes.io/arch'),
                           'provider_id': node['spec'].get('providerID')}
            (RAW / f'eks-r{trial}-{candidate}-node.json').write_text(json.dumps(node_record, indent=2) + '\n')
            baseline_tag, pulse_format = IMAGE_TAGS[candidate]
            cold = collect_pod(candidate, trial, 'cold', baseline_tag, node_name, run_number)
            records.append(cold)
            kubectl('delete', 'pod', cold['pod'], '-n', NAMESPACE, '--wait=true', '--timeout=120s')
            pulse_tag = pulse_format.format(pulse=trial)
            warm = collect_pod(candidate, trial, 'warm', pulse_tag, node_name, run_number)
            records.append(warm)
            kubectl('delete', 'pod', warm['pod'], '-n', NAMESPACE, '--wait=true', '--timeout=120s')
            drop_trial_database(f'app_{candidate}_r{trial}_cold')
            drop_trial_database(f'app_{candidate}_r{trial}_warm')
            kubectl('delete', 'pod', 'image-delivery-postgres', '-n', NAMESPACE,
                    '--wait=true', '--timeout=120s', check=False)
            (RAW / 'eks-delivery-trials.json').write_text(json.dumps(records, indent=2) + '\n')
            set_desired(0)
            wait_nodes_gone(instance_id)
            print(json.dumps({'state': 'complete', 'run': run_number, 'trial': trial, 'candidate': candidate,
                              'node_instance_id': instance_id,
                              'cold_pull_ms': cold['pull_elapsed_ms_from_kubernetes_events'],
                              'warm_pull_ms': warm['pull_elapsed_ms_from_kubernetes_events'],
                              'http_200': [cold['http_status_root'], warm['http_status_root']]}), flush=True)
        (RAW / 'eks-delivery-trials.json').write_text(json.dumps(records, indent=2) + '\n')
    finally:
        for p in json_cmd(['kubectl', 'get', 'pods', '-n', NAMESPACE, '-o', 'json']).get('items', []) if ns_result.returncode == 0 else []:
            kubectl('delete', 'pod', p['metadata']['name'], '-n', NAMESPACE, '--wait=false', check=False)
        if get_group_desired() != 0:
            set_desired(0)
        remaining_nodes = group_nodes()
        for node in remaining_nodes:
            wait_nodes_gone(node['spec']['providerID'].split('/')[-1])
        # Delete only the unique experiment namespace created by this script.
        kubectl('delete', 'namespace', NAMESPACE, '--wait=false', check=False)


if __name__ == '__main__':
    main()
