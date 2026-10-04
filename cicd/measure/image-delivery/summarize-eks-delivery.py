#!/usr/bin/env python3
"""Summarize the retained onebite-cicd image-delivery trial records."""
import json
import statistics
from pathlib import Path

ROOT = Path(__file__).parent
RAW = ROOT / 'raw'


def span(values):
    values = [float(v) for v in values if v is not None]
    if not values:
        return None
    return {
        'median': round(statistics.median(values), 3),
        'min': round(min(values), 3),
        'max': round(max(values), 3),
        'values': [round(v, 3) for v in values],
    }


trials = json.loads((RAW / 'eks-delivery-trials.json').read_text())
images = []
for candidate, prefix, baseline_file in [
    ('b0', 'delivery-b0', 'delivery-b0-baseline-local.json'),
    ('b1r', 'delivery-eks-b1r', 'delivery-eks-b1r-baseline.json'),
    ('b2', 'delivery-eks-b2', 'delivery-eks-b2-baseline.json'),
]:
    baseline = json.loads((RAW / baseline_file).read_text())
    base_layers = {layer['digest']: layer['size'] for layer in baseline['manifest']['layers']}
    pulses = []
    for pulse in range(1, 4):
        pulse_data = json.loads((RAW / f'{prefix}-pulse-{pulse}.json').read_text())
        pulse_layers = {layer['digest']: layer['size'] for layer in pulse_data['manifest']['layers']}
        new_layers = [{'digest': digest, 'compressed_bytes': size}
                      for digest, size in pulse_layers.items() if digest not in base_layers]
        pulses.append({
            'pulse': pulse,
            'source_blob_sha': pulse_data['source_blob_sha'],
            'manifest_digest': pulse_data['platform_manifest_digest'],
            'new_layers': new_layers,
            'new_layer_bytes': sum(layer['compressed_bytes'] for layer in new_layers),
        })
    rows = [row for row in trials if row['candidate'] == candidate]
    phases = {}
    for phase in ('cold', 'warm'):
        phase_rows = sorted((row for row in rows if row['phase'] == phase), key=lambda row: row['trial'])
        reported = [next((event['reported_pull_seconds'] for event in row.get('pull_events', {}).get('Pulled', [])
                          if event.get('reported_pull_seconds') is not None), None)
                    for row in phase_rows]
        phases[phase] = {
            'sample_count': len(phase_rows),
            'kubelet_reported_pull_seconds': span(reported),
            'event_timestamp_elapsed_seconds': span(
                (row.get('pull_elapsed_ms_from_kubernetes_events') or 0) / 1000 for row in phase_rows),
            'container_to_spring_started_seconds': span(
                (row.get('application_start_from_container_ms') or 0) / 1000 for row in phase_rows),
            'container_to_pod_ready_seconds': span(
                (row.get('ready_from_container_start_ms') or 0) / 1000 for row in phase_rows),
            'spring_started_to_pod_ready_seconds': span(
                (row.get('ready_from_spring_start_ms') or 0) / 1000 for row in phase_rows),
            'http_statuses': [row.get('http_status_root') for row in phase_rows],
            'restart_counts': [row.get('restart_count') for row in phase_rows],
            'all_success': all(row.get('success') for row in phase_rows),
        }
    images.append({
        'candidate': candidate,
        'baseline_tag': baseline['tag'],
        'runtime_base': baseline.get('runtime_base'),
        'runtime_base_index_digest': baseline.get('runtime_base_index_digest'),
        'runtime_base_linux_amd64_digest': baseline.get('runtime_base_linux_amd64_digest'),
        'baseline_manifest_digest': baseline['platform_manifest_digest'],
        'compressed_layer_bytes': baseline['compressed_layer_bytes'],
        'baseline_layers': [{'digest': layer['digest'], 'compressed_bytes': layer['size']}
                            for layer in baseline['manifest']['layers']],
        'source_pulses': pulses,
        'phases': phases,
    })

summary = {
    'cluster': 'onebite-cicd',
    'region': 'ap-northeast-2',
    'node_instance_type': 'm7i-flex.large',
    'architecture': 'amd64',
    'source_commit': '8cb156fc22e7111c4bfb02cfae763119cb5985b4',
    'planned_order': [
        {'round': 1, 'candidate': 'b0'}, {'round': 1, 'candidate': 'b1r'}, {'round': 1, 'candidate': 'b2'},
        {'round': 2, 'candidate': 'b1r'}, {'round': 2, 'candidate': 'b2'}, {'round': 2, 'candidate': 'b0'},
        {'round': 3, 'candidate': 'b2'}, {'round': 3, 'candidate': 'b0'}, {'round': 3, 'candidate': 'b1r'},
    ],
    'record_count': len(trials),
    'all_records_successful': len(trials) == 18 and all(row.get('success') for row in trials),
    'image_candidates': images,
}
(RAW / 'eks-delivery-summary.json').write_text(json.dumps(summary, indent=2) + '\n')
print(json.dumps(summary, indent=2))
