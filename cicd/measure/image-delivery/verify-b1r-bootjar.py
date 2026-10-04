#!/usr/bin/env python3
"""Determine and verify Spring Boot archive timestamp reproducibility."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import zipfile

ROOT = Path(os.environ["BACKEND_SOURCE_DIR"])
OUT = Path(__file__).parent / "raw"
SOURCE = Path("src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java")
BASELINE_COMMIT = "8cb156fc22e7111c4bfb02cfae763119cb5985b4"
BASELINE_SOURCE = subprocess.check_output(
    ["git", "show", f"{BASELINE_COMMIT}:{SOURCE.as_posix()}"], cwd=ROOT
)
BASELINE_BUILD = subprocess.check_output(
    ["git", "show", f"{BASELINE_COMMIT}:build.gradle"], cwd=ROOT
)
CONFIG = b"""
import org.gradle.api.tasks.bundling.AbstractArchiveTask

tasks.withType(AbstractArchiveTask).configureEach {
    preserveFileTimestamps = false
    reproducibleFileOrder = true
}
"""
PULSE_SOURCE = BASELINE_SOURCE.replace(
    b"package code.rice.bowl.spaghetti;\n\n",
    b"package code.rice.bowl.spaghetti;\n\n// Delivery image source pulse 1.\n\n",
    1,
)


def archive_inventory(path: Path) -> dict:
    with zipfile.ZipFile(path) as jar:
        dependencies = {}
        for item in jar.infolist():
            if item.filename.startswith("BOOT-INF/lib/") and item.filename.endswith(".jar"):
                dependencies[item.filename] = {
                    "timestamp": list(item.date_time),
                    "size": item.file_size,
                    "compressed_size": item.compress_size,
                    "crc32": f"{item.CRC:08x}",
                }
    return {
        "bootjar": path.name,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "dependency_entry_count": len(dependencies),
        "dependencies": dependencies,
    }


def build(name: str, *, config: bytes, source: bytes) -> dict:
    (ROOT / "build.gradle").write_bytes(config)
    (ROOT / SOURCE).write_bytes(source)
    proc = subprocess.run(
        ["bash", "./gradlew", "--no-daemon", "clean", "bootJar"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    (OUT / f"{name}-build.log").write_text(proc.stdout)
    if proc.returncode:
        raise RuntimeError(f"{name} failed; see {name}-build.log")
    artifacts = list((ROOT / "build/libs").glob("*.jar"))
    artifacts = [item for item in artifacts if not item.name.endswith("-plain.jar")]
    if len(artifacts) != 1:
        raise RuntimeError(f"Expected one bootJar, found {artifacts}")
    target = OUT / f"{name}.jar"
    target.write_bytes(artifacts[0].read_bytes())
    return archive_inventory(target)


try:
    records = {}
    for label in ["original-baseline-a", "original-baseline-b"]:
        records[label] = build(label, config=BASELINE_BUILD, source=BASELINE_SOURCE)
    records["original-pulse-1"] = build(
        "original-pulse-1", config=BASELINE_BUILD, source=PULSE_SOURCE
    )
    for label in ["repro-baseline-a", "repro-baseline-b"]:
        records[label] = build(label, config=BASELINE_BUILD + CONFIG, source=BASELINE_SOURCE)
    records["repro-pulse-1"] = build(
        "repro-pulse-1", config=BASELINE_BUILD + CONFIG, source=PULSE_SOURCE
    )
finally:
    (ROOT / "build.gradle").write_bytes(BASELINE_BUILD)
    (ROOT / SOURCE).write_bytes(BASELINE_SOURCE)

result = {
    "source_commit": BASELINE_COMMIT,
    "configuration": CONFIG.decode().strip(),
    "runs": records,
    "original_same_source_bootjar_hash_equal": (
        records["original-baseline-a"]["sha256"]
        == records["original-baseline-b"]["sha256"]
    ),
    "original_dependency_entry_metadata_equal_baseline_to_pulse": (
        records["original-baseline-a"]["dependencies"]
        == records["original-pulse-1"]["dependencies"]
    ),
    "repro_same_source_bootjar_hash_equal": (
        records["repro-baseline-a"]["sha256"]
        == records["repro-baseline-b"]["sha256"]
    ),
    "repro_dependency_entry_metadata_equal_baseline_to_pulse": (
        records["repro-baseline-a"]["dependencies"]
        == records["repro-pulse-1"]["dependencies"]
    ),
}
(OUT / "b1r-bootjar-reproducibility.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps({key: value for key, value in result.items() if key != "runs"}, indent=2))
for label, record in records.items():
    print(
        f"{label}: bootJar sha256={record['sha256']} bytes={record['bytes']} "
        f"dependency_entries={record['dependency_entry_count']}"
    )
