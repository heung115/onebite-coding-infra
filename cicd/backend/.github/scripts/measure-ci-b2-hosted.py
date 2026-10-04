#!/usr/bin/env python3
"""Run validation plus five B2 comment-only build/push pulses on a GitHub runner."""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "measure-output" / os.environ["IMAGE_TAG_PREFIX"]
REPO = os.environ["IMAGE_REPO"]
TAG_PREFIX = os.environ["IMAGE_TAG_PREFIX"]
BUILDER = os.environ.get("BUILDER", "")
PLATFORM = "linux/amd64"
SOURCE = Path("src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java")
OUT.mkdir(parents=True, exist_ok=True)


def run(args, *, check=True, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=check)


def go_seconds(value):
    scales = {"h": 3600, "m": 60, "s": 1, "ms": .001,
              "us": .000001, "µs": .000001, "ns": .000000001}
    return sum(float(n) * scales[u] for n, u in re.findall(
        r"(\d+(?:\.\d+)?)(ms|us|µs|ns|h|m|s)", value))


def vertex_seconds(log, marker):
    lines = log.splitlines()
    for i, line in enumerate(lines):
        if marker not in line:
            continue
        match = re.search(r"#(\d+)", line)
        if match:
            number = match.group(1)
            for done in lines[i + 1:]:
                if re.search(rf"^#{number} DONE ", done):
                    return go_seconds(done.rsplit("DONE", 1)[1].strip())
    return None


def registry_get(path, accept=None):
    scope = f"repository:{REPO}:pull"
    token_url = f"https://auth.docker.io/token?service=registry.docker.io&scope={scope}"
    with urllib.request.urlopen(token_url, timeout=30) as response:
        token = json.load(response)["token"]
    headers = {"Authorization": f"Bearer {token}"}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(f"https://registry-1.docker.io/v2/{REPO}/{path}", headers=headers)
    return urllib.request.urlopen(request, timeout=60)


ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json"])


def manifest_for(ref):
    with registry_get(f"manifests/{ref}", ACCEPT) as response:
        return json.load(response), response.headers.get("Docker-Content-Digest", "")


def smoke_test(image):
    name = "ci-b2-measure-validation"
    run(["docker", "rm", "-f", name], check=False)
    launched = run(["docker", "run", "-d", "--name", name, "--publish", "127.0.0.1:18082:8080",
                    "--env", "DB_URI=jdbc:postgresql://127.0.0.1:5432/spaghetti",
                    "--env", "DB_USERNAME=smoke", "--env", "DB_PASSWORD=smoke",
                    "--env", "REDIS_HOST=127.0.0.1", "--env", "REDIS_PORT=6379",
                    "--env", "REDIS_PASS=smoke",
                    "--env", "JWT_SECRET=ci-b2-measure-validation-secret-long-enough-32bytes",
                    "--env", "AI_SERVER_URL=http://127.0.0.1:9999", "--env", "AI_API_KEY=smoke",
                    "--env", "SPRING_JPA_HIBERNATE_DDL_AUTO=none",
                    "--env", "SPRING_JPA_PROPERTIES_HIBERNATE_BOOT_ALLOW_JDBC_METADATA_ACCESS=false",
                    image], check=False)
    if launched.returncode:
        raise RuntimeError(f"Validation container did not start: {launched.stdout}")
    try:
        started = False
        for _ in range(60):
            time.sleep(1)
            state = run(["docker", "inspect", "--format", "{{.State.Status}}", name], check=False)
            if state.returncode or state.stdout.strip() != "running":
                break
            logs = run(["docker", "logs", name], check=False).stdout
            if "Started SpaghettiApplication" in logs:
                started = True
                break
        logs = run(["docker", "logs", name], check=False).stdout
        http = run(["curl", "--silent", "--show-error", "--output", "/dev/null",
                    "--write-out", "%{http_code}", "--max-time", "5", "http://127.0.0.1:18082/"], check=False)
        result = {"spring_started_log": started, "http_status": http.stdout.strip(),
                  "http_returncode": http.returncode, "container_logs": logs}
        (OUT / "validation-smoke.json").write_text(json.dumps(result, indent=2) + "\n")
        if not started or http.returncode or http.stdout.strip() != "200":
            raise RuntimeError(f"Validation HTTP check failed: {result}")
        return result
    finally:
        run(["docker", "rm", "-f", name], check=False)


