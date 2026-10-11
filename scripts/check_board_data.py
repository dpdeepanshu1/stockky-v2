#!/usr/bin/env python3
"""group311: run the Market Board's two live-data checks on the VM and print plain verdicts.

  A  F&O underlyings in the Dhan instrument file   B  open / avg_price in market-data /quotes/bulk (run while the market is open)

Usage:  python3 scripts/check_board_data.py [http://localhost:PORT]     (default: POSITION_STOCKS_URL env or http://localhost:8006)
Read-only. Exit code 0 when both checks are ok, 1 otherwise, 2 when the service cannot be reached."""
import json
import os
import sys
import urllib.request


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else os.getenv("POSITION_STOCKS_URL") or "http://localhost:8006").rstrip("/")
    try:
        with urllib.request.urlopen(base + "/board/self-check", timeout=120) as r:
            out = json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        print(f"cannot reach {base}/board/self-check: {e}")
        return 2
    for key, title in (("A_fno_underlyings", "A  F&O underlyings (Dhan instrument file)"), ("B_open_and_avg_price", "B  open / avg_price (/quotes/bulk)")):
        c = out.get(key) or {}
        print(f"[{'OK ' if c.get('ok') else 'FAIL'}] {title}\n      {c.get('verdict')}")
        if c.get("sample"):
            print("      sample:", json.dumps(c["sample"][:3]))
    print(out.get("note", ""))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
