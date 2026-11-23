#!/usr/bin/env python3
"""gate_outcome_dhan.py - what did gate-blocked names do AFTER their first block? Uses Dhan 1-minute candles (read-only).

For the names blocked_outcome.py labels `no_bars` (position-stocks did not record them). Run INSIDE the market-data container, after 15:45 IST:

  docker compose exec -T market-data-service python - --day 2026-11-23 --syms "EPACKPEB:10:22 ARVIND:10:22" < gate_outcome_dhan.py

--syms is SYMBOL:HH:MM (first block time, IST, from the blocked_outcome table). --up 2.0 = the RAN threshold in percent (same rule as blocked_outcome.py).
Writes nothing, places no order. A symbol Dhan has no candles for is printed as no_data, never guessed.
Price at the block = close of the 1-minute candle that starts at HH:MM. best / worst = highest high / lowest low after it; close = last close of the day.
"""
import argparse
import sys
from datetime import datetime, timedelta


def outcome(candles, hhmm, up_pct):
    """candles: [{date:'YYYY-MM-DD HH:MM', open, high, low, close}] ascending. Returns None or dict(px0, best, worst, close, label)."""
    idx = next((i for i, c in enumerate(candles) if c["date"][11:16] >= hhmm), None)
    if idx is None:
        return None
    px0 = float(candles[idx]["close"])
    if px0 <= 0:
        return None
    after = candles[idx + 1:] or [candles[idx]]
    best = (max(float(c["high"]) for c in after) / px0 - 1) * 100
    worst = (min(float(c["low"]) for c in after) / px0 - 1) * 100
    close = (float(after[-1]["close"]) / px0 - 1) * 100
    if best >= up_pct and close > 0:
        label = "RAN"
    elif best >= up_pct:
        label = "SPIKE"
    else:
        label = "NO_MOVE"
    return {"px0": px0, "best": best, "worst": worst, "close": close, "label": label}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--syms", required=True, help='space separated SYMBOL:HH:MM')
    ap.add_argument("--up", type=float, default=2.0)
    a = ap.parse_args()
    from dhan_data import history, scrip_master  # only present inside the market-data container
    # A fresh python process starts with an EMPTY Dhan symbol map; without this every name reads as "not in the scrip master".
    loaded = scrip_master.ensure_loaded(block=True)
    print("dhan scrip master:", scrip_master.status(), "(loaded)" if loaded else "(NOT loaded: nothing below can be trusted)")
    day = datetime.strptime(a.day, "%Y-%m-%d")
    frm, to = day.replace(hour=9, minute=15), day.replace(hour=15, minute=30)
    rows, counts = [], {}
    for tok in a.syms.split():
        try:
            sym, hh, mm = tok.split(":")
            hhmm = f"{int(hh):02d}:{int(mm):02d}"
        except ValueError:
            print(f"skip bad token {tok!r} (want SYMBOL:HH:MM)")
            continue
        try:
            candles = history.fetch_intraday(sym, "1m", frm, to)
        except Exception as e:  # no scrip / no data / limiter: report, do not guess
            rows.append((sym, hhmm, None, f"no_data ({type(e).__name__}: {str(e)[:70]})"))
            counts["no_data"] = counts.get("no_data", 0) + 1
            continue
        r = outcome(candles, hhmm, a.up) if candles else None
        if r is None:
            rows.append((sym, hhmm, None, "no_data"))
            counts["no_data"] = counts.get("no_data", 0) + 1
        else:
            rows.append((sym, hhmm, r, r["label"]))
            counts[r["label"]] = counts.get(r["label"], 0) + 1
    print(f"DHAN 1-MIN OUTCOMES day {a.day} (percent of price at first block; RAN = best >= +{a.up}% and close above)")
    print("  ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"\n{'symbol':<12}{'1st block':>10}{'price':>10}{'best %':>9}{'worst %':>9}{'close %':>9}  label")
    for sym, hhmm, r, label in rows:
        if r:
            print(f"{sym:<12}{hhmm:>10}{r['px0']:>10.2f}{r['best']:>9.2f}{r['worst']:>9.2f}{r['close']:>9.2f}  {label}")
        else:
            print(f"{sym:<12}{hhmm:>10}{'':>10}{'':>9}{'':>9}{'':>9}  {label}")
    print("\nThe best price is not a price you could sell at. One day proves nothing: read 2-3 sessions together.")


if __name__ == "__main__":
    main()