def inspect_registry(tag, push_log):
    top, index_digest = manifest_for(tag)
    platform_digest = index_digest
    if "layers" not in top:
        descriptor = next(item for item in top["manifests"]
                          if item.get("platform", {}).get("os") == "linux"
                          and item.get("platform", {}).get("architecture") == "amd64")
        top, _ = manifest_for(descriptor["digest"])
        platform_digest = descriptor["digest"]
    with registry_get(f"blobs/{top['config']['digest']}") as response:
        config = json.load(response)
    statuses = {prefix: status for prefix, status in re.findall(
        r"^([0-9a-f]{8,64}): (Pushed|Layer already exists|Mounted from .+)$", push_log, re.M)}
    history = [entry.get("created_by", "") for entry in config.get("history", [])
               if not entry.get("empty_layer")]
    layers = []
    for index, descriptor in enumerate(top["layers"]):
        diff_id = config["rootfs"]["diff_ids"][index].removeprefix("sha256:")
        status = next((value for prefix, value in statuses.items() if diff_id.startswith(prefix)), "unmatched")
        created_by = history[index] if index < len(history) else ""
        label = "other"
        if "COPY /app/extracted/dependencies/" in created_by:
            label = "dependencies"
        elif "COPY /app/extracted/application/" in created_by:
            label = "application"
        elif "COPY /app/extracted/spring-boot-loader/" in created_by:
            label = "spring-boot-loader"
        elif "COPY /app/extracted/snapshot-dependencies/" in created_by:
            label = "snapshot-dependencies"
        layers.append({"digest": descriptor["digest"], "diff_id": config["rootfs"]["diff_ids"][index],
                       "compressed_bytes": descriptor["size"],
                       "push_status": status, "label": label, "history": created_by})
    deps = next((layer for layer in layers if layer["label"] == "dependencies"), None)
    app = next((layer for layer in layers if layer["label"] == "application"), None)
    if not deps or not app:
        raise RuntimeError("Could not identify dependencies/application image layers")
    return {"manifest_digest": index_digest, "platform_manifest_digest": platform_digest,
            "compressed_layer_bytes": sum(layer["compressed_bytes"] for layer in layers),
            "dependencies": deps, "application": app, "layers": layers}


