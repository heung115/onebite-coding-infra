#!/usr/bin/env python3
"""Build and push B2 JRE runtime images to unique Docker Hub experiment tags."""
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

ROOT = Path(os.environ["BACKEND_SOURCE_DIR"])
OUT = Path(__file__).parent / 'raw'
REPO = 'heung115/spaghetti-be'
BUILDER = 'onebite-image-delivery'
COMMIT = '8cb156fc22e7111c4bfb02cfae763119cb5985b4'
SRC = Path('src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java')
DOCKERFILE_PATH = Path('dockerfile')
source = subprocess.check_output(['git', 'show', f'{COMMIT}:{SRC.as_posix()}'], cwd=ROOT)
build_gradle = subprocess.check_output(['git', 'show', f'{COMMIT}:build.gradle'], cwd=ROOT)
dockerfile = subprocess.check_output(['git', 'show', f'{COMMIT}:{DOCKERFILE_PATH.as_posix()}'], cwd=ROOT)
repro = b'''\nimport org.gradle.api.tasks.bundling.AbstractArchiveTask\n\ntasks.withType(AbstractArchiveTask).configureEach {\n    preserveFileTimestamps = false\n    reproducibleFileOrder = true\n}\n'''
b2_dockerfile = b'''# B2: B1R reproducible layered JAR with Temurin JRE runtime
FROM gradle:8.5-jdk17 AS builder
WORKDIR /app
COPY . .
RUN chmod +x ./gradlew
RUN ./gradlew clean build -x test
RUN JAR_FILE=$(find /app/build/libs -maxdepth 1 -type f -name '*.jar' ! -name '*-plain.jar' -print -quit) && sha256sum "$JAR_FILE"
RUN JAR_FILE=$(find /app/build/libs -maxdepth 1 -type f -name '*.jar' ! -name '*-plain.jar' -print -quit) && java -Djarmode=tools -jar "$JAR_FILE" extract --layers --destination /app/extracted && find /app/extracted -exec touch -h -d '@315532800' {} +
FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=builder /app/extracted/dependencies/ ./
COPY --from=builder /app/extracted/spring-boot-loader/ ./
COPY --from=builder /app/extracted/snapshot-dependencies/ ./
COPY --from=builder /app/extracted/application/ ./
EXPOSE 8080
ENTRYPOINT ["java", "-jar", "/app/spaghetti-0.0.1-SNAPSHOT.jar"]
'''
accept = ', '.join([
    'application/vnd.oci.image.index.v1+json',
    'application/vnd.docker.distribution.manifest.list.v2+json',
    'application/vnd.oci.image.manifest.v1+json',
    'application/vnd.docker.distribution.manifest.v2+json',
])


def run(args, check=True):
    return subprocess.run(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=check)


def token():
    url = f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPO}:pull'
    return json.load(urllib.request.urlopen(url))['token']


def get(url, accept_type=None):
    headers = {'Authorization': f'Bearer {token()}'}
    if accept_type:
        headers['Accept'] = accept_type
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120)


def manifest_for(tag_or_digest):
    url = f'https://registry-1.docker.io/v2/{REPO}/manifests/{tag_or_digest}'
    try:
        with get(url, accept) as response:
            return json.load(response), response.headers.get('Docker-Content-Digest', '')
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def pulse(number):
    marker = b'package code.rice.bowl.spaghetti;\n\n'
    if source.count(marker) != 1:
        raise RuntimeError('Source pulse insertion point is ambiguous')
    return source.replace(marker, marker + f'// Delivery image source pulse {number}.\n\n'.encode(), 1)


def inventory(descriptor):
    url = f"https://registry-1.docker.io/v2/{REPO}/blobs/{descriptor['digest']}"
    with get(url) as response:
        payload = response.read()
    entries = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        for member in archive:
            entry = {'path': member.name, 'type': 'file' if member.isfile() else 'directory' if member.isdir() else 'other',
                     'mode': member.mode, 'uid': member.uid, 'gid': member.gid, 'mtime': member.mtime, 'size': member.size}
            if member.isfile():
                entry['sha256'] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            entries.append(entry)
    files_hash = hashlib.sha256('\n'.join(
        f"{x['path']}\0{x['size']}\0{x.get('sha256', '')}" for x in entries).encode()).hexdigest()
    return {'digest': descriptor['digest'], 'compressed_bytes': descriptor['size'],
            'file_count': sum(x['type'] == 'file' for x in entries), 'files_sha256': files_hash,
            'entries': entries}


