#!/usr/bin/env python3
"""Measure B2 packaging when Gradle runs on the GitHub-hosted runner."""
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "measure-output" / os.environ["IMAGE_TAG_PREFIX"]
REPO = os.environ["IMAGE_REPO"]
TAG_PREFIX = os.environ["IMAGE_TAG_PREFIX"]
BUILDER = os.environ["BUILDER"]
MODE = os.environ["MEASURE_MODE"]
PULSE_INDEX = int(os.environ.get("MEASURE_PULSE_INDEX", "6"))
GRADLE_CACHE_RESTORED = os.environ.get("GRADLE_BUILD_ACTION_CACHE_RESTORED", "false").lower() == "true"
EXPECTED_DEPS = os.environ["EXPECTED_DEPENDENCIES_DIGEST"]
SOURCE = Path("src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java")
PLATFORM = "linux/amd64"
GRADLE_HOME = Path(os.environ.get("GRADLE_USER_HOME", str(Path.home() / ".gradle")))
OUT.mkdir(parents=True, exist_ok=True)


def run(args, *, check=True, cwd=ROOT):
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=check)


def go_seconds(value):
    scales = {"h": 3600, "m": 60, "s": 1, "ms": .001,
              "us": .000001, "µs": .000001, "ns": .000000001}
    return sum(float(n) * scales[u] for n, u in re.findall(
        r"(\d+(?:\.\d+)?)(ms|us|µs|h|m|s)", value))


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


def inspect_registry(tag, push_log):
    with registry_get(f"manifests/{tag}", ACCEPT) as response:
        top = json.load(response)
        manifest_digest = response.headers.get("Docker-Content-Digest", "")
    if "layers" not in top:
        descriptor = next(item for item in top["manifests"]
                          if item.get("platform", {}).get("os") == "linux"
                          and item.get("platform", {}).get("architecture") == "amd64")
        with registry_get(f"manifests/{descriptor['digest']}", ACCEPT) as response:
            top = json.load(response)
        platform_digest = descriptor["digest"]
    else:
        platform_digest = manifest_digest
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
        for name in ("dependencies", "spring-boot-loader", "snapshot-dependencies", "application"):
            if f"COPY {name}/ ./" in created_by:
                label = name
                break
        layers.append({"digest": descriptor["digest"], "diff_id": config["rootfs"]["diff_ids"][index],
                       "compressed_bytes": descriptor["size"], "push_status": status,
                       "label": label, "history": created_by})
    by_label = {name: next((layer for layer in layers if layer["label"] == name), None)
                for name in ("dependencies", "spring-boot-loader", "snapshot-dependencies", "application")}
    if any(layer is None for layer in by_label.values()):
        raise RuntimeError(f"Could not identify all Spring Boot image layers: {by_label}")
    return {"manifest_digest": manifest_digest, "platform_manifest_digest": platform_digest,
            "compressed_layer_bytes": sum(layer["compressed_bytes"] for layer in layers),
            **by_label, "layers": layers}


def cache_state():
    paths = [GRADLE_HOME / "caches" / "modules-2" / "files-2.1",
             GRADLE_HOME / "wrapper" / "dists", GRADLE_HOME / "caches" / "8.13"]
    result = {"gradle_user_home": str(GRADLE_HOME), "paths": {}}
    for path in paths:
        files = [item for item in path.rglob("*") if item.is_file()] if path.exists() else []
        result["paths"][str(path.relative_to(GRADLE_HOME))] = {
            "exists": path.exists(), "file_count": len(files),
            "bytes": sum(item.stat().st_size for item in files)}
    result["file_count"] = sum(value["file_count"] for value in result["paths"].values())
    result["bytes"] = sum(value["bytes"] for value in result["paths"].values())
    return result


