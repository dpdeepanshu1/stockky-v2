#!/usr/bin/env python3
"""stagnation_after_exit.py - what did a stock do AFTER position-stocks closed it with a stagnation exit? (read-only, Dhan 1-minute candles)

Answers: is the 20-25 minute no-follow-through / 30 minute flat-band exit closing trades too early (price moves later) or right (price keeps going nowhere / down)?

1) VM host, in ~/stockky-v2: make the trade list (the same file the daily report writes)
     python3 scripts/daily_trade_report.py --days 30 --csv daily_trades.csv --out daily_trades.txt
2) copy it into the market-data container (once per run):
     docker compose cp daily_trades.csv market-data-service:/tmp/daily_trades.csv
3) run INSIDE the market-data container:
     docker compose exec -T market-data-service python - --csv /tmp/daily_trades.csv --pause 1.5 < scripts/stagnation_after_exit.py
   options: --since 2026-11-01   --limit 40 (first 40 trades only, for a test)   --reason no_followthrough | flat_band
   round 680: the same check for Real Trade's early loss exit (13 trades, all closed at about -0.35R by design):
     docker compose exec -T market-data-service python - --csv /tmp/daily_trades.csv --pause 1.5 --service real-trade --exit-prefix failed_entry_exit < scripts/stagnation_after_exit.py

For each position-stocks trade whose exit_reason starts with "stagnation_exit": entry price = close of the entry-minute candle, exit price = close of the exit-minute
candle (approximations: the real fills differ by a few paise). It then prints the return if the trade had been HELD to exit+30 min, exit+60 min and 15:00, and how
often the price later reached +1.5% / +2.4% (about the target) or fell to -2% (about a stop) counted from entry.
Read it as: returns BEFORE charges, and WITHOUT the stop or target the live rules would still have applied (a held trade could also have been stopped out;
the -2% column shows how often that was possible). One day of trades proves nothing; use all the trades in the file. Places no order, writes nothing.
"""
import argparse
import csv
import time
from datetime import datetime


def px_at(candles, hhmm, how="close"):
    """Candle value of the last candle starting at or before hhmm, or None."""
    sel = None
    for c in candles:
        if c["date"][11:16] <= hhmm:
            sel = c
        else:
            break
    return float(sel[how]) if sel else None


def add_minutes(hhmm, m):
    h, mm = int(hhmm[:2]), int(hhmm[3:5])
    t = min(h * 60 + mm + m, 15 * 60)          # nothing is held past 15:00
    return f"{t // 60:02d}:{t % 60:02d}"


def analyse(candles, entry_hhmm, exit_hhmm):
    """Percent figures relative to the entry price. None when the candles do not cover entry or exit."""
    e = px_at(candles, entry_hhmm)
    x = px_at(candles, exit_hhmm)
    if not e or not x or e <= 0:
        return None
    pct = lambda p: (p / e - 1.0) * 100.0
    out = {"actual": pct(x)}
    for label, m in (("h30", 30), ("h60", 60)):
        p = px_at(candles, add_minutes(exit_hhmm, m))
        out[label] = pct(p) if p else None
    p_eod = px_at(candles, "15:00")
    out["eod"] = pct(p_eod) if p_eod else None
    after = [c for c in candles if exit_hhmm < c["date"][11:16] <= "15:00"]
    out["best_after"] = pct(max(float(c["high"]) for c in after)) if after else pct(x)
    out["worst_after"] = pct(min(float(c["low"]) for c in after)) if after else pct(x)
    return out


def summarise(rows):
    """rows: list of analyse() dicts. Mean of each horizon plus how often later prices reached 1.5 / 2.4 / fell to -2 (percent of trades)."""
    n = len(rows)
    if not n:
        return None
    def mean(key):
        v = [r[key] for r in rows if r.get(key) is not None]
        return (sum(v) / len(v)) if v else None
    share = lambda f: 100.0 * sum(1 for r in rows if f(r)) / n
    return {"n": n, "actual": mean("actual"), "h30": mean("h30"), "h60": mean("h60"), "eod": mean("eod"),
            "reach_1_5": share(lambda r: r["best_after"] >= 1.5), "reach_2_4": share(lambda r: r["best_after"] >= 2.4),
            "fell_2": share(lambda r: r["worst_after"] <= -2.0)}