def smoke_test(image_tag):
    name = 'b2-jre-smoke'
    launch = run(['docker', 'run', '-d', '--name', name, '--publish', '127.0.0.1:18081:8080',
                  '--env', 'DB_URI=jdbc:postgresql://127.0.0.1:5432/spaghetti',
                  '--env', 'DB_USERNAME=smoke', '--env', 'DB_PASSWORD=smoke',
                  '--env', 'REDIS_HOST=127.0.0.1', '--env', 'REDIS_PORT=6379', '--env', 'REDIS_PASS=smoke',
                  '--env', 'JWT_SECRET=b2-application-smoke-secret-long-enough-32bytes', '--env', 'AI_SERVER_URL=http://127.0.0.1:9999',
                  '--env', 'AI_API_KEY=smoke', '--env', 'SPRING_JPA_HIBERNATE_DDL_AUTO=none',
                  '--env', 'SPRING_JPA_PROPERTIES_HIBERNATE_BOOT_ALLOW_JDBC_METADATA_ACCESS=false',
                  f'{REPO}:{image_tag}'], check=False)
    record = {'image_tag': f'{REPO}:{image_tag}', 'launch_returncode': launch.returncode,
              'launch_output': launch.stdout, 'container_name': name}
    if launch.returncode == 0:
        state = 'unknown'
        for _ in range(30):
            time.sleep(1)
            status = run(['docker', 'inspect', '--format', '{{.State.Status}}', name], check=False)
            if status.returncode:
                state = 'exited-or-removed'
                break
            state = status.stdout.strip()
            if state != 'running':
                break
            logs = run(['docker', 'logs', name], check=False).stdout
            if 'Started SpaghettiApplication' in logs:
                break
        record['container_state'] = state
        logs = run(['docker', 'logs', name], check=False).stdout
        java = run(['docker', 'exec', name, 'java', '-version'], check=False) if state == 'running' else None
        http = run(['curl', '--silent', '--show-error', '--output', '/dev/null', '--write-out', '%{http_code}',
                    '--max-time', '3', 'http://127.0.0.1:18081/'], check=False)
        record['application_started_log'] = 'Started SpaghettiApplication' in logs
        record['application_logs'] = logs
        record['java_version_returncode'] = java.returncode if java else None
        record['java_version_output'] = java.stdout if java else ''
        record['http_status_root'] = http.stdout.strip() if http.returncode == 0 else None
        record['http_probe_returncode'] = http.returncode
        run(['docker', 'stop', name], check=False)
        run(['docker', 'rm', name], check=False)
    (OUT / 'b2-eks-jre-application-smoke.json').write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps({'smoke_test': record.get('container_state'),
                      'application_started_log': record.get('application_started_log'),
                      'java_version': record.get('java_version_output'),
                      'http_status_root': record.get('http_status_root')}), flush=True)


