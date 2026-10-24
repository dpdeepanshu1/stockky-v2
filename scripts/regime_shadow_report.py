#!/usr/bin/env python3
"""regime_shadow_report.py - what would the Market Weather profile have done to your REAL trades? (Part 1 step 6 gate; changes nothing)

Joins the closed-trade records (export_records.py, real-trade) with the runner's shadow log (<data>/weather/log.jsonl). For every trade the state IN
FORCE at its entry is the last good log row at or before that minute (the log is the source, so it works from day 1; the stamp on the trade is not needed).
Then the profile for that state is replayed on the trade:
  * OPENING_ENTRY_NOT_BEFORE_IST  entry earlier than the profile time  -> trade skipped
  * ENTRY_LAST_TIME_IST           entry later than the profile time    -> trade skipped
  * RISK_MAX_ENTRIES_PER_DAY      the day's n-th kept entry beyond the cap -> trade skipped
  * MARKET_STATE_RISK_MULT        kept trades' P&L scaled by the multiplier (a first-order estimate: charges are not re-computed)
NOT modelled (reported, never guessed): MARKET_STATE_CONVICTION_ADD (the record has no conviction score) and the Position Stocks dials.

The honest test. A base strategy with a net loss "improves" under ANY rule that trades less. So the gate compares the skipped trades with the average
trade of the same tight days: the skip rule must have removed trades WORSE than average (excess), not merely fewer trades. Variants tried: 1 (the fixed
profile; nothing here is tuned on these trades). Calm / Normal days change nothing and only count as the control.

Verdicts (exit code): PASS (0) | FAIL (1) | NOT_ENOUGH_DATA (1)
  NOT_ENOUGH_DATA: fewer than MIN_DAYS logged trading days or fewer than MIN_TIGHT_TRADES trades on choppy / stressed / panic days.
  PASS: total change > 0 AND the skip excess > 0. FAIL otherwise.
Usage: regime_shadow_report.py --log weather/log.jsonl rt_latest.json [more records files...] [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

HERE = Path(__file__).resolve().parent
for _p in (HERE.parent, HERE.parent / "services" / "calibration-runner-service"):     # /app in the runner image; the repo layout otherwise
    if (_p / "regime" / "__init__.py").exists() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from regime import STATES  # noqa: E402
from regime import profiles  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
MIN_DAYS = 20
MIN_TIGHT_TRADES = 30
TIGHT = ("choppy", "stressed", "panic")
VARIANTS_TRIED = 1


def _dt(s: Any) -> Optional[datetime]:
    try:
        d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).astimezone(IST)


def _hm(s: Any) -> Optional[int]:
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def load_log(path: str) -> List[Dict[str, Any]]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for ln in f:
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            d = _dt(r.get("at"))
            if r.get("ok") and r.get("state") in STATES and d is not None:
                rows.append({"at": d, "day": d.strftime("%Y-%m-%d"), "stage": r.get("stage"), "state": r["state"]})
    rows.sort(key=lambda r: r["at"])
    return rows


def load_trades(paths: Sequence[str]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            doc = json.load(f)
        for r in (doc.get("records") if isinstance(doc, dict) else doc) or []:
            d = _dt(r.get("opened_at"))
            pnl = r.get("net_pnl") if r.get("net_pnl") is not None else r.get("gross_pnl")
            if d is None or pnl is None:
                continue
            key = (r.get("position_id") or r.get("id"), r.get("symbol"), d.isoformat())
            if key in seen:
                continue
            seen.add(key)
            out.append({"symbol": r.get("symbol"), "at": d, "day": d.strftime("%Y-%m-%d"), "min": d.hour * 60 + d.minute, "pnl": float(pnl)})
    out.sort(key=lambda t: t["at"])
    return out


def state_at(log: List[Dict[str, Any]], when: datetime) -> Optional[str]:
    """The state in force at `when`: the last good row of the SAME day at or before it. Never the previous day's, and nothing before the 09:14 lock."""
    best = None
    for r in log:
        if r["at"] > when:
            break
        if r["day"] == when.strftime("%Y-%m-%d") and r["stage"] in ("lock", "tick"):
            best = r["state"]
    return best


