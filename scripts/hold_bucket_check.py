#!/usr/bin/env python3
"""hold_bucket_check.py - how long do closed REAL trades really last, and what do they return per holding length (group 421).

  curl -s "http://127.0.0.1:8007/positions/REAL/records?days=60&limit=1000" > rt_$(date +%F).json     # adjust host / port / admin auth
  python3 scripts/hold_bucket_check.py rt_*.json [--mode REAL] [--min-trades 20] [--json]

Pass ALL the rt_*.json exports together: records are de-duplicated by (mode, position_id), the newest file wins, so overlapping exports are fine.

Buckets (trading sessions between the entry day and the exit day, IST, weekends and NSE holidays skipped): same day, 1 day, 2-5 days, 6+ days.
Per bucket: trades, mean return % (net of charges when the record knows the size, else the gross price move), win rate and the exit reasons.
A bucket under --min-trades (default 20) gets no verdict. A difference between two buckets is called only when BOTH have enough trades and the
95% bootstrap interval of the difference of means excludes zero (fixed seed, so the same input always prints the same answer).

It also prints how many trades were held 2+ sessions, which is what the Buy Sniper card used to claim ("2 to 5 Trading Days"), and how many had
no exit reason. Read-only; it only reads the files you give it. Records from before group 421 carry no hold fields: they are computed here from
opened_at / closed_at (weekends skipped, holidays not known to the script), so run it on fresh exports when you can.
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

BUCKETS = ("same_day", "1_day", "2_5_days", "6_plus_days")
LABELS = {"same_day": "same day", "1_day": "1 day", "2_5_days": "2-5 days", "6_plus_days": "6+ days"}
_IST = timezone(timedelta(hours=5, minutes=30))
_UNATTRIBUTED = ("none", None)


def _num(v: Any) -> Optional[float]:
    try:
        if v is None or isinstance(v, bool):
            return None
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def load_records(paths: Iterable[str]) -> List[dict]:
    """Records of every file ({"records": [...]} or a bare list), de-duplicated by (mode, position_id); later files win. Unreadable file: skipped with a note."""
    by_key: Dict[Tuple[Any, Any], dict] = {}
    loose: List[dict] = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as e:  # noqa: BLE001
            print(f"  skipped {p}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        recs = data.get("records") if isinstance(data, dict) else data
        if not isinstance(recs, list):
            print(f"  skipped {p}: no records list", file=sys.stderr)
            continue
        for r in recs:
            if not isinstance(r, dict):
                continue
            pid = r.get("position_id")
            if pid is None:
                loose.append(r)
            else:
                by_key[(r.get("mode"), pid)] = r
    return list(by_key.values()) + loose


def _parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def sessions_between(opened: Any, closed: Any) -> Optional[int]:
    """Weekday sessions after the entry's IST date up to the exit's IST date (0 = same day). Holidays are not known here."""
    a, b = _parse_ts(opened), _parse_ts(closed)
    if a is None or b is None:
        return None
    d0, d1 = a.astimezone(_IST).date(), b.astimezone(_IST).date()
    if d1 < d0:
        return None
    n, d = 0, d0
    while d < d1 and n < 4000:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def bucket_of(rec: dict) -> Optional[str]:
    """The record's own hold_bucket (group 421+), else from held_days, else from opened_at / closed_at."""
    b = rec.get("hold_bucket")
    if b in BUCKETS:
        return b
    d = rec.get("held_days")
    d = int(d) if _num(d) is not None else sessions_between(rec.get("opened_at"), rec.get("closed_at"))
    if d is None:
        return None
    return "same_day" if d <= 0 else "1_day" if d == 1 else "2_5_days" if d <= 5 else "6_plus_days"


def return_pct(rec: dict) -> Optional[float]:
    """Percent return of one trade: the record's return_pct, else net (or gross) P&L over entry x quantity, else the exit/entry price move."""
    v = _num(rec.get("return_pct"))
    if v is not None:
        return v
    entry, qty = _num(rec.get("entry_price")), _num(rec.get("quantity"))
    pnl = _num(rec.get("net_pnl"))
    if pnl is None:
        pnl = _num(rec.get("gross_pnl"))
    if entry and entry > 0 and qty and qty > 0 and pnl is not None:
        return round(pnl / (entry * qty) * 100.0, 3)
    ex = _num(rec.get("exit_price"))
    if entry and entry > 0 and ex and ex > 0:
        return round((ex / entry - 1.0) * 100.0, 3)
    return None


def _boot_diff(a: List[float], b: List[float], n_boot: int = 2000, seed: int = 7) -> Tuple[float, float, float]:
    """(mean(a) - mean(b), lo, hi) with a 95% percentile bootstrap interval. Deterministic."""
    rng = random.Random(seed)
    diffs = []
    for _ in range(n_boot):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(sum(sa) / len(sa) - sum(sb) / len(sb))
    diffs.sort()
    return (sum(a) / len(a) - sum(b) / len(b), diffs[int(0.025 * n_boot)], diffs[int(0.975 * n_boot) - 1])


