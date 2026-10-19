"""Gate scorecard (2026-10-19): did the position-stocks gates block winners or losers?

For every SKIPPED row in scalp_candidate_log, look up the recorded bars (/data/bars/<date>/<SYM>.json) and measure
what the stock did AFTER the skip: +30 min, +60 min, close of day, and whether a +TARGET% move came before a -STOP%
move inside 60 minutes. Results are grouped by gate (RANGE_GATE:NEAR_DAY_HIGH, OPENING_GATE:EXTENDED_ABOVE_OR, ...).

  * mean forward return clearly > costs, with a high target-first rate over 30+ samples  -> the gate blocks winners: LOOSEN
  * mean forward return <= 0 or target-first rate low                                    -> the gate saves money: KEEP

Run it INSIDE the position-stocks container (it needs the DB models and /data/bars). From ~/stockky-v2:

    docker compose exec -T position-stocks-service python - --days 15 < scripts/gate_scorecard.py

Options (after the lone "-"): --days N (default 15)   --target 1.5   --stop 1.0   --cost 0.15   --min-n 30
Read-only. Rows are de-duplicated to one per (symbol, gate, 30-minute bucket) so a stock re-skipped every 10 s does
not count as 200 samples. A bar file is [[epoch_s, open, high, low, close, ...], ...].
"""
import argparse
import calendar
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

ap = argparse.ArgumentParser()
ap.add_argument("--days", type=int, default=15)
ap.add_argument("--target", type=float, default=1.5)
ap.add_argument("--stop", type=float, default=1.0)
ap.add_argument("--cost", type=float, default=0.15, help="round-trip cost %% assumed")
ap.add_argument("--min-n", type=int, default=30)
ap.add_argument("--bars-dir", default="/data/bars")
args = ap.parse_args()

import db        # noqa: E402  (available inside the service container)
import models    # noqa: E402

S = db.get_session_factory()()
since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=args.days)
rows = (S.query(models.ScalpCandidateLog)
        .filter(models.ScalpCandidateLog.created_at >= since)
        .order_by(models.ScalpCandidateLog.id).all())

IST = timedelta(hours=5, minutes=30)
_bars_cache = {}


def load_bars(sym, day):
    key = (sym, day)
    if key not in _bars_cache:
        try:
            _bars_cache[key] = json.load(open(os.path.join(args.bars_dir, day, f"{sym}.json")))["bars"]
        except Exception:
            _bars_cache[key] = None
    return _bars_cache[key]


def gate_key(reason, decision):
    if decision == "ENTERED":
        return "ENTERED (baseline)"
    r = (reason or "").strip()
    if not r:
        return "SKIPPED:(no reason)"
    parts = r.split(":")
    if parts[0] == "QUALITY_GATE" and len(parts) > 1:
        # one bucket per metric ("technical_score 35 < floor 40" and "... 33 < floor 40" are the same gate), so samples add up
        return "QUALITY_GATE:" + (parts[1].strip().split(" ")[0] or "other")
    if parts[0] in ("RANGE_GATE", "OPENING_GATE", "PRICE_GUARD", "QUALITY_GATE", "BOARD_GATE"):
        return ":".join(parts[:2]) if len(parts) > 1 else parts[0]
    return parts[0][:40] if parts[0].isupper() else "OTHER:" + r.split(" ")[0][:30]


def forward(bars, t0):
    """(entry, r30, r60, rclose, target_first) as % from the last bar closing at/before t0; None when not measurable."""
    idx = None
    for i, b in enumerate(bars):
        if b[0] <= t0:
            idx = i
        else:
            break
    if idx is None or idx >= len(bars) - 1:
        return None
    entry = float(bars[idx][4])
    if entry <= 0:
        return None

    def at(sec):
        j = idx
        for k in range(idx, len(bars)):
            if bars[k][0] <= t0 + sec:
                j = k
            else:
                break
        return (float(bars[j][4]) / entry - 1) * 100

    tgt = stp = None
    outcome = "neither"
    for k in range(idx + 1, len(bars)):
        if bars[k][0] > t0 + 3600:
            break
        hi = (float(bars[k][2]) / entry - 1) * 100
        lo = (float(bars[k][3]) / entry - 1) * 100
        if lo <= -args.stop and hi >= args.target:
            outcome = "stop"          # same bar touches both: assume the worse
            break
        if lo <= -args.stop:
            outcome = "stop"
            break
        if hi >= args.target:
            outcome = "target"
            break
    return at(1800), at(3600), (float(bars[-1][4]) / entry - 1) * 100, outcome


groups = defaultdict(list)
seen = set()
skipped_nobars = 0
for r in rows:
    gk = gate_key(r.reason, r.decision)
    t0 = calendar.timegm(r.created_at.timetuple())
    day = (r.created_at + IST).strftime("%Y-%m-%d")
    bucket = (r.symbol, gk, day, t0 // 1800)
    if bucket in seen:
        continue
    seen.add(bucket)
    bars = load_bars(r.symbol, day)
    if not bars:
        skipped_nobars += 1
        continue
    f = forward(bars, t0)
    if f:
        groups[gk].append(f)

print(f"\nGate scorecard - last {args.days} days, target +{args.target}% / stop -{args.stop}% within 60 min, cost {args.cost}% round trip")
print(f"rows read: {len(rows)}   de-duplicated samples with bars: {sum(len(v) for v in groups.values())}   no-bars (skipped): {skipped_nobars}\n")
hdr = f"{'gate':<46}{'n':>5}{'avg30':>8}{'avg60':>8}{'avgEOD':>8}{'tgt1st%':>9}{'stop1st%':>10}{'net60':>8}  verdict"
print(hdr)
print("-" * len(hdr))
for gk, vals in sorted(groups.items(), key=lambda kv: -len(kv[1])):
    n = len(vals)
    a30 = sum(v[0] for v in vals) / n
    a60 = sum(v[1] for v in vals) / n
    ae = sum(v[2] for v in vals) / n
    tf = 100.0 * sum(1 for v in vals if v[3] == "target") / n
    sf = 100.0 * sum(1 for v in vals if v[3] == "stop") / n
    net60 = a60 - args.cost
    if gk.startswith("ENTERED"):
        verdict = "baseline"
    elif n < args.min_n:
        verdict = "too few samples"
    elif net60 > 0.3 and tf > sf:
        verdict = "LOOSEN? blocks winners"
    elif net60 <= 0 or sf > tf:
        verdict = "KEEP - saves money"
    else:
        verdict = "neutral"
    print(f"{gk:<46}{n:>5}{a30:>8.2f}{a60:>8.2f}{ae:>8.2f}{tf:>9.1f}{sf:>10.1f}{net60:>8.2f}  {verdict}")
print("\nReading it: compare each gate with the ENTERED baseline. Only act on gates with n >= %d over several different days;"
      " one trending day can make every gate look wrong." % args.min_n)
