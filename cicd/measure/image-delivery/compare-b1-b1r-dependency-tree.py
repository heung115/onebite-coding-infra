#!/usr/bin/env python3
"""Confirm timestamp normalization preserved dependency content and permissions."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import urllib.request

RAW = Path(__file__).parent / 'raw'
REPO = 'heung115/spaghetti-be'


def token():
    url = f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPO}:pull'
    return json.load(urllib.request.urlopen(url))['token']


def inventory(tag):
    image = json.loads((RAW / f'{tag}.json').read_text())
    layer = next(x for x in image['layers'] if 'COPY /app/extracted/dependencies/' in x['history'])
    url = f"https://registry-1.docker.io/v2/{REPO}/blobs/{layer['digest']}"
    request = urllib.request.Request(url, headers={'Authorization': f'Bearer {token()}'})
    payload = urllib.request.urlopen(request, timeout=120).read()
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
        for member in archive:
            entry = {'type': 'file' if member.isfile() else 'directory' if member.isdir() else 'other',
                     'mode': member.mode, 'uid': member.uid, 'gid': member.gid,
                     'mtime': member.mtime, 'size': member.size}
            if member.isfile():
                entry['sha256'] = hashlib.sha256(archive.extractfile(member).read()).hexdigest()
            entries[member.name] = entry
    return {'digest': layer['digest'], 'compressed_bytes': layer['compressed_bytes'], 'entries': entries}


before = inventory('delivery-b1-baseline-layered')
after = inventory('delivery-b1r-final-baseline')
paths_equal = before['entries'].keys() == after['entries'].keys()
common = before['entries'].keys() & after['entries'].keys()
content_equal = paths_equal and all(
    before['entries'][path]['type'] == after['entries'][path]['type']
    and before['entries'][path]['size'] == after['entries'][path]['size']
    and before['entries'][path].get('sha256') == after['entries'][path].get('sha256')
    for path in common
)
permissions_equal = paths_equal and all(
    (before['entries'][path]['mode'], before['entries'][path]['uid'], before['entries'][path]['gid'])
    == (after['entries'][path]['mode'], after['entries'][path]['uid'], after['entries'][path]['gid'])
    for path in common
)
mtime_changed = [path for path in sorted(common)
                 if before['entries'][path]['mtime'] != after['entries'][path]['mtime']]
result = {
    'comparison': ['delivery-b1-baseline-layered', 'delivery-b1r-final-baseline'],
    'b1': {'digest': before['digest'], 'compressed_bytes': before['compressed_bytes']},
    'b1r': {'digest': after['digest'], 'compressed_bytes': after['compressed_bytes']},
    'entry_count': len(common),
    'same_paths': paths_equal,
    'same_file_content_and_sizes': content_equal,
    'same_permissions_and_ownership': permissions_equal,
    'mtime_changed_count': len(mtime_changed),
    'mtime_after_unique': sorted({after['entries'][path]['mtime'] for path in common}),
    'mtime_changed_paths': mtime_changed,
    'entries': {path: {'b1': before['entries'][path], 'b1r': after['entries'][path]}
                for path in sorted(common)},
}
(RAW / 'b1-b1r-dependency-tree-comparison.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps({key: value for key, value in result.items() if key not in {'entries', 'mtime_changed_paths'}}, indent=2))