def summarize(records: List[dict], mode: Optional[str] = "REAL", min_trades: int = 20) -> Dict[str, Any]:
    rows = [r for r in records if (mode is None or str(r.get("mode") or "").upper() == mode.upper())]
    groups: Dict[str, List[dict]] = {b: [] for b in BUCKETS}
    skipped = 0
    for r in rows:
        b = bucket_of(r)
        if b is None:
            skipped += 1
        else:
            groups[b].append(r)
    out_b: Dict[str, Any] = {}
    series: Dict[str, List[float]] = {}
    for b in BUCKETS:
        rs = groups[b]
        rets = [x for x in (return_pct(r) for r in rs) if x is not None]
        series[b] = rets
        reasons: Dict[str, int] = {}
        for r in rs:
            key = r.get("exit_reason") or "no_exit_reason"
            reasons[key] = reasons.get(key, 0) + 1
        out_b[b] = {"n": len(rs), "n_with_return": len(rets), "mean_return_pct": round(sum(rets) / len(rets), 3) if rets else None,
                    "win_rate_pct": round(100.0 * sum(1 for x in rets if x > 0) / len(rets), 1) if rets else None,
                    "exit_reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
                    "enough": len(rets) >= min_trades}
    comparisons = []
    base = "same_day"
    if out_b[base]["enough"]:
        for b in BUCKETS[1:]:
            if out_b[b]["enough"]:
                d, lo, hi = _boot_diff(series[b], series[base])
                call = "better" if lo > 0 else "worse" if hi < 0 else "no clear difference"
                comparisons.append({"bucket": b, "vs": base, "diff_pp": round(d, 3), "ci95": [round(lo, 3), round(hi, 3)], "call": call})
    n_all = sum(v["n"] for v in out_b.values())
    held_2plus = out_b["2_5_days"]["n"] + out_b["6_plus_days"]["n"]
    no_reason = sum(1 for b in BUCKETS for r in groups[b] if not r.get("exit_reason"))
    enough_any = any(v["enough"] for v in out_b.values())
    verdict = ("NO VERDICT: every bucket has fewer than %d closed trades" % min_trades) if not enough_any else (
        "; ".join(f"{LABELS[c['bucket']]} vs same day: {c['call']} ({c['diff_pp']:+.2f} pp, CI {c['ci95'][0]:+.2f}..{c['ci95'][1]:+.2f})" for c in comparisons)
        or "NOTHING TO COMPARE YET: needs enough trades in the same-day bucket AND in another bucket")
    return {"mode": mode, "min_trades": min_trades, "trades": n_all, "skipped_no_dates": skipped, "buckets": out_b, "comparisons": comparisons,
            "held_2plus_sessions": held_2plus, "held_2plus_share_pct": round(100.0 * held_2plus / n_all, 1) if n_all else None,
            "no_exit_reason": no_reason, "verdict": verdict}


def render(s: Dict[str, Any]) -> str:
    lines = [f"{s['trades']} closed {s['mode'] or 'all-mode'} trades (min {s['min_trades']} per bucket for a verdict)", "",
             f"{'Held':<10}{'n':>4}  {'Mean return':>12}  {'Win %':>6}  Exit reasons"]
    for b in BUCKETS:
        v = s["buckets"][b]
        mean = "-" if v["mean_return_pct"] is None else f"{v['mean_return_pct']:+.2f}%"
        win = "-" if v["win_rate_pct"] is None else f"{v['win_rate_pct']:.0f}"
        why = ", ".join(f"{n} {k}" for k, n in v["exit_reasons"].items()) or "-"
        lines.append(f"{LABELS[b]:<10}{v['n']:>4}  {mean:>12}  {win:>6}  {why}" + ("" if v["enough"] or not v["n"] else "   (too few)"))
    lines += ["", f"Held 2+ sessions: {s['held_2plus_sessions']} of {s['trades']}"
              + ("" if s["held_2plus_share_pct"] is None else f" ({s['held_2plus_share_pct']}%)")
              + " - the Buy Sniper card used to say '2 to 5 Trading Days'.",
              f"Trades with no exit reason: {s['no_exit_reason']}" + (f"   (skipped, no dates: {s['skipped_no_dates']})" if s["skipped_no_dates"] else ""),
              f"VERDICT: {s['verdict']}"]
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Hold-length buckets for closed trades (read-only).")
    ap.add_argument("files", nargs="+", help="rt_*.json exports from GET /positions/REAL/records")
    ap.add_argument("--mode", default="REAL", help="REAL (default), DEMO, or ALL")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--json", action="store_true", help="print the summary as JSON")
    a = ap.parse_args(argv)
    recs = load_records(a.files)
    s = summarize(recs, None if a.mode.upper() == "ALL" else a.mode, max(1, a.min_trades))
    print(json.dumps(s, indent=2) if a.json else render(s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
