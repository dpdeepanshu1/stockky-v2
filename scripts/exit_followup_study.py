#!/usr/bin/env python3
"""exit_followup_study.py - E252 (2026-10-28): did the trades we closed early (STAGNATION_EXIT, trailing stop) go on to reach their target, and how many
REAL-mode stop-outs came within minutes of the entry. Read-only; it only reads the files you give it.

  curl -s "http://127.0.0.1:<port>/positions/DEMO/records?days=60&limit=1000" > ps_$(date +%F).json     # position-stocks records (adjust host / port / auth)
  curl -s "http://127.0.0.1:8007/positions/REAL/records?days=60&limit=1000"  > rt_$(date +%F).json     # real-trade records
  python3 scripts/exit_followup_study.py --records ps_*.json rt_*.json --bars /path/to/bar_recorder_dir [--early-min 10] [--json]

  D1  FOLLOW-UP: for every closed trade whose exit was STAGNATION_EXIT or a trailing stop (STOP_HIT / TRAIL_STOP closed ABOVE entry for a long, BELOW for a short),
      look at the recorded 1-minute bars after the exit and report the best price within 30 and 60 minutes (and to the end of that day), as a % of entry, and whether
      the trade's own target (or its stop, whichever came first) would have been reached. Needs <bars>/<YYYY-MM-DD>/<SYMBOL>.json (feed/bar_recorder.py).
  D2  EARLY STOP-OUTS: per mode, how many stop-outs (STOP_HIT / STOP_BREACH_EXIT at a loss) closed within --early-min minutes of the entry, out of all stop-outs.

Records are de-duplicated by (mode, position_id), the newest file wins. A trade without bars after its exit is counted as "no bars", never guessed.
A bucket under --min-trades (default 20) is printed but gets no verdict. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _intraday_data as _bars  # noqa: E402

EARLY_EXIT_STATUSES = ("STAGNATION_EXIT",)
STOP_STATUSES = ("STOP_HIT", "STOP_BREACH_EXIT")


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _ts(v: Any) -> Optional[float]:
    """Epoch seconds from an ISO string (naive = UTC) or a number."""
    n = _num(v)
    if n is not None:
        return n
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except (TypeError, ValueError):
        return None


def load_records(paths: List[str]) -> List[Dict[str, Any]]:
    by_key: Dict[Any, Dict[str, Any]] = {}
    for p in paths:
        try:
            data = json.loads(Path(p).read_text())
        except Exception as e:  # noqa: BLE001
            print(f"skipped {p}: {e}", file=sys.stderr)
            continue
        recs = data.get("records") if isinstance(data, dict) else data
        for r in recs or []:
            if not isinstance(r, dict):
                continue
            pid = r.get("position_id", r.get("id"))
            by_key[(r.get("mode"), pid) if pid is not None else (id(r),)] = r
    return list(by_key.values())


def _status(r: Dict[str, Any]) -> str:
    return str(r.get("exit_reason") or r.get("status") or "").upper()


def _is_short(r: Dict[str, Any]) -> bool:
    return str(r.get("side") or r.get("direction") or "LONG").upper() == "SHORT"


def classify_exit(r: Dict[str, Any]) -> Optional[str]:
    """'stagnation' | 'trail' | None for the D1 population."""
    st = _status(r)
    entry, ex = _num(r.get("entry_price")), _num(r.get("exit_price"))
    if st in EARLY_EXIT_STATUSES:
        return "stagnation"
    if st == "TRAIL_STOP":
        return "trail"
    if st == "STOP_HIT" and entry and ex:
        return "trail" if ((ex < entry) if _is_short(r) else (ex > entry)) else None
    return None


def follow_up(r: Dict[str, Any], bars: List[tuple], horizons=(30, 60)) -> Optional[Dict[str, Any]]:
    """Best move after the exit and what the trade's own levels would have done. bars = [(t, o, h, l, c, v)]. None without bars after the exit."""
    entry, tgt, stp = _num(r.get("entry_price")), _num(r.get("target_price")), _num(r.get("stop_price"))
    t_exit = _ts(r.get("closed_at"))
    if not entry or t_exit is None:
        return None
    short = _is_short(r)
    after = [b for b in bars if b[0] >= t_exit - 30.0]
    if not after:
        return None
    out: Dict[str, Any] = {}
    for h in horizons:
        win = [b for b in after if b[0] <= t_exit + h * 60.0]
        if not win:
            out[f"best_{h}m_pct"] = None
            continue
        best = min(b[3] for b in win) if short else max(b[2] for b in win)
        out[f"best_{h}m_pct"] = ((entry - best) if short else (best - entry)) / entry * 100.0
    best_day = min(b[3] for b in after) if short else max(b[2] for b in after)
    out["best_day_pct"] = ((entry - best_day) if short else (best_day - entry)) / entry * 100.0
    first = None                      # which level the 1-minute bars touch first (a bar touching both counts as the stop: the cautious reading)
    for b in after:
        hit_t = (b[3] <= tgt) if (tgt and short) else (b[2] >= tgt if tgt else False)
        hit_s = (b[2] >= stp) if (stp and short) else (b[3] <= stp if stp else False)
        if hit_s:
            first = "stop"
            break
        if hit_t:
            first = "target"
            break
    out["first_level"] = first or "none"
    return out


