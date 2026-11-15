#!/usr/bin/env python3
"""scripts/qc_liquidity_check.py - round 562 (E587): classify the answer of position-stocks GET /board/<symbol> for scripts/vm_quick_check.sh.

Reads the JSON body on stdin and prints ONE line, exit code says what it means:
  0  the `liquidity` key is there (the popup badge is wired); the line lists its fields
  3  the route answered "no bars for <symbol> today" (market closed, weekend, holiday, or the stock is untracked): the key is only added AFTER that check, so nothing can be said yet.
     This is INFO, not a warning: it is the normal answer outside market hours (main.py 404 'no bars for ... today')
  1  anything else (not JSON, an error body, or bars present but no `liquidity` key): a real WARN
Display-only helper: reads nothing but stdin, writes nothing.
"""
from __future__ import annotations

import json
import sys
from typing import Any, Tuple


def classify(raw: str) -> Tuple[int, str]:
    try:
        d: Any = json.loads(raw)
    except (TypeError, ValueError):
        return 1, "not JSON: " + str(raw)[:160]
    if not isinstance(d, dict):
        return 1, "unexpected answer: " + str(d)[:160]
    if "liquidity" in d:
        liq = d.get("liquidity")
        if isinstance(liq, dict):
            return 0, "key present, fields: " + ",".join(sorted(liq))
        return 0, "key present"
    detail = str(d.get("detail", ""))
    if "no bars for" in detail:
        return 3, "no bars today (market closed or stock untracked), so no liquidity key yet; recheck in market hours: " + detail[:100]
    return 1, "no liquidity key: " + str(d)[:160]


def main() -> int:
    code, line = classify(sys.stdin.read())
    print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