def measure(pulse, baseline_source):
    marker = b"package code.rice.bowl.spaghetti;\n\n"
    if pulse is None:
        current = baseline_source
        name = "validation"
    else:
        if baseline_source.count(marker) != 1:
            raise RuntimeError("Java source pulse insertion point is not unique")
        current = baseline_source.replace(marker, marker + f"// CI B2 source pulse {pulse}.\n\n".encode(), 1)
        name = f"pulse-{pulse}"
    (ROOT / SOURCE).write_bytes(current)
    tag = f"{TAG_PREFIX}-{name}"
    image_tar = OUT / f"{name}.docker.tar"
    build_log = OUT / f"{name}-build.log"
    push_log = OUT / f"{name}-push.log"
    start = time.monotonic()
    build_start = time.monotonic()
    built = run(["docker", "buildx", "build", "--builder", BUILDER, "--platform", PLATFORM,
                 "--no-cache", "--provenance=false", "--progress=plain", "--tag", f"{REPO}:{tag}",
                 "--output", f"type=docker,dest={image_tar}", "."], check=False)
    build_seconds = time.monotonic() - build_start
    build_log.write_text(built.stdout)
    if built.returncode:
        raise RuntimeError(f"Build failed; inspect {build_log}")
    bootjar_seconds = vertex_seconds(built.stdout, "RUN ./gradlew clean bootJar -x test")
    extraction_seconds = vertex_seconds(built.stdout, "extract --layers --destination /app/extracted")
    if bootjar_seconds is None or extraction_seconds is None:
        raise RuntimeError("Could not parse the Gradle/extraction BuildKit vertices")
    load_start = time.monotonic()
    loaded = run(["docker", "load", "--input", str(image_tar)])
    load_seconds = time.monotonic() - load_start
    (OUT / f"{name}-load.log").write_text(loaded.stdout)
    image_tar.unlink(missing_ok=True)
    smoke = smoke_test(f"{REPO}:{tag}") if pulse is None else None
    push_start = time.monotonic()
    pushed = run(["docker", "push", f"{REPO}:{tag}"], check=False)
    push_seconds = time.monotonic() - push_start
    push_log.write_text(pushed.stdout)
    if pushed.returncode:
        raise RuntimeError(f"Push failed; inspect {push_log}")
    registry = inspect_registry(tag, pushed.stdout)
    status_by_digest = {layer["digest"]: layer["push_status"] for layer in registry["layers"]}
    new_blob_bytes = sum(layer["compressed_bytes"] for layer in registry["layers"]
                         if layer["push_status"] == "Pushed")
    build_context_assembly = max(0, build_seconds - bootjar_seconds - extraction_seconds)
    record = {
        "run_name": name, "tag": tag, "pulse": pulse,
        "source_sha256": hashlib.sha256(current).hexdigest(),
        "source_commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "timing_seconds": {
            "bootjar_vertex": round(bootjar_seconds, 3),
            "layered_extraction_and_mtime_normalization_vertex": round(extraction_seconds, 3),
            "image_assembly_residual": round(build_context_assembly, 3),
            "buildx_build_total": round(build_seconds, 3),
            "docker_load": round(load_seconds, 3),
            "docker_hub_push": round(push_seconds, 3),
            "build_and_push_total": round(build_seconds + load_seconds + push_seconds, 3),
            "pipeline_wall_total": round(time.monotonic() - start, 3)},
        "smoke_validation": smoke, "registry": registry, "new_blob_bytes": new_blob_bytes,
        "docker_push_output": pushed.stdout}
    (OUT / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    print(json.dumps({"tag": tag, "pulse": pulse, "timing_seconds": record["timing_seconds"],
                      "new_blob_bytes": new_blob_bytes,
                      "dependencies": registry["dependencies"],
                      "application": registry["application"],
                      "image_manifest_digest": registry["manifest_digest"]}), flush=True)
    return record


def percentile_summary(records):
    keys = ["bootjar_vertex", "layered_extraction_and_mtime_normalization_vertex",
            "image_assembly_residual", "buildx_build_total", "docker_load",
            "docker_hub_push", "build_and_push_total", "pipeline_wall_total"]
    pulses = [record for record in records if record["pulse"] is not None]
    summary = {}
    for key in keys:
        values = [record["timing_seconds"][key] for record in pulses]
        summary[key] = {"median": sorted(values)[len(values) // 2], "min": min(values), "max": max(values),
                        "values": values}
    return summary


source_path = ROOT / SOURCE
baseline_source = source_path.read_bytes()
start = dt.datetime.now(dt.timezone.utc).isoformat()
records = []
try:
    records.append(measure(None, baseline_source))
    for pulse in range(1, 6):
        records.append(measure(pulse, baseline_source))
finally:
    source_path.write_bytes(baseline_source)

for record in records[1:]:
    if record["registry"]["dependencies"]["digest"] != records[0]["registry"]["dependencies"]["digest"]:
        raise RuntimeError("Dependency layer digest changed across source-only pulse")
    dependency_push_status = record["registry"]["dependencies"]["push_status"]
    if dependency_push_status == "Pushed" or dependency_push_status == "unmatched":
        raise RuntimeError("Docker Hub did not reuse the dependency layer")
    if record["registry"]["application"]["push_status"] != "Pushed":
        raise RuntimeError("Docker Hub did not upload the application layer")

result = {"source_commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
          "runner_os": os.environ.get("RUNNER_OS"), "runner_arch": os.environ.get("RUNNER_ARCH"),
          "builder": BUILDER, "platform": PLATFORM, "remote_buildkit_cache": False,
          "started_at_utc": start, "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
          "validation": records[0], "pulses": records[1:], "pulse_timing_summary": percentile_summary(records)}
(OUT / "summary.json").write_text(json.dumps(result, indent=2) + "\n")

summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
if summary_path:
    lines = ["## B2 hosted CI measurement", "", f"Commit: `{result['source_commit']}`", "",
             "Validation run is excluded below. Pulse values are seconds, n=5.", "",
             "| Stage | Median | Min | Max |", "|---|---:|---:|---:|"]
    display = [("bootjar_vertex", "Gradle bootJar"),
               ("layered_extraction_and_mtime_normalization_vertex", "Layer extraction + mtime"),
               ("image_assembly_residual", "Image assembly residual"),
               ("buildx_build_total", "Buildx build total"), ("docker_load", "Docker load"),
               ("docker_hub_push", "Docker Hub push"),
               ("build_and_push_total", "Build + push total"),
               ("pipeline_wall_total", "Pipeline wall total")]
    for key, label in display:
        stat = result["pulse_timing_summary"][key]
        lines.append(f"| {label} | {stat['median']:.3f} | {stat['min']:.3f} | {stat['max']:.3f} |")
    lines += ["", f"Dependency layer: `{records[0]['registry']['dependencies']['digest']}` "
              f"({records[0]['registry']['dependencies']['compressed_bytes']} B).",
              f"Source pulse new blob bytes: {[record['new_blob_bytes'] for record in records[1:]]}."]
    with open(summary_path, "a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")
print(json.dumps({"tag_prefix": TAG_PREFIX, "source_commit": result["source_commit"],
                  "pulse_timing_summary": result["pulse_timing_summary"],
                  "dependency_layer": records[0]["registry"]["dependencies"],
                  "new_blob_bytes": [record["new_blob_bytes"] for record in records[1:]]}, indent=2))