def study_followup(records: List[Dict[str, Any]], days: Dict[str, Dict[str, Any]], min_trades: int = 20) -> Dict[str, Any]:
    groups: Dict[str, List[Dict[str, Any]]] = {"stagnation": [], "trail": []}
    no_bars = 0
    for r in records:
        kind = classify_exit(r)
        if not kind:
            continue
        sym = str(r.get("symbol") or "").upper()
        t_exit = _ts(r.get("closed_at"))
        day = _bars.date_of(t_exit) if t_exit else None
        bars = (days.get(sym, {}).get(day, {}) or {}).get("bars") if day else None
        res = follow_up(r, bars or [])
        if res is None:
            no_bars += 1
            continue
        groups[kind].append(res)
    out: Dict[str, Any] = {"no_bars": no_bars}
    for kind, rows in groups.items():
        n = len(rows)
        d: Dict[str, Any] = {"n": n, "enough": n >= min_trades}
        for key in ("best_30m_pct", "best_60m_pct", "best_day_pct"):
            vals = sorted(x[key] for x in rows if x.get(key) is not None)
            d[key + "_median"] = vals[len(vals) // 2] if vals else None
        d["target_first"] = sum(1 for x in rows if x["first_level"] == "target")
        d["stop_first"] = sum(1 for x in rows if x["first_level"] == "stop")
        d["neither"] = sum(1 for x in rows if x["first_level"] == "none")
        d["target_first_pct"] = round(d["target_first"] / n * 100.0, 1) if n else None
        out[kind] = d
    return out


def study_early_stopouts(records: List[Dict[str, Any]], early_min: float = 10.0) -> Dict[str, Any]:
    out: Dict[str, Dict[str, int]] = {}
    for r in records:
        if _status(r) not in STOP_STATUSES:
            continue
        entry, ex = _num(r.get("entry_price")), _num(r.get("exit_price"))
        if entry and ex and ((ex > entry) if not _is_short(r) else (ex < entry)):
            continue                                              # a trailed / breakeven exit above entry is not a stop-out at a loss
        t0, t1 = _ts(r.get("opened_at")), _ts(r.get("closed_at"))
        if t0 is None or t1 is None:
            continue
        mode = str(r.get("mode") or "?").upper()
        m = out.setdefault(mode, {"stop_outs": 0, "early": 0})
        m["stop_outs"] += 1
        if (t1 - t0) / 60.0 <= early_min:
            m["early"] += 1
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--records", nargs="+", required=True, help="records JSON exports (position-stocks and / or real-trade)")
    ap.add_argument("--bars", default="", help="bar_recorder directory (<date>/<SYMBOL>.json); without it only D2 is produced")
    ap.add_argument("--early-min", type=float, default=10.0)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    recs = load_records(a.records)
    result: Dict[str, Any] = {"records": len(recs), "early_stopouts": study_early_stopouts(recs, a.early_min)}
    if a.bars:
        st: Dict[str, int] = {}
        days = _bars.load_dir(a.bars, stats=st)
        result["followup"] = study_followup(recs, days, a.min_trades)
        result["bar_files"] = st
    if a.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(f"records: {result['records']}")
    fu = result.get("followup")
    if fu:
        print(f"\nD1 follow-up after an early exit (no bars for {fu['no_bars']} trades)")
        for kind in ("stagnation", "trail"):
            d = fu[kind]
            if not d["n"]:
                print(f"  {kind:<11} no trades")
                continue
            fmt = lambda v: "-" if v is None else f"{v:+.2f}%"  # noqa: E731
            print(f"  {kind:<11} n={d['n']:<4} median best 30m {fmt(d['best_30m_pct_median'])}, 60m {fmt(d['best_60m_pct_median'])}, day {fmt(d['best_day_pct_median'])}; "
                  f"target first {d['target_first']} ({d['target_first_pct']}%), stop first {d['stop_first']}, neither {d['neither']}"
                  + ("" if d["enough"] else f"   (under {a.min_trades}: no verdict)"))
    elif a.bars == "":
        print("\nD1 skipped: pass --bars <bar_recorder dir>")
    print(f"\nD2 stop-outs closed within {a.early_min:g} minutes of entry")
    for mode, m in sorted(result["early_stopouts"].items()):
        share = f"{m['early'] / m['stop_outs'] * 100:.0f}%" if m["stop_outs"] else "-"
        print(f"  {mode:<6} {m['early']} of {m['stop_outs']} stop-outs ({share})")
    if not result["early_stopouts"]:
        print("  no stop-outs in the records")
    return 0


if __name__ == "__main__":
    sys.exit(main())