def measure(tag, pulse_number):
    if manifest_for(tag) is not None:
        raise RuntimeError(f'Refusing to overwrite {tag}')
    (ROOT / 'build.gradle').write_bytes(build_gradle + repro)
    (ROOT / DOCKERFILE_PATH).write_bytes(b2_dockerfile)
    current_source = source if pulse_number is None else pulse(pulse_number)
    (ROOT / SRC).write_bytes(current_source)
    expected = {' M build.gradle', ' M dockerfile'}
    if pulse_number is not None:
        expected.add(f' M {SRC.as_posix()}')
    if set(run(['git', 'status', '--short']).stdout.splitlines()) != expected:
        raise RuntimeError('Unexpected temp checkout changes')

    key = tag.replace(':', '-')
    archive = Path(f'/tmp/{key}.docker.tar')
    built = run(['docker', 'buildx', 'build', '--builder', BUILDER, '--platform', 'linux/amd64',
                 '--no-cache', '--provenance=false', '--progress=plain', '--tag', f'{REPO}:{tag}',
                 '--output', f'type=docker,dest={archive}', '.'], check=False)
    (OUT / f'{key}-build.log').write_text(built.stdout)
    if built.returncode:
        raise RuntimeError(f'Build failed; see raw/{key}-build.log')
    jar_match = re.search(r'\b([0-9a-f]{64})\s+/app/build/libs/[^\s]+\.jar', built.stdout)
    if not jar_match:
        raise RuntimeError(f'bootJar hash missing in raw/{key}-build.log')
    bootjar_hash = jar_match.group(1)
    loaded = run(['docker', 'load', '--input', str(archive)])
    (OUT / f'{key}-load.log').write_text(loaded.stdout)
    archive.unlink(missing_ok=True)
    if pulse_number is None:
        smoke_test(tag)

    started = dt.datetime.now(ZoneInfo('Asia/Seoul')).isoformat()
    before = time.monotonic()
    pushed = run(['docker', 'push', f'{REPO}:{tag}'], check=False)
    seconds = round(time.monotonic() - before, 3)
    ended = dt.datetime.now(ZoneInfo('Asia/Seoul')).isoformat()
    (OUT / f'{key}-push.log').write_text(pushed.stdout)
    if pushed.returncode:
        raise RuntimeError(f'Push failed; see raw/{key}-push.log')

    top, index_digest = manifest_for(tag)
    if 'layers' in top:
        image, platform_digest = top, index_digest
    else:
        child = next(x for x in top['manifests'] if x.get('platform', {}).get('os') == 'linux' and x.get('platform', {}).get('architecture') == 'amd64')
        image, _ = manifest_for(child['digest'])
        platform_digest = child['digest']
    with get(f"https://registry-1.docker.io/v2/{REPO}/blobs/{image['config']['digest']}") as response:
        config = json.load(response)
    statuses = re.findall(r'^([0-9a-f]{8,64}): (Pushed|Layer already exists|Mounted from .+)$', pushed.stdout, re.M)
    histories = [x.get('created_by', '') for x in config['history'] if not x.get('empty_layer')]
    layers, dependencies, application = [], None, None
    for i, descriptor in enumerate(image['layers']):
        digest_hex = descriptor['digest'].removeprefix('sha256:')
        status = next((s for short, s in statuses if digest_hex.startswith(short)), 'status-unmatched')
        layer = {'index': i, 'digest': descriptor['digest'], 'compressed_bytes': descriptor['size'],
                 'diff_id': config['rootfs']['diff_ids'][i], 'history': histories[i] if i < len(histories) else '',
                 'push_status': status}
        layers.append(layer)
        if 'COPY /app/extracted/dependencies/' in layer['history']:
            dependencies = inventory(descriptor)
        if 'COPY /app/extracted/application/' in layer['history']:
            application = layer
    if dependencies is None or application is None:
        raise RuntimeError(f'Could not identify payload layers for {tag}')

    git_blob = b'blob ' + str(len(current_source)).encode() + b'\0' + current_source
    record = {'tag': tag, 'condition': 'B2-JRE-runtime', 'source_commit': COMMIT, 'source_pulse': pulse_number,
              'source_blob_sha': hashlib.sha1(git_blob).hexdigest(), 'bootjar_sha256': bootjar_hash,
              'builder_base': 'gradle:8.5-jdk17', 'runtime_base': 'eclipse-temurin:17-jre',
              'runtime_base_index_digest': 'sha256:207ecae0b2b104dfc6dfa763d1e9cd2041cb344800a180e24cb1a28ed533ff90',
              'runtime_base_linux_amd64_digest': 'sha256:0a451f1fe167afde19385156f05af505bdcd35e5680151d886d987b999d73d53',
              'builder': BUILDER, 'remote_cache': False,
              'archive_reproducibility': {'preserveFileTimestamps': False, 'reproducibleFileOrder': True,
                                          'scope': 'all AbstractArchiveTask tasks'},
              'push_started_at_kst': started, 'push_completed_at_kst': ended, 'push_seconds': seconds,
              'docker_push_status': 'success', 'index_digest': index_digest,
              'platform_manifest_digest': platform_digest,
              'compressed_layer_bytes': sum(x['size'] for x in image['layers']),
              'manifest': top, 'layers': layers, 'dependencies': dependencies,
              'application': application, 'push_output': pushed.stdout}
    (OUT / f'{key}.json').write_text(json.dumps(record, indent=2) + '\n')
    result = {'tag': tag, 'bootjar_sha256': bootjar_hash,
              'dependencies_digest': dependencies['digest'], 'dependencies_bytes': dependencies['compressed_bytes'],
              'dependencies_files_sha256': dependencies['files_sha256'], 'dependencies_files': dependencies['file_count'],
              'application_digest': application['digest'], 'application_bytes': application['compressed_bytes'],
              'new_blob_bytes': sum(x['compressed_bytes'] for x in layers if x['push_status'] == 'Pushed'),
              'push_seconds': seconds, 'index_digest': index_digest, 'platform_manifest_digest': platform_digest}
    print(json.dumps(result), flush=True)
    return record


if run(['git', 'rev-parse', 'HEAD']).stdout.strip() != COMMIT:
    raise SystemExit('Temporary clone is not at baseline source commit')
if (ROOT / SRC).read_bytes() != source or (ROOT / DOCKERFILE_PATH).read_bytes() != dockerfile:
    raise SystemExit('Temporary clone is not clean baseline source/Dockerfile')
records = []
try:
    for pulse_number in [None, 1, 2, 3]:
        tag = 'delivery-eks-b2-baseline' if pulse_number is None else f'delivery-eks-b2-pulse-{pulse_number}'
        records.append(measure(tag, pulse_number))
finally:
    (ROOT / 'build.gradle').write_bytes(build_gradle)
    (ROOT / DOCKERFILE_PATH).write_bytes(dockerfile)
    (ROOT / SRC).write_bytes(source)

summary = []
for record in records:
    summary.append({'tag': record['tag'], 'source_pulse': record['source_pulse'],
                    'bootjar_sha256': record['bootjar_sha256'], 'dependencies': {
                        key: record['dependencies'][key] for key in ['digest', 'compressed_bytes', 'files_sha256', 'file_count']},
                    'application': {key: record['application'][key] for key in ['digest', 'compressed_bytes', 'push_status']},
                    'new_blob_bytes': sum(x['compressed_bytes'] for x in record['layers'] if x['push_status'] == 'Pushed'),
                    'push_seconds': record['push_seconds'], 'index_digest': record['index_digest'],
                    'platform_manifest_digest': record['platform_manifest_digest'],
                    'compressed_layer_bytes': record['compressed_layer_bytes']})
    (OUT / 'b2-eks-push-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