def smoke_test(image):
    name = "ci-b2-runner-validation"
    run(["docker", "rm", "-f", name], check=False)
    launched = run(["docker", "run", "-d", "--name", name, "--publish", "127.0.0.1:18082:8080",
                    "--env", "DB_URI=jdbc:postgresql://127.0.0.1:5432/spaghetti",
                    "--env", "DB_USERNAME=smoke", "--env", "DB_PASSWORD=smoke",
                    "--env", "REDIS_HOST=127.0.0.1", "--env", "REDIS_PORT=6379",
                    "--env", "REDIS_PASS=smoke",
                    "--env", "JWT_SECRET=ci-b2-runner-validation-secret-long-enough-32bytes",
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


def selected_jar():
    candidates = sorted((ROOT / "build" / "libs").glob("*.jar"))
    candidates = [path for path in candidates if not path.name.endswith("-plain.jar")]
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one bootJar artifact, found {candidates}")
    return candidates[0]


def measure(label, pulse, baseline_source, smoke=False):
    marker = b"package code.rice.bowl.spaghetti;\n\n"
    if pulse is None:
        current = baseline_source
    else:
        if baseline_source.count(marker) != 1:
            raise RuntimeError("Java source pulse insertion point is not unique")
        comments = "".join(f"// CI B2 source pulse {pulse}, line {line}.\n"
                            for line in range(1, pulse + 1))
        current = baseline_source.replace(marker, marker + comments.encode() + b"\n", 1)
    (ROOT / SOURCE).write_bytes(current)
    tag = f"{TAG_PREFIX}-{label}"
    tagref = f"{REPO}:{tag}"
    run(["chmod", "+x", "./gradlew"])
    start = time.monotonic()
    cache_before = cache_state()

    gradle_start = time.monotonic()
    gradle = run(["./gradlew", "clean", "bootJar", "-x", "test"], check=False)
    gradle_seconds = time.monotonic() - gradle_start
    (OUT / f"{label}-gradle.log").write_text(gradle.stdout)
    if gradle.returncode:
        raise RuntimeError(f"Gradle bootJar failed; inspect {label}-gradle.log")
    jar = selected_jar()
    jar_bytes = jar.stat().st_size
    jar_sha = hashlib.sha256(jar.read_bytes()).hexdigest()

    stage = OUT / f"{label}-stage"
    extracted = stage / "extracted"
    extraction_start = time.monotonic()
    shutil.rmtree(stage, ignore_errors=True)
    extracted.mkdir(parents=True)
    extract = run(["java", "-Djarmode=tools", "-jar", str(jar), "extract", "--layers",
                   "--destination", str(extracted)], check=False)
    if extract.returncode:
        raise RuntimeError(f"Layer extraction failed: {extract.stdout}")
    (OUT / f"{label}-extract.log").write_text(extract.stdout)
    normalize = run(["find", str(extracted), "-exec", "touch", "-h", "-d", "@315532800", "{}", "+"])
    extraction_seconds = time.monotonic() - extraction_start

    package = stage / "context"
    package.mkdir()
    for layer in ("dependencies", "spring-boot-loader", "snapshot-dependencies", "application"):
        source_dir = extracted / layer
        if not source_dir.is_dir():
            raise RuntimeError(f"Spring Boot extraction omitted expected layer {layer}")
        shutil.copytree(source_dir, package / layer, copy_function=shutil.copy2)
    shutil.copy2(ROOT / "dockerfile.ci-runner-gradle", package / "Dockerfile")
    image_tar = OUT / f"{label}.docker.tar"
    package_log = OUT / f"{label}-package.log"
    package_start = time.monotonic()
    built = run(["docker", "buildx", "build", "--builder", BUILDER, "--platform", PLATFORM,
                 "--no-cache", "--provenance=false", "--progress=plain", "--tag", tagref,
                 "--file", str(package / "Dockerfile"), "--output", f"type=docker,dest={image_tar}",
                 str(package)], check=False)
    package_seconds = time.monotonic() - package_start
    package_log.write_text(built.stdout)
    if built.returncode:
        raise RuntimeError(f"Docker image packaging failed; inspect {package_log}")

    load_start = time.monotonic()
    loaded = run(["docker", "load", "--input", str(image_tar)])
    load_seconds = time.monotonic() - load_start
    (OUT / f"{label}-load.log").write_text(loaded.stdout)
    image_tar.unlink(missing_ok=True)

    smoke_result = smoke_test(tagref) if smoke else None
    push_start = time.monotonic()
    pushed = run(["docker", "push", tagref], check=False)
    push_seconds = time.monotonic() - push_start
    (OUT / f"{label}-push.log").write_text(pushed.stdout)
    if pushed.returncode:
        raise RuntimeError(f"Docker Hub push failed; inspect {label}-push.log")
    registry = inspect_registry(tag, pushed.stdout)
    new_blob_bytes = sum(layer["compressed_bytes"] for layer in registry["layers"]
                         if layer["push_status"] == "Pushed")
    docker_build_to_push = gradle_seconds + extraction_seconds + package_seconds + load_seconds + push_seconds
    result = {
        "run_name": label, "tag": tag, "pulse": pulse,
        "source_sha256": hashlib.sha256(current).hexdigest(),
        "source_commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "bootjar_bytes": jar_bytes, "bootjar_sha256": jar_sha,
        "gradle_cache_before": cache_before, "gradle_cache_after": cache_state(),
        "timing_seconds": {
            "gradle_bootjar": round(gradle_seconds, 3),
            "layered_extraction_and_mtime_normalization": round(extraction_seconds, 3),
            "docker_image_packaging": round(package_seconds, 3),
            "docker_load": round(load_seconds, 3),
            "docker_hub_push": round(push_seconds, 3),
            "build_to_push_total": round(docker_build_to_push, 3),
            "pipeline_wall_total": round(time.monotonic() - start, 3)},
            "smoke_validation": smoke_result, "registry": registry,
            "new_blob_bytes": new_blob_bytes,
            "dependency_layer_reused": registry["dependencies"]["push_status"] not in ("Pushed", "unmatched"),
            "application_layer_uploaded": registry["application"]["push_status"] == "Pushed",
            "newly_uploaded_layer_labels": [layer["label"] for layer in registry["layers"]
                                             if layer["push_status"] == "Pushed"],
            "docker_push_output": pushed.stdout}
    (OUT / f"{label}.json").write_text(json.dumps(result, indent=2) + "\n")
    shutil.rmtree(stage, ignore_errors=True)
    print(json.dumps({"tag": tag, "pulse": pulse, "timing_seconds": result["timing_seconds"],
                      "new_blob_bytes": new_blob_bytes, "manifest": registry["manifest_digest"],
                      "dependencies": registry["dependencies"], "application": registry["application"]}),
          flush=True)
    return result


def timing_summary(records):
    keys = ["gradle_bootjar", "layered_extraction_and_mtime_normalization", "docker_image_packaging",
            "docker_load", "docker_hub_push", "build_to_push_total", "pipeline_wall_total"]
    output = {}
    for key in keys:
        values = [item["timing_seconds"][key] for item in records]
        output[key] = {"median": sorted(values)[len(values) // 2], "min": min(values),
                       "max": max(values), "values": values}
    return output


source_path = ROOT / SOURCE
baseline_source = source_path.read_bytes()
started = dt.datetime.now(dt.timezone.utc).isoformat()
records = []
baseline_record = None
cache_state_at_start = None
try:
    if MODE == "warm":
        cache_state_at_start = cache_state()
        if cache_state_at_start["file_count"] == 0 or cache_state_at_start["bytes"] == 0:
            raise RuntimeError("Warm mode started without a restored/populated Gradle User Home cache")
        baseline_record = measure("warm-baseline", None, baseline_source)
        for pulse in range(1, 6):
            records.append(measure(f"pulse-{pulse}", pulse, baseline_source))
    elif MODE == "warm-single":
        cache_state_at_start = cache_state()
        if cache_state_at_start["file_count"] == 0 or cache_state_at_start["bytes"] == 0:
            raise RuntimeError("Warm-single mode started without a restored Gradle User Home cache")
        if not GRADLE_CACHE_RESTORED:
            raise RuntimeError("Warm-single mode did not report a Gradle setup-gradle cache restore")
        if PULSE_INDEX not in range(21, 26):
            raise RuntimeError(f"warm-single pulse must be in 21..25, got {PULSE_INDEX}")
        records.append(measure(f"warm-pulse-{PULSE_INDEX}", PULSE_INDEX, baseline_source))
    elif MODE in ("cold", "seed"):
        records.append(measure(MODE, None, baseline_source, smoke=(MODE == "cold")))
    else:
        raise RuntimeError(f"Unsupported measurement mode {MODE!r}")
except Exception as error:
    (OUT / "failure.json").write_text(json.dumps({"mode": MODE, "error": repr(error)}, indent=2) + "\n")
    raise
finally:
    source_path.write_bytes(baseline_source)

for record in records:
    deps = record["registry"]["dependencies"]
    if deps["digest"] != EXPECTED_DEPS:
        raise RuntimeError(f"Dependencies layer drifted from B2: {deps['digest']} != {EXPECTED_DEPS}")
    labels = [layer["label"] for layer in record["registry"]["layers"]
              if layer["label"] in ("dependencies", "spring-boot-loader", "snapshot-dependencies", "application")]
    if labels != ["dependencies", "spring-boot-loader", "snapshot-dependencies", "application"]:
        raise RuntimeError(f"B2 Spring Boot layer order/shape changed: {labels}")
if MODE == "warm":
    pulse_application_digests = set()
    for record in records:
        dep = record["registry"]["dependencies"]
        app = record["registry"]["application"]
        if dep["push_status"] == "Pushed" or dep["push_status"] == "unmatched":
            raise RuntimeError("Docker Hub did not reuse the B2 dependency layer")
        if app["push_status"] != "Pushed":
            raise RuntimeError("Docker Hub did not upload the source pulse application layer")
        if record["new_blob_bytes"] < 150_000 or record["new_blob_bytes"] > 250_000:
            raise RuntimeError(f"Source pulse new blob is outside the expected B2 application-layer range: {record['new_blob_bytes']}")
        if record["new_blob_bytes"] != app["compressed_bytes"]:
            raise RuntimeError(f"Source pulse uploaded blobs beyond the application layer: {record['new_blob_bytes']} B")
        if any(layer["label"] != "application" and layer["push_status"] == "Pushed"
               for layer in record["registry"]["layers"]):
            raise RuntimeError("Docker Hub uploaded a non-application layer for a source-only pulse")
        if app["digest"] == baseline_record["registry"]["application"]["digest"]:
            raise RuntimeError("Source-only pulse did not change the application layer from the warm baseline")
        pulse_application_digests.add(app["digest"])
    if len(pulse_application_digests) != 5:
        raise RuntimeError("Java comment pulses did not produce five distinct application layers")
elif MODE == "warm-single":
    record = records[0]
    print(json.dumps({"warm_single_layer_transfer_check": {
        "dependency_layer_reused": record["dependency_layer_reused"],
        "application_layer_uploaded": record["application_layer_uploaded"],
        "new_blob_bytes": record["new_blob_bytes"],
        "newly_uploaded_layer_labels": record["newly_uploaded_layer_labels"]}}), flush=True)

result = {"mode": MODE, "source_commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
          "runner_os": os.environ.get("RUNNER_OS"), "runner_arch": os.environ.get("RUNNER_ARCH"),
          "java_version": run(["java", "-version"], check=False).stdout,
          "gradle_user_home": str(GRADLE_HOME), "builder": BUILDER, "platform": PLATFORM,
          "remote_buildkit_cache": False, "started_at_utc": started,
          "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
          "gradle_setup_action_cache_restored": GRADLE_CACHE_RESTORED,
          "gradle_cache_at_warm_job_start": cache_state_at_start,
          "baseline": baseline_record,
          "runs": records, "timing_summary": timing_summary(records) if MODE in ("warm", "warm-single") else None}
(OUT / f"{MODE}-summary.json").write_text(json.dumps(result, indent=2) + "\n")
summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
if summary_path:
    lines = [f"## B2 runner Gradle measurement: {MODE}", "", f"Commit: `{result['source_commit']}`", "",
             f"Gradle User Home cache at start: {(cache_state_at_start or records[0]['gradle_cache_before'])['file_count']} files, "
             f"{(cache_state_at_start or records[0]['gradle_cache_before'])['bytes']} bytes.", ""]
    if result["timing_summary"]:
        lines += ["| Stage | Median | Min | Max |", "|---|---:|---:|---:|"]
        for key, label in (("gradle_bootjar", "Gradle bootJar"),
                           ("layered_extraction_and_mtime_normalization", "Extraction + normalization"),
                           ("docker_image_packaging", "Docker packaging"), ("docker_load", "Docker load"),
                           ("docker_hub_push", "Docker Hub push"), ("build_to_push_total", "Build to push total"),
                           ("pipeline_wall_total", "Pipeline wall total")):
            row = result["timing_summary"][key]
            lines.append(f"| {label} | {row['median']:.3f} | {row['min']:.3f} | {row['max']:.3f} |")
        lines += ["", f"Gradle setup action cache restored: `{GRADLE_CACHE_RESTORED}`.",
                  f"Dependency layer: `{records[0]['registry']['dependencies']['digest']}`.",
                  f"Dependency layer reused: `{records[0]['dependency_layer_reused']}`.",
                  f"New blob bytes: {[item['new_blob_bytes'] for item in records]}."]
    with open(summary_path, "a", encoding="utf-8") as summary_file:
        summary_file.write("\n".join(lines) + "\n")
print(json.dumps({"mode": MODE, "source_commit": result["source_commit"],
                  "runs": [{"tag": record["tag"], "timing_seconds": record["timing_seconds"],
                            "new_blob_bytes": record["new_blob_bytes"],
                            "dependency": record["registry"]["dependencies"],
                            "application": record["registry"]["application"]} for record in records],
                  "timing_summary": result["timing_summary"]}, indent=2))
