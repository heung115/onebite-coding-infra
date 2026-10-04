#!/usr/bin/env python3
"""Compare extracted dependency layer file metadata and bytes from Docker Hub."""

import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent
RAW = ROOT / "raw"
REPOSITORY = "heung115/spaghetti-be"


def token():
    url = (
        "https://auth.docker.io/token?service=registry.docker.io"
        f"&scope=repository:{REPOSITORY}:pull"
    )
    return json.load(urllib.request.urlopen(url))["token"]


def inventory(tag):
    image = json.loads((RAW / f"{tag}.json").read_text())
    descriptor = image["layers"][7]
    url = f"https://registry-1.docker.io/v2/{REPOSITORY}/blobs/{descriptor['digest']}"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token()}"}
    )
    compressed = urllib.request.urlopen(request, timeout=90).read()
    files = {}
    with tarfile.open(fileobj=io.BytesIO(compressed), mode="r:gz") as archive:
        for member in archive:
            if member.isfile():
                payload = archive.extractfile(member).read()
                files[member.name] = {
                    "size": member.size,
                    "mtime": member.mtime,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
    return descriptor, files


before_tag = "delivery-b1-baseline-layered"
after_tag = "delivery-b1-pulse-1"
before_descriptor, before = inventory(before_tag)
after_descriptor, after = inventory(after_tag)
same_paths = before.keys() == after.keys()
same_contents = same_paths and all(
    before[path]["size"] == after[path]["size"]
    and before[path]["sha256"] == after[path]["sha256"]
    for path in before.keys() & after.keys()
)
changed_mtime = sorted(
    path for path in before.keys() & after.keys()
    if before[path]["mtime"] != after[path]["mtime"]
)
result = {
    "comparison": [before_tag, after_tag],
    "layers": {
        before_tag: {
            "digest": before_descriptor["digest"],
            "compressed_bytes": before_descriptor["compressed_bytes"],
        },
        after_tag: {
            "digest": after_descriptor["digest"],
            "compressed_bytes": after_descriptor["compressed_bytes"],
        },
    },
    "files_before": len(before),
    "files_after": len(after),
    "same_paths": same_paths,
    "same_file_sizes_and_sha256": same_contents,
    "changed_mtime_count": len(changed_mtime),
    "changed_mtime_files": changed_mtime,
    "file_inventory": {
        path: {"before": before[path], "after": after[path]}
        for path in sorted(before.keys() & after.keys())
    },
}
(RAW / "b1-dependency-layer-metadata-comparison.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps({key: value for key, value in result.items() if key != "file_inventory"}, indent=2))
