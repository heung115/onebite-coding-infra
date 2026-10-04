#!/usr/bin/env python3
"""Measure five source-only B2 build/push runs against the current backend checkout."""
import datetime as dt
import json
import re
import subprocess
import time
import urllib.request
import os
from pathlib import Path
from zoneinfo import ZoneInfo

BACKEND = Path(os.environ["BACKEND_SOURCE_DIR"])
OUT = Path(__file__).resolve().parent / "raw" / "ci-b2-baseline-20261003"
REPO = "heung115/spaghetti-be"
BUILDER = "onebite-image-delivery"
PLATFORM = "linux/amd64"
SOURCE = Path("src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java")
KST = ZoneInfo("Asia/Seoul")
TAG_PREFIX = "ci-b2-20261003-" + dt.datetime.now(KST).strftime("%H%M%S")
OUT.mkdir(parents=True, exist_ok=True)


def run(args, *, check=True, cwd=BACKEND):
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=check)


def now():
    return dt.datetime.now(KST).isoformat(timespec="seconds")


def go_seconds(value):
    total = 0.0
    for number, unit in re.findall(r"(\d+(?:\.\d+)?)(h|m|s|ms|us|µs|ns)", value):
        total += float(number) * {"h": 3600, "m": 60, "s": 1, "ms": .001,
                                  "us": .000001, "µs": .000001, "ns": .000000001}[unit]
    return total


def vertex_seconds(log, marker):
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if marker in line:
            m = re.search(r"#(\d+)", line)
            if not m:
                continue
            vertex = m.group(1)
            for done in lines[i + 1:]:
                if re.search(rf"^#{vertex} DONE ", done):
                    return go_seconds(done.rsplit("DONE", 1)[1].strip())
    return None


def registry_token():
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPO}:pull"
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)["token"]


