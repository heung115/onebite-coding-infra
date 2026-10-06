#!/usr/bin/env python3
"""Send a fixed-rate HTTP probe and save one raw CSV file per condition/trial."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def iso_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def request(url: str, timeout: float) -> dict[str, object]:
    started_wall = iso_utc()
    started = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(4096)
            status = response.status
            error = ""
    except urllib.error.HTTPError as exc:
        status = exc.code
        error = f"HTTPError: {exc.reason}"
    except Exception as exc:  # Network failures are raw result data.
        status = ""
        error = f"{type(exc).__name__}: {exc}"
    return {
        "started_at_utc": started_wall,
        "status": status,
        "latency_ms": round((time.monotonic() - started) * 1000, 3),
        "error": error,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--condition", required=True)
    parser.add_argument("--trial", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rps", type=float, default=10.0)
    parser.add_argument("--duration", type=float, default=180.0)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()

    if args.rps <= 0 or args.duration <= 0 or args.workers <= 0:
        parser.error("rps, duration, and workers must be positive")
    if args.output.exists():
        parser.error(f"Refusing to overwrite {args.output}")
    args.output.parent.mkdir(parents=True, exist_ok=True)

    interval = 1.0 / args.rps
    start = time.monotonic()
    deadline = start + args.duration
    next_at = start
    futures: list[tuple[int, concurrent.futures.Future[dict[str, object]]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        request_id = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_at:
                futures.append((request_id, pool.submit(request, args.url, args.timeout)))
                request_id += 1
                next_at += interval
            else:
                time.sleep(min(0.01, next_at - now))

    with args.output.open("w", encoding="utf-8", newline="") as stream:
        fields = ["request_id", "started_at_utc", "status", "latency_ms", "error"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        rows = [(request_id, future.result()) for request_id, future in futures]
        for request_id, result in sorted(rows, key=lambda row: row[0]):
            writer.writerow({"request_id": request_id, **result})

    total = len(futures)
    print(f"condition={args.condition} trial={args.trial} requests={total} output={args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
