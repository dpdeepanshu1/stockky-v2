#!/usr/bin/env python3
"""calibration_pack.py - WS-7: turn the walk-forward of scripts/structure_backtest.py into the env lines it really maps to. Advisory, applies nothing.

  python3 scripts/calibration_pack.py --backtest backtest.json [--ab ab_7/ab_report.json --ab ab_11/ab_report.json ...]
                                      [--min-oos-edge 0.25] [--min-oos-n 100] [--out pack.md]

What the walk-forward tunes are the four GATE thresholds (not STRUCTURE_PARAMS_JSON, which overrides the technical engine's own parameters):
  chase   -> STRUCTURE_MAX_CHASE_ATR     (position-stocks: PS_STRUCTURE_MAX_CHASE_ATR)
  stretch -> STRUCTURE_MAX_STRETCH_ATR   (position-stocks: PS_STRUCTURE_MAX_STRETCH_ATR)
  room    -> STRUCTURE_MIN_ROOM_ATR      (position-stocks: PS_STRUCTURE_MIN_ROOM_ATR)
  rr      -> STRUCTURE_MIN_RR            (real-trade only)
A threshold is suggested only when the rule's walk-forward is `stable`, BOTH sides of its pooled out-of-sample test (passed and rejected) hold at least
--min-oos-n rows (default 100), the edge is above --min-oos-edge (pp) and the most common per-fold pick differs from the current default.
Everything else is reported with the reason it was not suggested. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

# rule -> (real-trade env name, current real-trade default, position-stocks env name or None, current position-stocks default or None)
RULE_MAP: Dict[str, tuple] = {
    "chase": ("STRUCTURE_MAX_CHASE_ATR", 1.5, "PS_STRUCTURE_MAX_CHASE_ATR", 4.0),
    "stretch": ("STRUCTURE_MAX_STRETCH_ATR", 3.0, "PS_STRUCTURE_MAX_STRETCH_ATR", 5.0),
    "room": ("STRUCTURE_MIN_ROOM_ATR", 0.5, "PS_STRUCTURE_MIN_ROOM_ATR", 0.3),
    "rr": ("STRUCTURE_MIN_RR", 0.0, None, None),
}


def _mode(picks: List[float]) -> Optional[float]:
    """Most common pick; on a tie the later fold's pick wins (it was chosen on the most history)."""
    if not picks:
        return None
    c = Counter(picks)
    top = max(c.values())
    return [p for p in picks if c[p] == top][-1]


def suggestions(bt: Any, min_oos_edge: float = 0.25, min_oos_n: int = 100) -> List[Dict[str, Any]]:
    wf = (bt or {}).get("walk_forward") if isinstance(bt, dict) else None
    out: List[Dict[str, Any]] = []
    for rule, (rt_env, rt_now, ps_env, ps_now) in RULE_MAP.items():
        w = (wf or {}).get(rule)
        item: Dict[str, Any] = {"rule": rule, "status": "", "env": rt_env, "now": rt_now, "ps_env": ps_env, "ps_now": ps_now,
                                "oos_edge": None, "stable": None, "picks": [], "passed_n": 0, "rejected_n": 0}
        out.append(item)
        if not isinstance(w, dict):
            item["status"] = "no_input"
            continue
        picks = [f["threshold"] for f in w.get("folds") or [] if f.get("threshold") is not None]
        item.update(oos_edge=w.get("oos_edge"), stable=w.get("stable"), picks=picks,
                    passed_n=int(w.get("oos_passed_n") or 0), rejected_n=int(w.get("oos_rejected_n") or 0))
        if w.get("note") or w.get("oos_edge") is None:
            item["status"] = "insufficient_sample"
        elif min(item["passed_n"], item["rejected_n"]) < min_oos_n:
            item["status"] = "sample_too_small"          # an edge measured on a few dozen rows on one side is noise
        elif not w.get("stable"):
            item["status"] = "unstable"
        elif float(w["oos_edge"]) <= min_oos_edge:
            item["status"] = "no_edge"
        else:
            pick = _mode(picks)
            item["pick"] = pick
            item["status"] = "keep" if pick is None or abs(pick - rt_now) < 1e-9 else "suggest"
    return out


def render(bt: Any, abs_: List[Dict[str, Any]], min_oos_edge: float = 0.25, min_oos_n: int = 100) -> str:
    items = suggestions(bt, min_oos_edge, min_oos_n)
    verdicts = [str(r.get("verdict")) for r in abs_]
    all_pass = bool(verdicts) and all(v == "pass" for v in verdicts)
    L = ["# Structure gate calibration (advisory, applies nothing)", ""]
    if isinstance(bt, dict):
        L.append(f"Data: {bt.get('symbols')} symbols, {bt.get('with_outcome')} samples with a T+{bt.get('horizon')} outcome, entry={bt.get('entry')}, "
                 f"cost={bt.get('cost_pct')}%, slippage={bt.get('slip_pct')}%.")
    L.append(f"A/B verdicts: {', '.join(verdicts) if verdicts else 'none given'}  -> "
             + ("all `pass`." if all_pass else "not all `pass`: treat every line below as a hypothesis."))
    L.append("")
    for it in items:
        tail = f"(OOS edge {it['oos_edge']}, stable {it['stable']}, picks {it['picks']})"
        if it["status"] == "suggest":
            pick = it["pick"]
            where = f"real-trade: {it['env']}={pick:g} (now {it['now']:g})"
            if it["ps_env"]:
                where += f"; position-stocks: {it['ps_env']}={pick:g} (now {it['ps_now']:g})"
            L.append(f"- {it['rule']}: SUGGEST {pick:g} (OOS edge {it['oos_edge']} pp on {it['passed_n']} passed / {it['rejected_n']} rejected; "
                     f"fold picks {it['picks']}) -> {where}")
        else:
            L.append(f"- {it['rule']}: {it['status']} {tail}")
    L += ["", "STRUCTURE_PARAMS_JSON is not touched by any of this: it overrides the technical service's engine parameters only.",
          "Change one threshold at a time, on one service, and watch `GET /structure/edge` (position-stocks) / `/api/structure/report` before the next."]
    return "\n".join(L)


def _load_json(path: str, what: str):
    """Read one input JSON. An unreadable, empty or non-JSON file ends the run with one clear line instead of a traceback
    (an empty backtest.json is what a walk-forward that was killed or failed leaves behind)."""
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise SystemExit(f"calibration_pack: cannot read the {what} {path}: {e}")
    if not text.strip():
        raise SystemExit(f"calibration_pack: the {what} {path} is empty - the job that writes it did not finish; "
                         "run it again (for the walk-forward: the Structure rules walk-forward job) and then this one.")
    try:
        return json.loads(text)
    except ValueError as e:
        raise SystemExit(f"calibration_pack: the {what} {path} is not valid JSON ({e}); it starts with {text.strip()[:80]!r}. "
                         "Run the job that writes it again.")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--backtest", required=True, help="JSON printed by scripts/structure_backtest.py")
    ap.add_argument("--ab", action="append", default=[], help="ab_report.json (repeat for several seeds)")
    ap.add_argument("--min-oos-edge", type=float, default=0.25)
    ap.add_argument("--min-oos-n", type=int, default=100)
    ap.add_argument("--out", default="")
    a = ap.parse_args(argv)
    bt = _load_json(a.backtest, "walk-forward result")
    reports = [_load_json(p, "A/B report") for p in a.ab]
    text = render(bt, reports, a.min_oos_edge, a.min_oos_n)
    print(text)
    if a.out:
        Path(a.out).write_text(text + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