def fmt(v):
    return "   n/a" if v is None else f"{v:6.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True)
    ap.add_argument("--since", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--reason", default="")
    ap.add_argument("--service", default="position-stocks", help="round 680: which service's trades to check (default position-stocks; real-trade for failed_entry_exit)")
    ap.add_argument("--exit-prefix", default="stagnation_exit", help="round 680: exit_reason prefix to check (default stagnation_exit; failed_entry_exit for Real Trade's early loss exit)")
    ap.add_argument("--pause", type=float, default=1.5)
    a = ap.parse_args()
    from dhan_data import history, scrip_master  # only present inside the market-data container
    loaded = scrip_master.ensure_loaded(block=True)   # load first, then print the status
    print("dhan scrip master:", scrip_master.status(), "(loaded)" if loaded else "(NOT loaded: nothing below can be trusted)")
    trades = []
    with open(a.csv, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            reason = (r.get("exit_reason") or "")
            if r.get("service") != a.service or not reason.startswith(a.exit_prefix):
                continue
            if a.reason and a.reason not in reason:
                continue
            if a.since and (r.get("day") or "") < a.since:
                continue
            trades.append(r)
    if a.limit:
        trades = trades[:a.limit]
    print(f"{len(trades)} {a.service} trades with exit reason {a.exit_prefix}* to check")
    cache, groups, skipped, diffs = {}, {}, 0, []
    for r in trades:
        sym = r["symbol"]
        day = r["entry_ist"][:10]
        if r["exit_ist"][:10] != day:
            skipped += 1
            continue
        key = (sym, day)
        if key not in cache:
            d = datetime.strptime(day, "%Y-%m-%d")
            candles = None
            for attempt in (1, 2):
                try:
                    candles = history.fetch_intraday(sym, "1m", d.replace(hour=9, minute=15), d.replace(hour=15, minute=30))
                    break
                except Exception as e:
                    if attempt == 1 and ("429" in str(e) or "RateLimit" in type(e).__name__):
                        time.sleep(5)
                        continue
                    break
            cache[key] = candles
            time.sleep(max(0.0, a.pause))
        res = analyse(cache[key], r["entry_ist"][11:16], r["exit_ist"][11:16]) if cache[key] else None
        if res is None:
            skipped += 1
            continue
        try:
            diffs.append(abs(res["actual"] - float(r["return_pct"])))
        except (TypeError, ValueError):
            pass
        groups.setdefault(r["exit_reason"], []).append(res)
        groups.setdefault("ALL stagnation", []).append(res)
    print(f"checked {sum(len(v) for k, v in groups.items() if k != 'ALL stagnation')}, skipped {skipped} (no candles / overnight / bad time)")
    if diffs:
        print(f"sanity: candle return vs recorded return differ by {sum(diffs) / len(diffs):.2f} points on average (small = the approximation is fine)")
    print(f"\n{'exit reason':<36}{'n':>4} | mean % of entry: {'actual':>6}{'+30m':>7}{'+60m':>7}{'15:00':>7} | % of trades later reaching: {'1.5%':>5}{'2.4%':>6}{'-2%':>6}")
    for k in sorted(groups, key=lambda x: (x == "ALL stagnation", x)):
        s = summarise(groups[k])
        print(f"{k:<36}{s['n']:>4} | {fmt(s['actual']):>22}{fmt(s['h30']):>7}{fmt(s['h60']):>7}{fmt(s['eod']):>7} | {s['reach_1_5']:>27.0f}{s['reach_2_4']:>6.0f}{s['fell_2']:>6.0f}")
    print("\nHeld-longer columns have no stop or target and no charges: an upper-bound picture, not a result. Compare '15:00' with 'actual': if it is clearly higher on 100+ trades the exit is too early; if equal or lower, the exit is doing its job.")


if __name__ == "__main__":
    main()