#!/usr/bin/env python3
"""Diagnose Docker Hub blob availability and push behavior without image optimization."""
import base64
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO = os.environ["IMAGE_REPO"]
TAG = os.environ["IMAGE_TAG"]
REFERENCE_TAG = os.environ["REFERENCE_TAG"]
PUSH_PATH = os.environ["PUSH_PATH"]
BUILDER = os.environ["BUILDER"]
EXPECTED_DEPS = os.environ["EXPECTED_DEPENDENCIES_DIGEST"]
USERNAME = os.environ["DOCKER_USERNAME"]
PASSWORD = os.environ["DOCKER_PASSWORD"]
SOURCE = Path("src/main/java/code/rice/bowl/spaghetti/SpaghettiApplication.java")
OUT = ROOT / "measure-output" / TAG
OUT.mkdir(parents=True, exist_ok=True)
TOKEN = None
COMMANDS = []
PLATFORM = "linux/amd64"
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json"])


def run(args, *, check=True, cwd=ROOT):
    COMMANDS.append([str(x) for x in args])
    return subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=check)


def run_timed_stream(args):
    """Run Buildx with event timestamps so packaging and registry push can be split."""
    COMMANDS.append([str(x) for x in args])
    started = time.monotonic()
    process = subprocess.Popen(args, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, bufsize=1)
    lines = []
    push_started = None
    exporter_layers_seconds = None
    pushed_layers_seconds = None
    pushed_manifest_seconds = None
    for line in process.stdout:
        elapsed = time.monotonic() - started
        line = line.rstrip("\n")
        lines.append(f"[{elapsed:.3f}s] {line}")
        if push_started is None and re.match(r"^#\d+ pushing layers(?:\s|$)", line):
            push_started = elapsed
        match = re.match(r"^#\d+ exporting layers ([0-9.]+)s done$", line)
        if match:
            exporter_layers_seconds = float(match.group(1))
        match = re.match(r"^#\d+ pushing layers ([0-9.]+)s done$", line)
        if match:
            pushed_layers_seconds = float(match.group(1))
        match = re.match(r"^#\d+ pushing manifest .* ([0-9.]+)s done$", line)
        if match:
            pushed_manifest_seconds = float(match.group(1))
    returncode = process.wait()
    total = time.monotonic() - started
    timing = {"buildx_command_seconds": round(total, 3),
              "buildx_build_and_packaging_until_push_starts_seconds":
                  round(push_started, 3) if push_started is not None else None,
              "buildx_registry_push_section_seconds":
                  round(total - push_started, 3) if push_started is not None else None,
              "buildx_exporter_layers_reported_seconds": exporter_layers_seconds,
              "buildx_push_layers_reported_seconds": pushed_layers_seconds,
              "buildx_push_manifest_reported_seconds": pushed_manifest_seconds}
    return subprocess.CompletedProcess(args, returncode, "\n".join(lines) + "\n"), timing


def registry_token():
    global TOKEN
    if TOKEN:
        return TOKEN
    scope = f"repository:{REPO}:pull"
    url = f"https://auth.docker.io/token?service=registry.docker.io&scope={scope}"
    basic = base64.b64encode(f"{USERNAME}:{PASSWORD}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {basic}"})
    with urllib.request.urlopen(request, timeout=30) as response:
        TOKEN = json.load(response)["token"]
    return TOKEN


def registry_request(path, *, method="GET", accept=None):
    headers = {"Authorization": f"Bearer {registry_token()}"}
    if accept:
        headers["Accept"] = accept
    request = urllib.request.Request(f"https://registry-1.docker.io/v2/{REPO}/{path}",
                                     headers=headers, method=method)
    return urllib.request.urlopen(request, timeout=60)