def manifest(tag):
    token = registry_token()
    request = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{REPO}/manifests/{tag}",
        headers={"Authorization": f"Bearer {token}", "Accept": ", ".join([
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json"])})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response), response.headers.get("Docker-Content-Digest", "")


def push_statuses(output):
    return {digest: status for digest, status in re.findall(
        r"^([0-9a-f]{8,64}): (Pushed|Layer already exists|Mounted from .+)$", output, re.M)}


def smoke(image):
    name = "ci-b2-baseline-smoke"
    run(["docker", "rm", "-f", name], check=False)
    launch = run(["docker", "run", "-d", "--name", name,
                  "--publish", "127.0.0.1:18082:8080",
                  "--env", "DB_URI=jdbc:postgresql://127.0.0.1:5432/spaghetti",
                  "--env", "DB_USERNAME=smoke", "--env", "DB_PASSWORD=smoke",
                  "--env", "REDIS_HOST=127.0.0.1", "--env", "REDIS_PORT=6379",
                  "--env", "REDIS_PASS=smoke",
                  "--env", "JWT_SECRET=ci-b2-baseline-smoke-secret-long-enough-32bytes",
                  "--env", "AI_SERVER_URL=http://127.0.0.1:9999", "--env", "AI_API_KEY=smoke",
                  "--env", "SPRING_JPA_HIBERNATE_DDL_AUTO=none",
                  "--env", "SPRING_JPA_PROPERTIES_HIBERNATE_BOOT_ALLOW_JDBC_METADATA_ACCESS=false",
                  image], check=False)
    result = {"launch_returncode": launch.returncode, "launch_output": launch.stdout}
    if launch.returncode:
        raise RuntimeError(f"Container failed to start: {launch.stdout}")
    try:
        started = False
        status = None
        for _ in range(60):
            time.sleep(1)
            status = run(["docker", "inspect", "--format", "{{.State.Status}}", name], check=False)
            if status.returncode or status.stdout.strip() != "running":
                break
            logs = run(["docker", "logs", name], check=False).stdout
            if "Started SpaghettiApplication" in logs:
                started = True
                break
        logs = run(["docker", "logs", name], check=False).stdout
        http = run(["curl", "--silent", "--show-error", "--output", "/dev/null",
                    "--write-out", "%{http_code}", "--max-time", "5", "http://127.0.0.1:18082/"], check=False)
        result.update({"state": status.stdout.strip() if status else None,
                       "spring_started_log": started, "http_status": http.stdout.strip(),
                       "http_returncode": http.returncode, "logs": logs})
        if not started or http.returncode or http.stdout.strip() != "200":
            raise RuntimeError(f"Smoke validation failed: {result}")
    finally:
        run(["docker", "rm", "-f", name], check=False)
    return result


def measure(pulse, original_source):
    if pulse is None:
        tag = f"{TAG_PREFIX}-baseline"
        current = original_source
    else:
        tag = f"{TAG_PREFIX}-pulse-{pulse}"
        marker = b"package code.rice.bowl.spaghetti;\n\n"
        if original_source.count(marker) != 1:
            raise RuntimeError("Java source pulse insertion point is not unique")
        current = original_source.replace(marker, marker +
            f"// CI image source pulse {pulse}.\n\n".encode(), 1)
    (BACKEND / SOURCE).write_bytes(current)
    key = tag
    tar = OUT / f"{key}.docker.tar"
    build_started = time.monotonic()
    build = run(["docker", "buildx", "build", "--builder", BUILDER,
                 "--platform", PLATFORM, "--no-cache", "--provenance=false",
                 "--progress=plain", "--tag", f"{REPO}:{tag}",
                 "--output", f"type=docker,dest={tar}", "."], check=False)
    build_seconds = time.monotonic() - build_started
    (OUT / f"{key}-build.log").write_text(build.stdout)
    if build.returncode:
        raise RuntimeError(f"Build failed for {tag}; inspect {key}-build.log")

    gradle_seconds = vertex_seconds(build.stdout, "RUN ./gradlew clean bootJar -x test")
    extraction_seconds = vertex_seconds(build.stdout, "extract --layers --destination /app/extracted")
    if gradle_seconds is None or extraction_seconds is None:
        raise RuntimeError(f"Could not isolate BuildKit stage timings for {tag}")

    loaded = run(["docker", "load", "--input", str(tar)])
    (OUT / f"{key}-load.log").write_text(loaded.stdout)
    tar.unlink(missing_ok=True)
    if pulse is None:
        smoke_result = smoke(f"{REPO}:{tag}")
        (OUT / f"{key}-smoke.json").write_text(json.dumps(smoke_result, indent=2) + "\n")
    else:
        smoke_result = None

    push_started_at = now()
    push_t0 = time.monotonic()
    pushed = run(["docker", "push", f"{REPO}:{tag}"], check=False)
    push_seconds = time.monotonic() - push_t0
    push_completed_at = now()
    (OUT / f"{key}-push.log").write_text(pushed.stdout)
    if pushed.returncode:
        raise RuntimeError(f"Push failed for {tag}; inspect {key}-push.log")
    top, digest = manifest(tag)
    if "layers" not in top:
        top = next((manifest(child["digest"])[0] for child in top["manifests"]
                    if child.get("platform", {}).get("os") == "linux"
                    and child.get("platform", {}).get("architecture") == "amd64"), None)
    statuses = push_statuses(pushed.stdout)
    layers = []
    for layer in top["layers"]:
        hex_digest = layer["digest"].removeprefix("sha256:")
        status = next((value for prefix, value in statuses.items() if hex_digest.startswith(prefix)), "unmatched")
        layers.append({"digest": layer["digest"], "compressed_bytes": layer["size"], "push_status": status})
    new_blob_bytes = sum(layer["compressed_bytes"] for layer in layers if layer["push_status"] == "Pushed")
    record = {
        "tag": tag, "pulse": pulse, "source_file": str(SOURCE),
        "source_sha256": __import__("hashlib").sha256(current).hexdigest(),
        "builder": BUILDER, "platform": PLATFORM, "remote_cache": False, "build_no_cache": True,
        "runtime": "eclipse-temurin:17-jre", "push_started_at_kst": push_started_at,
        "push_completed_at_kst": push_completed_at,
        "timing_seconds": {
            "gradle_bootjar_vertex": round(gradle_seconds, 3),
            "layered_extraction_and_mtime_normalization_vertex": round(extraction_seconds, 3),
            "docker_image_assembly_residual": round(max(0, build_seconds - gradle_seconds - extraction_seconds), 3),
            "buildx_build_total": round(build_seconds, 3),
            "docker_hub_push": round(push_seconds, 3),
            "build_and_push_total": round(build_seconds + push_seconds, 3)},
        "image_manifest_digest": digest,
        "compressed_layer_bytes": sum(layer["compressed_bytes"] for layer in layers),
        "new_blob_bytes": new_blob_bytes, "layers": layers,
        "docker_push_output": pushed.stdout, "smoke_validation": smoke_result}
    (OUT / f"{key}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"tag": tag, "pulse": pulse,
                      "timing_seconds": record["timing_seconds"],
                      "new_blob_bytes": new_blob_bytes,
                      "uploaded": [x for x in layers if x["push_status"] == "Pushed"],
                      "image_manifest_digest": digest}), flush=True)
    return record


source_path = BACKEND / SOURCE
original_source = source_path.read_bytes()
start = now()
records = []
try:
    records.append(measure(None, original_source))
    for pulse in range(1, 6):
        records.append(measure(pulse, original_source))
finally:
    source_path.write_bytes(original_source)

summary = {"backend": str(BACKEND), "source_commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
           "source_start_sha256": __import__("hashlib").sha256(original_source).hexdigest(),
           "started_at_kst": start, "completed_at_kst": now(), "records": records}
(OUT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
print(f"Summary: {OUT / 'summary.json'}", flush=True)