def replay(log: List[Dict[str, Any]], trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_state: Dict[str, Dict[str, Any]] = {s: {"trades": 0, "base": 0.0, "kept": 0, "skipped": 0, "skipped_pnl": 0.0, "profile": 0.0} for s in STATES}
    unknown = 0
    days = {r["day"] for r in log if r["stage"] in ("lock", "tick")}
    per_day: Dict[str, List[Dict[str, Any]]] = {}
    for t in trades:
        st = state_at(log, t["at"])
        if st is None:
            unknown += 1
            continue
        t["state"] = st
        per_day.setdefault(t["day"], []).append(t)
    for day_trades in per_day.values():
        kept_n: Dict[str, int] = {}
        for t in sorted(day_trades, key=lambda x: x["at"]):
            p = profiles.PROFILE[t["state"]]
            b = by_state[t["state"]]
            b["trades"] += 1
            b["base"] += t["pnl"]
            skip = False
            lo, hi = _hm(p.get("OPENING_ENTRY_NOT_BEFORE_IST")), _hm(p.get("ENTRY_LAST_TIME_IST"))
            if (lo is not None and t["min"] < lo) or (hi is not None and t["min"] > hi):
                skip = True
            cap = p.get("RISK_MAX_ENTRIES_PER_DAY")
            if not skip and cap is not None and kept_n.get(t["state"], 0) >= int(cap):
                skip = True
            if skip:
                b["skipped"] += 1
                b["skipped_pnl"] += t["pnl"]
            else:
                kept_n[t["state"]] = kept_n.get(t["state"], 0) + 1
                b["kept"] += 1
                b["profile"] += t["pnl"] * float(p.get("MARKET_STATE_RISK_MULT") or 1.0)
    tight = [by_state[s] for s in TIGHT]
    n_tight = sum(b["trades"] for b in tight)
    mean_tight = (sum(b["base"] for b in tight) / n_tight) if n_tight else 0.0
    n_skip = sum(b["skipped"] for b in tight)
    skip_pnl = sum(b["skipped_pnl"] for b in tight)
    excess = n_skip * mean_tight - skip_pnl          # > 0: the skipped trades were worse than the average trade of the same days
    base_total = sum(b["base"] for b in by_state.values())
    prof_total = sum(b["profile"] for b in by_state.values())
    out = {"days_logged": len(days), "trades_joined": sum(b["trades"] for b in by_state.values()), "trades_no_state": unknown, "tight_trades": n_tight,
           "tight_skipped": n_skip, "skipped_pnl": round(skip_pnl, 2), "skip_excess": round(excess, 2), "baseline_net": round(base_total, 2),
           "profile_net": round(prof_total, 2), "change": round(prof_total - base_total, 2), "variants_tried": VARIANTS_TRIED,
           "by_state": {s: {k: round(v, 2) if isinstance(v, float) else v for k, v in b.items()} for s, b in by_state.items()},
           "not_modelled": ["MARKET_STATE_CONVICTION_ADD (no conviction on the record)", "position-stocks dials", "charges re-computed on scaled size"]}
    if out["days_logged"] < MIN_DAYS or n_tight < MIN_TIGHT_TRADES:
        out["verdict"] = "NOT_ENOUGH_DATA"
    else:
        out["verdict"] = "PASS" if out["change"] > 0 and excess > 0 else "FAIL"
    return out


def render(r: Dict[str, Any]) -> str:
    L = ["Market Weather shadow report (what the profile WOULD have done; nothing was applied)", "",
         f"Logged trading days: {r['days_logged']} (need {MIN_DAYS})   Trades joined to a state: {r['trades_joined']}   without a state: {r['trades_no_state']}",
         f"Trades on choppy/stressed/panic days: {r['tight_trades']} (need {MIN_TIGHT_TRADES})   would be skipped: {r['tight_skipped']}", "",
         f"{'state':9} {'trades':>6} {'base net':>10} {'skipped':>8} {'skipped net':>12} {'profile net':>12}"]
    for s in STATES:
        b = r["by_state"][s]
        L.append(f"{s:9} {b['trades']:>6} {b['base']:>10.0f} {b['skipped']:>8} {b['skipped_pnl']:>12.0f} {b['profile']:>12.0f}")
    L += ["", f"Baseline net {r['baseline_net']:+.0f}  ->  profile net {r['profile_net']:+.0f}  (change {r['change']:+.0f})",
          f"Skip excess {r['skip_excess']:+.0f}: skipped trades vs the average trade of the same tight days (> 0 means the rule removed worse-than-average trades, not just fewer).",
          f"Variants tried: {r['variants_tried']} (the fixed profile; nothing tuned on these trades).",
          "Not modelled: " + "; ".join(r["not_modelled"]) + ".", "",
          "A direction, not a proof: a short window of one strategy. PASS still needs your reading before MARKET_STATE_MODE goes live.",
          f"REGIME_SHADOW: {r['verdict']}"]
    return "\n".join(L)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("records", nargs="+", help="export_records.py output file(s)")
    ap.add_argument("--log", required=True, help="<data>/weather/log.jsonl")
    ap.add_argument("--out", default="", help="directory for regime_shadow.json")
    a = ap.parse_args(argv)
    try:
        log, trades = load_log(a.log), load_trades(a.records)
    except (OSError, ValueError) as e:
        print(f"cannot read input: {e}")
        print("REGIME_SHADOW: NOT_ENOUGH_DATA")
        return 1
    r = replay(log, trades)
    print(render(r))
    if a.out:
        Path(a.out).mkdir(parents=True, exist_ok=True)
        (Path(a.out) / "regime_shadow.json").write_text(json.dumps(r, indent=1), encoding="utf-8")
    return 0 if r["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