def inspect_manifest(tag):
    with registry_request(f"manifests/{tag}", accept=ACCEPT) as response:
        top = json.load(response)
        manifest_digest = response.headers.get("Docker-Content-Digest", "")
    if "layers" not in top:
        descriptor = next(item for item in top["manifests"]
                          if item.get("platform", {}).get("os") == "linux"
                          and item.get("platform", {}).get("architecture") == "amd64")
        with registry_request(f"manifests/{descriptor['digest']}", accept=ACCEPT) as response:
            top = json.load(response)
        platform_manifest_digest = descriptor["digest"]
    else:
        platform_manifest_digest = manifest_digest
    with registry_request(f"blobs/{top['config']['digest']}") as response:
        config = json.load(response)
    nonempty_history = [item.get("created_by", "") for item in config.get("history", [])
                        if not item.get("empty_layer")]
    layers = []
    for index, descriptor in enumerate(top["layers"]):
        history = nonempty_history[index] if index < len(nonempty_history) else ""
        label = "other"
        for name in ("dependencies", "spring-boot-loader", "snapshot-dependencies", "application"):
            if f"COPY {name}/ ./" in history:
                label = name
                break
        layers.append({"digest": descriptor["digest"], "compressed_bytes": descriptor["size"],
                       "diff_id": config["rootfs"]["diff_ids"][index], "label": label,
                       "history": history})
    return {"tag": tag, "manifest_digest": manifest_digest,
            "platform_manifest_digest": platform_manifest_digest,
            "compressed_layer_bytes": sum(layer["compressed_bytes"] for layer in layers),
            "layers": layers}


def head_blob(digest):
    request = urllib.request.Request(
        f"https://registry-1.docker.io/v2/{REPO}/blobs/{digest}",
        headers={"Authorization": f"Bearer {registry_token()}"}, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"digest": digest, "http_status": response.status,
                    "content_length": response.headers.get("Content-Length"),
                    "content_digest": response.headers.get("Docker-Content-Digest"),
                    "final_url": response.geturl()}
    except urllib.error.HTTPError as error:
        return {"digest": digest, "http_status": error.code,
                "content_length": error.headers.get("Content-Length"),
                "content_digest": error.headers.get("Docker-Content-Digest"),
                "final_url": error.geturl()}


def head_snapshot(layers):
    unique = list(dict.fromkeys(layer["digest"] for layer in layers))
    return {digest: head_blob(digest) for digest in unique}


def cache_state():
    home = Path(os.environ.get("GRADLE_USER_HOME", str(Path.home() / ".gradle")))
    paths = [home / "caches" / "modules-2" / "files-2.1",
             home / "wrapper" / "dists", home / "caches" / "8.13"]
    entries = []
    for path in paths:
        files = [item for item in path.rglob("*") if item.is_file()] if path.exists() else []
        entries.append({"path": str(path.relative_to(home)), "file_count": len(files),
                        "bytes": sum(item.stat().st_size for item in files)})
    return {"file_count": sum(item["file_count"] for item in entries),
            "bytes": sum(item["bytes"] for item in entries), "paths": entries}


def capture_start(name):
    pcap = OUT / f"{name}-egress.pcap"
    stderr_path = OUT / f"{name}-tcpdump.stderr.log"
    stderr_file = stderr_path.open("w")
    route = run(["ip", "route", "get", "1.1.1.1"], check=False)
    match = re.search(r"\bdev\s+(\S+)", route.stdout)
    if route.returncode or not match:
        stderr_file.close()
        return {"available": False, "error": f"Could not identify default egress interface: {route.stdout}",
                "pcap": str(pcap)}, None, None
    interface = match.group(1)
    command = ["sudo", "tcpdump", "-i", interface, "-Q", "out", "-nn", "-tt", "-q", "-U",
               "-w", str(pcap), "tcp dst port 443"]
    COMMANDS.append(command)
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=stderr_file, cwd=ROOT)
    time.sleep(.4)
    if process.poll() is not None:
        stderr_file.close()
        return {"available": False, "error": stderr_path.read_text(), "pcap": str(pcap)}, None, None
    return {"available": True, "interface": interface, "pcap": str(pcap)}, process, stderr_file


