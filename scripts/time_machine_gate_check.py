#!/usr/bin/env python3
"""time_machine_gate_check.py - E115: is the Time Machine live gate running in both trading services, and is it behaving? (places nothing, changes no switch)

  TOKEN=<admin session token> python3 scripts/time_machine_gate_check.py [--rt-url http://127.0.0.1:8004] [--ps-url http://127.0.0.1:8006]
                                                                           [--scan] [--wait] [--expect-mode live] [--max-refusal-rate 0.5] [--min-checks 10]

Reads GET /time-machine/gate on real-trade and position-stocks: the mode, the worker + timer threads, the counters and the refusal rate. With --scan it first
POSTs /time-machine/scan (reason "runner"): the same trigger the timer and the Market Weather 09:14 lock use; --wait makes the service compute the verdicts before answering.
Verdict line: `TM GATE: PASS | FAIL - <reason> | NOT_ENOUGH_DATA - <progress>`.
  FAIL  = a service did not answer, is not in the expected mode, has no running threads, or refuses more than --max-refusal-rate of at least --min-checks checks
          (then look at recent_checks and consider TM_GATE_MODE=log).
  NOT_ENOUGH_DATA = wired and running, fewer than --min-checks entry checks so far today.
Exit 0 = PASS or NOT_ENOUGH_DATA, 1 = FAIL, 2 = bad usage. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Tuple

Call = Callable[[str, str], Tuple[int, Any]]      # (method, url) -> (status, json)


def http_call(token: str, timeout: float = 60.0) -> Call:
    def call(method: str, url: str) -> Tuple[int, Any]:
        h = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            req = urllib.request.Request(url, method=method, headers=h, data=(b"" if method == "POST" else None))
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
                return r.status, json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:  # noqa: BLE001
            return 0, None
    return call


def check_service(name: str, base: str, call: Call, scan: bool, wait: bool, expect_mode: str, max_rate: float, min_checks: int) -> Tuple[List[str], List[str], List[str]]:
    """(lines, failures, progress). Pure apart from the injected call."""
    lines: List[str] = []
    fails: List[str] = []
    prog: List[str] = []
    base = base.rstrip("/")
    if scan:
        st, body = call("POST", f"{base}/time-machine/scan?reason=runner&wait={'true' if wait else 'false'}")
        if st == 200 and isinstance(body, dict):
            lines.append(f"{name}: scan ok={body.get('ok')} targets={body.get('targets')} queued={body.get('queued')} ran={body.get('ran')}")
        else:
            lines.append(f"{name}: scan answered HTTP {st} (needs the admin token)")
    st, g = call("GET", f"{base}/time-machine/gate")
    if st != 200 or not isinstance(g, dict):
        fails.append(f"{name}: GET /time-machine/gate answered HTTP {st}")
        lines.append(fails[-1])
        return lines, fails, prog
    mode = g.get("mode")
    timer = g.get("timer") or {}
    c = g.get("counters") or {}
    checks = int(c.get("gate_checks") or 0)
    rate = g.get("refusal_rate")
    lines.append(f"{name}: mode={mode} threads_running={timer.get('threads_running')} timer_on={g.get('auto_enabled')} checks={checks} refused={c.get('rejects', 0)} "
                 f"no_opinion={c.get('no_opinion', 0)} cached={g.get('cached_verdicts')} refusal_rate={rate}")
    if expect_mode != "any" and mode != expect_mode:
        fails.append(f"{name}: mode is {mode}, expected {expect_mode}")
    if mode != "off" and not timer.get("threads_running"):
        fails.append(f"{name}: the worker / timer threads are not running")
    if checks >= min_checks and isinstance(rate, (int, float)) and rate > max_rate:
        fails.append(f"{name}: refuses {rate * 100:.0f}% of {checks} checks (limit {max_rate * 100:.0f}%): read recent_checks, consider TM_GATE_MODE=log")
    if checks < min_checks:
        prog.append(f"{name} {checks}/{min_checks} checks")
    return lines, fails, prog


def run(rt: str, ps: str, call: Call, scan: bool = False, wait: bool = False, expect_mode: str = "live", max_rate: float = 0.5, min_checks: int = 10) -> Tuple[List[str], int]:
    out: List[str] = []
    fails: List[str] = []
    prog: List[str] = []
    for name, base in (("real-trade", rt), ("position-stocks", ps)):
        l, f, p = check_service(name, base, call, scan, wait, expect_mode, max_rate, min_checks)
        out += l
        fails += f
        prog += p
    if fails:
        out.append(f"TM GATE: FAIL - {fails[0]}")
        return out, 1
    if prog:
        out.append("TM GATE: NOT_ENOUGH_DATA - wired and running; " + ", ".join(prog))
        return out, 0
    out.append("TM GATE: PASS - both services run the gate and the refusal rate is within the limit")
    return out, 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rt-url", default=os.environ.get("RT_URL", "http://127.0.0.1:8004"))
    ap.add_argument("--ps-url", default=os.environ.get("PS_URL", "http://127.0.0.1:8006"))
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--wait", action="store_true")
    ap.add_argument("--expect-mode", default="live", choices=("live", "log", "off", "any"))
    ap.add_argument("--max-refusal-rate", type=float, default=0.5)
    ap.add_argument("--min-checks", type=int, default=10)
    try:
        a = ap.parse_args(argv)
    except SystemExit:
        return 2
    lines, code = run(a.rt_url, a.ps_url, http_call(os.environ.get("TOKEN", "")), a.scan, a.wait, a.expect_mode, a.max_refusal_rate, a.min_checks)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
