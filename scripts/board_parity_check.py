#!/usr/bin/env python3
"""board_parity_check.py - go / no-go for removing the legacy Market Board code in position-stocks (screening/market_structure.py).

  python3 scripts/board_parity_check.py --url http://localhost:8006/board/status [--token T] [--min-compared 200] [--min-days 3] [--strict]
  python3 scripts/board_parity_check.py --file board_status.json

Reads the `ms_engine` block of GET /board/status (written by screening/ms_engine_switch.py, group 362 A/B: the engine's zones / swings against the
legacy maths on the same bars, observational only). The legacy bodies are the A/B's baseline and the per-call fallback, so they may go only when:
  * the engine is the one serving the board (requested + vendored + active),
  * `ab.verdict` is `parity_ok` (>= 200 comparisons, no engine-added zone, no swing difference, no comparison error),
  * the comparisons span >= --min-days IST days (one quiet day is not a sample),
  * `fallbacks` is 0 (the engine never raised and handed a call to the legacy code).
Verdict: GO | NO_GO | NOT_ENOUGH_DATA | NO_INPUT. It changes nothing. Exit 1 on NO_GO; with --strict also on NOT_ENOUGH_DATA / NO_INPUT. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

GO, NO_GO, NED, NOINPUT = "GO", "NO_GO", "NOT_ENOUGH_DATA", "NO_INPUT"

# differences the engine is allowed to have against the legacy code (documented in group 362 / 365); shown, never a blocker
DOCUMENTED = ("engine_dropped", "status_changed", "bounds_changed", "other_changed", "cap_knockon", "served_differs")


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def evaluate(status: Any, min_compared: int = 200, min_days: int = 3) -> Dict[str, Any]:
    """Pure. `status` = the /board/status JSON (or just its ms_engine block)."""
    ms = status.get("ms_engine", status) if isinstance(status, dict) else None
    if not isinstance(ms, dict) or not isinstance(ms.get("ab"), dict):
        return {"verdict": NOINPUT, "reasons": ["no ms_engine.ab block in the input (is this GET /board/status of position-stocks?)"],
                "numbers": {}, "next": "Fetch http://<position-stocks>:8006/board/status and pass it with --file or --url."}
    ab = ms["ab"]
    days = ab.get("days") if isinstance(ab.get("days"), dict) else {}
    n_days = sum(1 for d in days.values() if isinstance(d, dict) and _int(d.get("compared")) > 0)
    compared = _int(ab.get("compared"))
    fallbacks = _int(ms.get("fallbacks"))
    numbers = {"compared": compared, "days": n_days, "fallbacks": fallbacks, "engine_version": ms.get("engine_version"),
               "ab_verdict": ab.get("verdict"), "documented_differences": {k: _int(ab.get(k)) for k in DOCUMENTED}}
    verdict = str(ab.get("verdict") or "")
    no_go: List[str] = []
    ned: List[str] = []

    if not (ms.get("requested") and ms.get("vendored") and ms.get("active")):
        no_go.append(f"the engine is not serving the board (requested={ms.get('requested')}, vendored={ms.get('vendored')}, active={ms.get('active')}): "
                     "set BOARD_USE_MS_ENGINE=1 and check the vendored stockky_ms copy")
    if verdict == "contract_break":
        no_go.append(f"A/B verdict contract_break (engine_added={_int(ab.get('engine_added'))}, swings_differ={_int(ab.get('swings_differ'))}, "
                     f"errors={_int(ab.get('errors'))}; last_error={ab.get('last_error')})")
    if fallbacks > 0:
        no_go.append(f"{fallbacks} call(s) fell back to the legacy code (last_error={ms.get('last_error')}): the engine raised; fix that first")
    if no_go:
        return {"verdict": NO_GO, "reasons": no_go, "numbers": numbers, "next": "Do not remove the legacy bodies; read ms_engine.ab.samples in /board/status."}

    if verdict == "disabled":
        ned.append("the A/B is disabled (BOARD_AB_ENABLED=0), so no comparisons are collected")
    elif verdict == "insufficient_sample":
        ned.append(f"A/B verdict insufficient_sample ({compared} comparisons, the A/B needs {_int(ab.get('min_sample')) or 200})")
    elif verdict != "parity_ok":
        ned.append(f"A/B verdict is {verdict or 'missing'}, not parity_ok")
    else:
        if compared < min_compared:
            ned.append(f"{compared} comparisons, this check wants {min_compared}")
        if n_days < min_days:
            ned.append(f"the comparisons span {n_days} IST day(s), this check wants {min_days}")
    if ned:
        return {"verdict": NED, "reasons": ned, "numbers": numbers, "next": "Keep the service running through market hours and re-run."}

    return {"verdict": GO, "reasons": ["parity_ok over enough comparisons and days, no engine fallbacks"], "numbers": numbers,
            "next": "Replace the legacy find_zones / swings bodies with calls to stockky_ms.compat_board, keep the golden test, then drop the fallback in ms_engine_switch."}


def render(r: Dict[str, Any]) -> str:
    n = r.get("numbers") or {}
    lines = [f"Board parity (legacy removal): {r['verdict']}"] + [f"  - {x}" for x in r["reasons"]]
    if n:
        lines.append(f"  compared={n.get('compared')} days={n.get('days')} fallbacks={n.get('fallbacks')} engine_version={n.get('engine_version')} "
                     f"documented differences={n.get('documented_differences')}")
    if r.get("next"):
        lines.append(f"  Next: {r['next']}")
    return "\n".join(lines)


def _load(a: argparse.Namespace) -> Any:
    if a.file:
        return json.loads(Path(a.file).read_text())
    req = urllib.request.Request(a.url, headers={"Authorization": f"Bearer {a.token}"} if a.token else {})
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 - operator-supplied URL
        return json.loads(resp.read().decode("utf-8", "replace"))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--min-compared", type=int, default=200)
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--strict", action="store_true")
    a = ap.parse_args(argv)
    try:
        if not (a.url or a.file):
            raise ValueError("give --url or --file")
        status = _load(a)
    except Exception as e:  # noqa: BLE001
        r = {"verdict": NOINPUT, "reasons": [f"could not read the status: {type(e).__name__}: {e}"], "numbers": {}, "next": ""}
    else:
        r = evaluate(status, a.min_compared, a.min_days)
    print(render(r))
    if r["verdict"] == NO_GO or (a.strict and r["verdict"] in (NED, NOINPUT)):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