def capture_stop(capture, process, stderr_file):
    if process is None:
        return capture
    process.send_signal(signal.SIGINT)
    process.wait(timeout=15)
    stderr_file.close()
    pcap = Path(capture["pcap"])
    decoded = run(["sudo", "tcpdump", "-nn", "-tt", "-q", "-r", str(pcap)], check=False)
    (OUT / (pcap.stem + "-decoded.log")).write_text(decoded.stdout)
    destination_bytes = {}
    destination_packets = {}
    for line in decoded.stdout.splitlines():
        match = re.search(r">\s+([0-9.]+)\.(\d+): tcp (\d+)\s*$", line)
        if match and match.group(2) == "443":
            destination, _, payload = match.groups()
            payload = int(payload)
            destination_bytes[destination] = destination_bytes.get(destination, 0) + payload
            destination_packets[destination] = destination_packets.get(destination, 0) + bool(payload)
    capture.update({"tcp_packets_with_payload": sum(destination_packets.values()),
                    "outbound_tcp_payload_bytes": sum(destination_bytes.values()),
                    "outbound_tcp_payload_bytes_by_destination": destination_bytes,
                    "payload_packets_by_destination": destination_packets,
                    "pcap_bytes": pcap.stat().st_size,
                    "decode_returncode": decoded.returncode,
                    "tcpdump_stderr": str(OUT / f"{pcap.stem.split('-egress')[0]}-tcpdump.stderr.log")})
    return capture


def cache_restore_log():
    return {"gradle_build_action_cache_restored":
            os.environ.get("GRADLE_BUILD_ACTION_CACHE_RESTORED", "false").lower() == "true",
            "gradle_cache_at_script_start": cache_state()}


