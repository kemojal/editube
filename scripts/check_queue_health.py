#!/usr/bin/env python3
"""
Tiny CLI check for queue health.

Usage:
  python scripts/check_queue_health.py
  python scripts/check_queue_health.py --url http://127.0.0.1:8000/health/queue
"""

from __future__ import annotations

import argparse
import json
import sys
from urllib.error import URLError
from urllib.request import urlopen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000/health/queue",
        help="Queue health endpoint URL",
    )
    args = parser.parse_args()

    try:
        with urlopen(args.url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except URLError as exc:
        print(f"Queue health check failed: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Queue health check failed: {exc}")
        return 2

    status = payload.get("status", "unknown")
    redis_reachable = bool(payload.get("redis_reachable"))
    worker_connected = bool(payload.get("worker_connected"))
    worker_count = int(payload.get("worker_count", 0))
    backlog = int(payload.get("queue_backlog_count", 0))
    error = payload.get("error")

    print(
        f"status={status} redis_reachable={redis_reachable} "
        f"worker_connected={worker_connected} workers={worker_count} backlog={backlog}"
    )
    if error:
        print(f"error={error}")

    if redis_reachable and worker_connected:
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
