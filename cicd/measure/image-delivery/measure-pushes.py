#!/usr/bin/env python3
"""Build and push isolated B0/B1 image variants; never push deployment tags."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


ROOT = Path(os.environ["BACKEND_SOURCE_DIR"])
REPO = "heung115/spaghetti-be"
BUILDER = "onebite-image-delivery"
SOURCE_COMMIT = "8cb156fc22e7111c4bfb02cfae763119cb5985b4"
JAVA_PATH = Path("src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java")
DOCKERFILE_PATH = Path("dockerfile")
OUT = Path(__file__).parent / "raw"
OUT.mkdir(parents=True, exist_ok=True)

B1_DOCKERFILE = b"""# Build and extract the Spring Boot 3.4 layered archive.
FROM gradle:8.5-jdk17 AS builder
WORKDIR /app
COPY . .
RUN chmod +x ./gradlew
RUN ./gradlew clean build -x test
RUN JAR_FILE=$(find /app/build/libs -maxdepth 1 -type f -name '*.jar' ! -name '*-plain.jar' -print -quit) && java -Djarmode=tools -jar "$JAR_FILE" extract --layers --destination /app/extracted

FROM eclipse-temurin:17-jdk
WORKDIR /app
COPY --from=builder /app/extracted/dependencies/ ./
COPY --from=builder /app/extracted/spring-boot-loader/ ./
COPY --from=builder /app/extracted/snapshot-dependencies/ ./
COPY --from=builder /app/extracted/application/ ./
EXPOSE 8080
ENTRYPOINT [\"java\", \"-jar\", \"application.jar\"]
"""


def run(args: list[str], *, cwd: Path = ROOT, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check)


def save_log(name: str, content: str) -> None:
    (OUT / f"{name}.log").write_text(content, encoding="utf-8")


def docker_token() -> str:
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPO}:pull"
    with urllib.request.urlopen(url) as response:
        return json.load(response)["token"]


ACCEPT = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def get_manifest(tag: str) -> tuple[dict, str] | None:
    req = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{REPO}/manifests/{tag}",
        headers={"Authorization": f"Bearer {docker_token()}", "Accept": ACCEPT},
    )
    try:
        with urllib.request.urlopen(req) as response:
            return json.load(response), response.headers.get("Docker-Content-Digest", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def blob_json(digest: str) -> dict:
    req = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{REPO}/blobs/{digest}",
        headers={"Authorization": f"Bearer {docker_token()}"},
    )
    with urllib.request.urlopen(req) as response:
        return json.load(response)


def platform_manifest(doc: dict) -> dict:
    if "layers" in doc:
        return doc
    for descriptor in doc["manifests"]:
        platform = descriptor.get("platform", {})
        if platform.get("os") == "linux" and platform.get("architecture") == "amd64":
            child, _ = get_manifest(descriptor["digest"])
            return child
    raise RuntimeError("Published image has no linux/amd64 manifest")


def layer_statuses(log: str, manifest: dict, config: dict) -> list[dict]:
    statuses = re.findall(r"^([0-9a-f]{8,64}): (Pushed|Layer already exists|Mounted from .+)$", log, re.M)
    diff_ids = config["rootfs"]["diff_ids"]
    histories = [item.get("created_by", "") for item in config.get("history", []) if not item.get("empty_layer", False)]
    layers = manifest["layers"]
    out = []
    for index, descriptor in enumerate(layers):
        layer_digest = descriptor["digest"].removeprefix("sha256:")
        status = next((state for short, state in statuses if layer_digest.startswith(short)), "status-unmatched")
        out.append(
            {
                "index": index,
                "digest": descriptor["digest"],
                "compressed_bytes": descriptor["size"],
                "diff_id": diff_ids[index],
                "push_status": status,
                "history": histories[index] if index < len(histories) else "",
            }
        )
    return out


baseline_source = run(["git", "show", f"{SOURCE_COMMIT}:{JAVA_PATH.as_posix()}"]).stdout.encode()
baseline_dockerfile = run(["git", "show", f"{SOURCE_COMMIT}:{DOCKERFILE_PATH.as_posix()}"]).stdout.encode()
source_file = ROOT / JAVA_PATH
dockerfile_file = ROOT / DOCKERFILE_PATH
original_source = source_file.read_bytes()
original_dockerfile = dockerfile_file.read_bytes()

if original_source != baseline_source or original_dockerfile != baseline_dockerfile:
    raise SystemExit("Temporary checkout is not at the requested clean baseline commit")
if run(["git", "rev-parse", "HEAD"]).stdout.strip() != SOURCE_COMMIT:
    raise SystemExit("Temporary checkout HEAD does not match the baseline source commit")

summary_path = OUT / "push-summary.json"
summary = []


def pulse_source(n: int) -> None:
    marker = b"package code.rice.bowl.spaghetti;\n\n"
    if baseline_source.count(marker) != 1:
        raise RuntimeError("Could not identify the unique source pulse insertion point")
    source_file.write_bytes(baseline_source.replace(marker, marker + f"// Delivery image source pulse {n}.\n\n".encode(), 1))


def measure(tag: str, dockerfile: bytes, pulse: int | None) -> None:
    existing = get_manifest(tag)
    if existing is not None:
        raise RuntimeError(f"Refusing to overwrite existing experiment tag {tag}")
    dockerfile_file.write_bytes(dockerfile)
    if pulse is None:
        source_file.write_bytes(baseline_source)
    else:
        pulse_source(pulse)
    status = run(["git", "status", "--short"]).stdout.splitlines()
    expected = {" M dockerfile"} if dockerfile != baseline_dockerfile else set()
    if pulse is not None:
        expected.add(f" M {JAVA_PATH.as_posix()}")
    if set(status) != expected:
        raise RuntimeError(f"Unexpected temporary checkout changes: {status}")

    key = tag.replace(":", "-")
    archive = Path(f"/tmp/{key}.docker.tar")
    build = [
        "docker", "buildx", "build", "--builder", BUILDER,
        "--platform", "linux/amd64", "--no-cache", "--provenance=false",
        "--progress=plain", "--tag", f"{REPO}:{tag}",
        "--output", f"type=docker,dest={archive}", ".",
    ]
    build_result = run(build, check=False)
    save_log(f"{key}-build", build_result.stdout)
    if build_result.returncode != 0:
        raise RuntimeError(f"Build failed for {tag}: {build_result.stdout[-2000:]}")
    load_result = run(["docker", "load", "--input", str(archive)])
    save_log(f"{key}-load", load_result.stdout)
    archive.unlink(missing_ok=True)

    started = dt.datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
    t0 = time.monotonic()
    push_result = run(["docker", "push", f"{REPO}:{tag}"], check=False)
    elapsed = time.monotonic() - t0
    ended = dt.datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
    save_log(f"{key}-push", push_result.stdout)
    if push_result.returncode != 0:
        raise RuntimeError(f"Docker Hub push failed for {tag}: {push_result.stdout[-1000:]}")

    result = get_manifest(tag)
    if result is None:
        raise RuntimeError(f"Tag {tag} is absent after successful push")
    doc, top_digest = result
    manifest = platform_manifest(doc)
    manifest_digest = top_digest
    if manifest is not doc:
        manifest_digest = next(
            item["digest"] for item in doc["manifests"]
            if item.get("platform", {}).get("os") == "linux"
            and item.get("platform", {}).get("architecture") == "amd64"
        )
    config = blob_json(manifest["config"]["digest"])
    layers = layer_statuses(push_result.stdout, manifest, config)
    record = {
        "tag": tag,
        "condition": "B1-layered-jar" if dockerfile != baseline_dockerfile else "B0-original",
        "source_commit": SOURCE_COMMIT,
        "source_pulse": pulse,
        "source_blob_sha": run(["git", "hash-object", JAVA_PATH.as_posix()]).stdout.strip(),
        "runtime_base": "eclipse-temurin:17-jdk",
        "builder_base": "gradle:8.5-jdk17",
        "builder": BUILDER,
        "remote_cache": False,
        "push_started_at_kst": started,
        "push_completed_at_kst": ended,
        "push_seconds": round(elapsed, 3),
        "docker_push_status": "success",
        "index_digest": top_digest,
        "platform_manifest_digest": manifest_digest,
        "compressed_layer_bytes": sum(item["size"] for item in manifest["layers"]),
        "manifest": doc,
        "layers": layers,
        "push_output": push_result.stdout,
    }
    (OUT / f"{key}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    summary.append({k: record[k] for k in ["tag", "condition", "source_pulse", "push_seconds", "index_digest", "platform_manifest_digest", "compressed_layer_bytes"]})
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary[-1], ensure_ascii=False))


try:
    # B0: original Dockerfile, baseline then three distinct comment-only source pulses.
    if len(sys.argv) == 1 or "b0" in sys.argv[1:]:
        for pulse in [None, 1, 2, 3]:
            tag = "delivery-b0-baseline-local" if pulse is None else f"delivery-b0-pulse-{pulse}"
            measure(tag, baseline_dockerfile, pulse)

    # B1: same source and JDK bases, changing only JAR extraction/runtime layout.
    if len(sys.argv) == 1 or "b1" in sys.argv[1:]:
        for pulse in [None, 1, 2, 3]:
            tag = "delivery-b1-baseline-layered" if pulse is None else f"delivery-b1-pulse-{pulse}"
            measure(tag, B1_DOCKERFILE, pulse)
finally:
    source_file.write_bytes(baseline_source)
    dockerfile_file.write_bytes(baseline_dockerfile)
