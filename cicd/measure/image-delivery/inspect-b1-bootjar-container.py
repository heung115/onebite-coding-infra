#!/usr/bin/env python3
"""Compare original bootJar dependency ZIP timestamps in fresh Gradle builder containers."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(os.environ["BACKEND_SOURCE_DIR"])
OUT = Path(__file__).parent / 'raw'
COMMIT = '8cb156fc22e7111c4bfb02cfae763119cb5985b4'
SOURCE = Path('src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java')
source = subprocess.check_output(['git', 'show', f'{COMMIT}:{SOURCE.as_posix()}'], cwd=ROOT)
BUILD_GRADLE = subprocess.check_output(['git', 'show', f'{COMMIT}:build.gradle'], cwd=ROOT)
REPRO_CONFIG = b'''\nimport org.gradle.api.tasks.bundling.AbstractArchiveTask\n\ntasks.withType(AbstractArchiveTask).configureEach {\n    preserveFileTimestamps = false\n    reproducibleFileOrder = true\n}\n'''


def inspect(path):
    with zipfile.ZipFile(path) as jar:
        entries = {}
        for item in jar.infolist():
            if item.filename.startswith('BOOT-INF/lib/') and item.filename.endswith('.jar'):
                entries[item.filename] = {'timestamp': list(item.date_time), 'size': item.file_size,
                                          'crc32': f'{item.CRC:08x}'}
    return {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size,
            'dependency_entry_count': len(entries), 'dependency_entries': entries}


def build(name, contents, reproducible=False):
    (ROOT / 'build.gradle').write_bytes(BUILD_GRADLE + (REPRO_CONFIG if reproducible else b''))
    (ROOT / SOURCE).write_bytes(contents)
    result = subprocess.run(
        ['docker', 'run', '--rm', '--platform', 'linux/amd64', '--user', '0:0',
         '--volume', f'{ROOT}:/app', '--workdir', '/app', 'gradle:8.5-jdk17',
         'bash', './gradlew', '--no-daemon', 'clean', 'bootJar'],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    (OUT / f'{name}-build.log').write_text(result.stdout)
    if result.returncode:
        raise RuntimeError(f'{name} failed; see raw/{name}-build.log')
    jars = [p for p in (ROOT / 'build/libs').glob('*.jar') if not p.name.endswith('-plain.jar')]
    if len(jars) != 1:
        raise RuntimeError(f'expected one bootJar, got {jars}')
    saved = OUT / f'{name}.jar'
    saved.write_bytes(jars[0].read_bytes())
    return inspect(saved)


try:
    pulse_source = source.replace(
        b'package code.rice.bowl.spaghetti;\n\n',
        b'package code.rice.bowl.spaghetti;\n\n// Delivery image source pulse 1.\n\n', 1,
    )
    original_base_path = OUT / 'b1-original-fresh-container-baseline.jar'
    original_pulse_path = OUT / 'b1-original-fresh-container-pulse-1.jar'
    if original_base_path.exists() and original_pulse_path.exists():
        baseline = inspect(original_base_path)
        pulse = inspect(original_pulse_path)
    else:
        baseline = build('b1-original-fresh-container-baseline', source)
        pulse = build('b1-original-fresh-container-pulse-1', pulse_source)
    repro_base_a = build('b1r-fresh-container-baseline-a', source, reproducible=True)
    repro_base_b = build('b1r-fresh-container-baseline-b', source, reproducible=True)
    repro_pulse = build('b1r-fresh-container-pulse-1', pulse_source, reproducible=True)
finally:
    (ROOT / SOURCE).write_bytes(source)
    (ROOT / 'build.gradle').write_bytes(BUILD_GRADLE)

common = baseline['dependency_entries'].keys() & pulse['dependency_entries'].keys()
result = {
    'builder': 'gradle:8.5-jdk17',
    'build_isolation': 'fresh linux/amd64 container for each build; no shared Gradle home',
    'source_commit': COMMIT,
    'baseline': baseline,
    'pulse_1': pulse,
    'same_entry_paths': baseline['dependency_entries'].keys() == pulse['dependency_entries'].keys(),
    'changed_dependency_entry_timestamps': [
        name for name in sorted(common)
        if baseline['dependency_entries'][name]['timestamp'] != pulse['dependency_entries'][name]['timestamp']
    ],
    'changed_dependency_entry_count': sum(
        baseline['dependency_entries'][name]['timestamp'] != pulse['dependency_entries'][name]['timestamp']
        for name in common
    ),
    'reproducibility_config': REPRO_CONFIG.decode().strip(),
    'repro_baseline_a': repro_base_a,
    'repro_baseline_b': repro_base_b,
    'repro_pulse_1': repro_pulse,
    'repro_same_source_bootjar_sha_equal': repro_base_a['sha256'] == repro_base_b['sha256'],
    'repro_same_source_dependency_metadata_equal': repro_base_a['dependency_entries'] == repro_base_b['dependency_entries'],
    'repro_dependency_metadata_equal_baseline_to_pulse': repro_base_a['dependency_entries'] == repro_pulse['dependency_entries'],
}
(OUT / 'b1-original-fresh-container-bootjar-comparison.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({key: value for key, value in result.items() if key not in {'baseline', 'pulse_1'}}, indent=2))
print('original baseline/pulse bootJar sha256', baseline['sha256'], pulse['sha256'])
print('repro baseline A/B/pulse bootJar sha256', repro_base_a['sha256'], repro_base_b['sha256'], repro_pulse['sha256'])
