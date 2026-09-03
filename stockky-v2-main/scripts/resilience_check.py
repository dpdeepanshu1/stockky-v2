#!/usr/bin/env python3
"""
Stockky resilience checks (free-tier friendly).

Usage:
  export API_GATEWAY_URL=https://your-api-gateway.onrender.com
  python scripts/resilience_check.py

What it covers:
  1. Health / wake-all latency
  2. Circuit breaker snapshots
  3. Metrics endpoint
  4. Ops alert dry evaluation (POST /ops/check-alert)
  5. Optional: decide endpoint timeout behaviour

No extra packages beyond stdlib + urllib.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


def get(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, method="GET")
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body, (time.time() - t0) * 1000
    except Exception as e:
        return None, str(e), (time.time() - t0) * 1000


def post(url: str, timeout: float = 30.0):
    req = urllib.request.Request(url, method="POST", data=b"{}", headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, body, (time.time() - t0) * 1000
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, body, (time.time() - t0) * 1000
    except Exception as e:
        return None, str(e), (time.time() - t0) * 1000


def main():
    base = (os.environ.get("API_GATEWAY_URL") or "").rstrip("/")
    if not base:
        print("Set API_GATEWAY_URL", file=sys.stderr)
        sys.exit(1)

    print(f"Target: {base}\n")

    # 1. Health
    status, body, ms = get(f"{base}/health", timeout=25)
    print(f"[health] status={status} latency_ms={ms:.0f}")
    if status != 200:
        print("  FAIL health")
    else:
        print("  OK")

    # 2. Circuits
    status, body, ms = get(f"{base}/circuits", timeout=20)
    print(f"[circuits] status={status} latency_ms={ms:.0f}")
    open_list = []
    if status == 200:
        try:
            data = json.loads(body)
            for name, snap in (data.get("circuits") or {}).items():
                st = snap.get("state")
                print(f"  - {name}: {st}")
                if st == "open":
                    open_list.append(name)
        except Exception as e:
            print(f"  parse error: {e}")
    else:
        print(f"  body={body[:200]}")

    # 3. Metrics
    status, body, ms = get(f"{base}/metrics", timeout=20)
    print(f"[metrics] status={status} latency_ms={ms:.0f}")
    if status == 200:
        try:
            snap = json.loads(body)
            print(f"  uptime_sec={snap.get('uptime_sec')} counters={len(snap.get('counters') or {})}")
        except Exception:
            print(f"  raw={body[:120]}")

    # Prometheus text sample
    status, body, ms = get(f"{base}/metrics?format=prom", timeout=20)
    print(f"[metrics prom] status={status} lines={len(body.splitlines()) if body else 0}")

    # 4. Ops alert
    status, body, ms = post(f"{base}/ops/check-alert", timeout=25)
    print(f"[ops/check-alert] status={status} latency_ms={ms:.0f}")
    if status == 200:
        try:
            print(f"  {json.loads(body)}")
        except Exception:
            print(f"  {body[:200]}")

    # 5. Optional decide smoke (symbol RELIANCE)
    if os.environ.get("RESILIENCE_DECIDE", "0") == "1":
        status, body, ms = get(f"{base}/decide/RELIANCE", timeout=90)
        print(f"[decide RELIANCE] status={status} latency_ms={ms:.0f}")
        if status == 200:
            try:
                d = json.loads(body)
                print(f"  decision={d.get('decision')} score={d.get('combined_score')} dq={d.get('data_quality')}")
            except Exception:
                pass

    print("\nDone.")
    if open_list:
        print(f"WARNING: open circuits: {open_list}")
        sys.exit(2)


if __name__ == "__main__":
    main()