def main():
    if PUSH_PATH not in ("load-push", "buildx-push"):
        raise RuntimeError(f"Unsupported push path {PUSH_PATH}")
    ref = inspect_manifest(REFERENCE_TAG)
    if not ref["layers"]:
        raise RuntimeError("Reference manifest has no layers")
    deps = next((layer for layer in ref["layers"] if layer["label"] == "dependencies"), None)
    if not deps or deps["digest"] != EXPECTED_DEPS:
        raise RuntimeError(f"Reference B2 dependency digest mismatch: {deps}")
    previous_app = next((layer for layer in ref["layers"] if layer["label"] == "application"), None)
    prebuild_heads = head_snapshot(ref["layers"])

    source_path = ROOT / SOURCE
    original = source_path.read_bytes()
    marker = b"package code.rice.bowl.spaghetti;\n\n"
    if original.count(marker) != 1:
        raise RuntimeError("Java source pulse insertion point is not unique")
    comments = "".join(f"// CI B2 registry diagnostic source pulse 27, line {line}.\n"
                        for line in range(1, 28))
    pulsed = original.replace(marker, marker + comments.encode() + b"\n", 1)
    source_path.write_bytes(pulsed)
    timings = {}
    docker_log = ""
    wire = {}
    prepush_heads = {}
    post_manifest = None
    post_heads = {}
    bootjar = {}
    bootjar_ready_at = None
    image_context = OUT / "context"
    extracted = OUT / "extracted"
    image_ref = f"{REPO}:{TAG}"
    image_tar = OUT / "image.docker.tar"
    started = time.monotonic()
    try:
        run(["chmod", "+x", "./gradlew"])
        gradle_start = time.monotonic()
        gradle = run(["./gradlew", "clean", "bootJar", "-x", "test"], check=False)
        timings["gradle_bootjar_seconds"] = round(time.monotonic() - gradle_start, 3)
        (OUT / "gradle.log").write_text(gradle.stdout)
        if gradle.returncode:
            raise RuntimeError("Gradle bootJar failed")
        jars = [path for path in (ROOT / "build" / "libs").glob("*.jar")
                if not path.name.endswith("-plain.jar")]
        if len(jars) != 1:
            raise RuntimeError(f"Expected one bootJar artifact, found {jars}")
        bootjar = {"path": str(jars[0].relative_to(ROOT)), "bytes": jars[0].stat().st_size,
                   "sha256": hashlib.sha256(jars[0].read_bytes()).hexdigest()}
        bootjar_ready_at = time.monotonic()

        shutil.rmtree(extracted, ignore_errors=True)
        extracted.mkdir(parents=True)
        extraction_start = time.monotonic()
        result = run(["java", "-Djarmode=tools", "-jar", str(jars[0]), "extract", "--layers",
                      "--destination", str(extracted)], check=False)
        (OUT / "extract.log").write_text(result.stdout)
        if result.returncode:
            raise RuntimeError(f"Layer extraction failed: {result.stdout}")
        run(["find", str(extracted), "-exec", "touch", "-h", "-d", "@315532800", "{}", "+"])
        timings["extraction_and_normalization_seconds"] = round(time.monotonic() - extraction_start, 3)
        context_start = time.monotonic()
        shutil.rmtree(image_context, ignore_errors=True)
        image_context.mkdir(parents=True)
        for layer in ("dependencies", "spring-boot-loader", "snapshot-dependencies", "application"):
            layer_dir = extracted / layer
            if not layer_dir.is_dir():
                raise RuntimeError(f"Missing extracted layer {layer}")
            shutil.copytree(layer_dir, image_context / layer, copy_function=shutil.copy2)
        shutil.copy2(ROOT / "dockerfile.ci-runner-gradle", image_context / "Dockerfile")
        timings["image_context_preparation_seconds"] = round(time.monotonic() - context_start, 3)

        if PUSH_PATH == "load-push":
            package_start = time.monotonic()
            built = run(["docker", "buildx", "build", "--builder", BUILDER, "--platform", PLATFORM,
                         "--no-cache", "--provenance=false", "--progress=plain", "--tag", image_ref,
                         "--file", str(image_context / "Dockerfile"), "--output",
                         f"type=docker,dest={image_tar}", str(image_context)], check=False)
            timings["image_assembly_seconds"] = round(time.monotonic() - package_start, 3)
            (OUT / "image-assembly.log").write_text(built.stdout)
            if built.returncode:
                raise RuntimeError("Buildx image assembly failed")
            load_start = time.monotonic()
            loaded = run(["docker", "load", "--input", str(image_tar)], check=False)
            timings["docker_load_seconds"] = round(time.monotonic() - load_start, 3)
            (OUT / "docker-load.log").write_text(loaded.stdout)
            if loaded.returncode:
                raise RuntimeError("docker load failed")
            image_tar.unlink(missing_ok=True)
            prepush_heads = head_snapshot(ref["layers"])
            wire, cap_process, cap_err = capture_start("docker-push")
            push_start = time.monotonic()
            pushed = run(["docker", "push", image_ref], check=False)
            timings["push_seconds"] = round(time.monotonic() - push_start, 3)
            wire = capture_stop(wire, cap_process, cap_err)
            docker_log = pushed.stdout
            (OUT / "docker-push.log").write_text(docker_log)
            if pushed.returncode:
                raise RuntimeError("docker push failed")
        else:
            prepush_heads = head_snapshot(ref["layers"])
            wire, cap_process, cap_err = capture_start("buildx-push")
            push_start = time.monotonic()
            pushed, buildx_timings = run_timed_stream(
                ["docker", "buildx", "build", "--builder", BUILDER, "--platform", PLATFORM,
                 "--no-cache", "--provenance=false", "--progress=plain", "--tag", image_ref,
                 "--file", str(image_context / "Dockerfile"), "--push", str(image_context)])
            timings.update(buildx_timings)
            timings["build_and_push_seconds"] = round(time.monotonic() - push_start, 3)
            if bootjar_ready_at is not None:
                timings["bootjar_ready_to_registry_push_complete_seconds"] = round(
                    time.monotonic() - bootjar_ready_at, 3)
            wire = capture_stop(wire, cap_process, cap_err)
            docker_log = pushed.stdout
            (OUT / "buildx-push.log").write_text(docker_log)
            if pushed.returncode:
                raise RuntimeError("buildx --push failed")

        post_manifest = inspect_manifest(TAG)
        post_heads = head_snapshot(post_manifest["layers"])
    finally:
        source_path.write_bytes(original)

    statuses = {prefix: status for prefix, status in re.findall(
        r"^([0-9a-f]{8,64}): (Pushed|Layer already exists|Mounted from .+)$", docker_log, re.M)}
    for layer in post_manifest["layers"]:
        layer["docker_cli_status"] = next((status for prefix, status in statuses.items()
                                           if layer["diff_id"].removeprefix("sha256:").startswith(prefix)),
                                          "not-reported-by-cli")
    before_by_digest = {item["digest"]: item for item in ref["layers"]}
    digest_comparison = []
    for index, layer in enumerate(post_manifest["layers"]):
        old = ref["layers"][index] if index < len(ref["layers"]) else None
        digest_comparison.append({"index": index, "label": layer["label"],
                                  "reference_digest": old["digest"] if old else None,
                                  "current_digest": layer["digest"],
                                  "same_digest_at_same_index": bool(old and old["digest"] == layer["digest"]),
                                  "reference_bytes": old["compressed_bytes"] if old else None,
                                  "current_bytes": layer["compressed_bytes"]})
    result = {
        "repository": REPO, "path": PUSH_PATH, "tag": TAG, "reference_tag": REFERENCE_TAG,
        "source_commit": run(["git", "rev-parse", "HEAD"]).stdout.strip(),
        "source_pulse": {"kind": "Java comments only", "lines": 27,
                         "sha256": hashlib.sha256(pulsed).hexdigest()},
        "runner": {"os": os.environ.get("RUNNER_OS"), "arch": os.environ.get("RUNNER_ARCH"),
                   "builder": BUILDER, "driver": "docker-container", "platform": PLATFORM,
                   "remote_buildkit_cache": False},
        "gradle_cache": cache_restore_log(), "bootjar": bootjar,
        "timing_seconds": {**timings, "script_total_including_registry_checks": round(time.monotonic()-started, 3)},
        "reference_manifest": ref, "reference_application_layer": previous_app,
        "registry_head_before_image_build": prebuild_heads,
        "registry_head_immediately_before_push_or_buildx_push": prepush_heads,
        "registry_head_after_push": post_heads, "wire_capture": wire,
        "docker_cli_output_statuses": statuses,
        "current_manifest": post_manifest, "manifest_layer_digest_comparison": digest_comparison,
        "commands": COMMANDS}
    (OUT / "diagnostic-result.json").write_text(json.dumps(result, indent=2) + "\n")
    (OUT / "commands.json").write_text(json.dumps(COMMANDS, indent=2) + "\n")
    (OUT / "docker-cli-push-status.log").write_text(docker_log)
    shutil.rmtree(image_context, ignore_errors=True)
    shutil.rmtree(extracted, ignore_errors=True)
    print(json.dumps({"path": PUSH_PATH, "tag": TAG,
                      "prebuild_head_statuses": {k: v["http_status"] for k, v in prebuild_heads.items()},
                      "prepush_head_statuses": {k: v["http_status"] for k, v in prepush_heads.items()},
                      "postpush_head_statuses": {k: v["http_status"] for k, v in post_heads.items()},
                      "dependency": next(layer for layer in post_manifest["layers"]
                                         if layer["label"] == "dependencies"),
                      "wire_capture": wire, "timing_seconds": result["timing_seconds"]}, indent=2), flush=True)


if __name__ == "__main__":
    main()
