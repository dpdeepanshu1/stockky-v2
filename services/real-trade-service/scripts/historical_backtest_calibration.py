"""
scripts/historical_backtest_calibration.py — 2026-09-04

Answers the honest open question from the last audit: board/results/
bulk_block catalyst types have essentially zero live "entered" positions
yet, so calibrate_decay_profiles.py can't say anything useful about their
bands or hold times — normally that means waiting weeks for enough live
fills to accumulate.

This script gets real (not fabricated) calibration signal much faster by
going backward instead of forward: every WatchlistEntry row already has a
symbol + catalyst_ts, REGARDLESS of whether it ever became a live trade
(active/missed/expired all count, not just entered). For each one, this
pulls the ACTUAL historical price action that followed via market-data-
service's /history endpoint (same endpoint, same normalize_symbol rename
handling — GROWW/ZOMATO-style issues — as the live system already uses),
and simulates: would the current entry_band_pct have let an entry happen,
and if so, what would the current exit profile (trail/target/stop/
max_hold_days) have actually produced.

This is a backtest against real historical prices, not a live P&L
guarantee — slippage, liquidity, and order-fill mechanics aren't modeled.
Treat the output the same way as calibrate_decay_profiles.py's: a
directional signal for a human to sanity-check before editing decay.py,
not an auto-apply. Where the two tools disagree (live data says one thing,
historical backtest says another), trust live data more once there's
enough of it — this tool exists specifically for the gap before that.

Usage (run inside the real-trade-service container):
    python scripts/historical_backtest_calibration.py --mode REAL --lookback-days 60
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
from datetime import datetime, timedelta

sys.path.insert(0, ".")

import httpx

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "http://market-data-service:8001").rstrip("/")
MIN_SAMPLES = 8


async def _fetch_forward_candles(client: httpx.AsyncClient, symbol: str, catalyst_ts: datetime, window_days: int) -> list[dict]:
    """Historical daily candles starting from just before catalyst_ts through
    window_days after. Uses the live system's own /history endpoint so
    symbol-rename handling (ZOMATO->ETERNAL etc.) is identical to what
    entry_engine actually saw — no separate data path to disagree with."""
    days_since_catalyst = (datetime.utcnow() - catalyst_ts).days
    total_days = days_since_catalyst + 2  # +2 buffer either side
    try:
        r = await client.get(
            f"{MARKET_DATA_URL}/history/{symbol}",
            params={"interval": "1d", "days": min(total_days, 730)},
            timeout=15.0,
        )
        if r.status_code != 200:
            return []
        candles = (r.json() or {}).get("candles") or []
    except Exception:
        return []

    out = []
    for c in candles:
        try:
            d = datetime.fromisoformat(str(c["date"])[:10])
        except Exception:
            continue
        if d.date() >= catalyst_ts.date():
            out.append(c)
        if len(out) >= window_days:
            break
    return out


def _simulate(catalyst_price: float, entry_band_pct: float, exit_profile: dict, candles: list[dict]) -> dict | None:
    """
    Given the candles from catalyst day onward, simulate what the CURRENT
    decay.py profile would have actually done. Returns None if there's not
    enough candle data to simulate anything meaningful.
    """
    if not candles or catalyst_price <= 0:
        return None

    # Would this have entered? First candle whose close is within band.
    entry_idx = None
    entry_price = None
    for i, c in enumerate(candles):
        close = c.get("close")
        if close is None:
            continue
        pct_move = (close - catalyst_price) / catalyst_price
        if pct_move <= entry_band_pct:
            entry_idx = i
            entry_price = close
            break
    if entry_idx is None:
        return {"would_enter": False}

    # From entry, walk forward up to max_hold_days, tracking peak return
    # and whether/when a trail-stop-style pullback would have exited early.
    max_hold_days = exit_profile.get("max_hold_days", 10)
    trail_schedule = exit_profile.get("trail_atr_schedule", [(99, 1.5)])
    remaining = candles[entry_idx:entry_idx + max_hold_days + 1]

    peak_price = entry_price
    peak_day = 0
    exit_day = len(remaining) - 1
    exit_reason = "time_stop"
    exit_price = remaining[-1]["close"] if remaining and remaining[-1].get("close") else entry_price

    for day_idx, c in enumerate(remaining):
        close = c.get("close")
        if close is None:
            continue
        if close > peak_price:
            peak_price = close
            peak_day = day_idx
        # Simplified trail check: once past the first schedule breakpoint,
        # exit if price has pulled back more than the trail multiplier's
        # rough ATR-equivalent (using % of peak as a stand-in since we
        # don't have per-symbol ATR in this backtest) — this is a coarse
        # approximation, flagged as such in the output, not a precise
        # reproduction of exit_engine's real ATR-based logic.
        trail_pct = 0.02 * next((mult for max_d, mult in trail_schedule if day_idx <= max_d), trail_schedule[-1][1])
        if day_idx > 0 and close < peak_price * (1 - trail_pct):
            exit_day = day_idx
            exit_price = close
            exit_reason = "trail_stop_approx"
            break

    held_days = exit_day
    pnl_pct = (exit_price - entry_price) / entry_price * 100
    peak_pnl_pct = (peak_price - entry_price) / entry_price * 100

    return {
        "would_enter": True,
        "held_days": held_days,
        "pnl_pct": round(pnl_pct, 1),
        "peak_pnl_pct": round(peak_pnl_pct, 1),
        "peak_day": peak_day,
        "exit_reason": exit_reason,
        "cut_off_before_peak": exit_reason == "time_stop" and peak_day >= held_days - 1,
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="ALL", choices=["REAL", "DEMO", "ALL"])
    parser.add_argument("--lookback-days", type=int, default=60, help="how far back to pull WatchlistEntry rows from")
    args = parser.parse_args()

    import db
    import models
    from watchlist_engine.decay import CATALYST_PROFILES, EXIT_PROFILES

    session = db.get_session_factory()()
    cutoff = datetime.utcnow() - timedelta(days=args.lookback_days)

    print(f"{'='*78}\nHistorical backtest calibration — last {args.lookback_days} days, mode={args.mode}\n{'='*78}\n")
    print("NOTE: this backtests against real historical prices for every catalyst")
    print("detected (not just ones that became live trades) — much faster signal")
    print("than waiting for live fills, but slippage/fills aren't modeled. Treat")
    print("as directional, same as calibrate_decay_profiles.py.\n")

    async with httpx.AsyncClient() as client:
        for ctype in sorted(CATALYST_PROFILES.keys()):
            q = session.query(models.WatchlistEntry).filter(
                models.WatchlistEntry.catalyst_type == ctype,
                models.WatchlistEntry.catalyst_ts >= cutoff,
                models.WatchlistEntry.catalyst_price > 0,  # skip rows where price was never captured (pre-2026-09-04 fix)
            )
            if args.mode != "ALL":
                q = q.filter(models.WatchlistEntry.mode == args.mode)
            entries = q.all()

            if not entries:
                print(f"[{ctype}] no usable watchlist rows (with a real catalyst_price) in this window — skip\n")
                continue

            profile = CATALYST_PROFILES[ctype]
            exit_profile = EXIT_PROFILES.get(profile["horizon_class"], {})

            # BUG FIX (2026-09-06): the forward-candle window used to be
            # args.lookback_days — the SAME number used to filter which
            # WatchlistEntry rows to pull in the first place. That's a
            # coincidental match at the 60-day default, but it silently
            # decouples if someone runs with a shorter --lookback-days (e.g.
            # to focus on recent catalysts): mid-horizon types (results/
            # board, max_hold_days=20) would get truncated to fewer forward
            # candles than their own exit profile needs, biasing every
            # held_days/pnl/cut_off_before_peak stat toward "cut off early"
            # regardless of what actually happened. Size the fetch off the
            # catalyst type's OWN max_hold_days instead, independent of the
            # WatchlistEntry lookback filter.
            max_hold = exit_profile.get("max_hold_days", 10)
            forward_window_days = max_hold + 5  # small buffer past the hold ceiling

            # BUG FIX (2026-09-06): a catalyst whose max_hold_days window
            # hasn't fully elapsed yet (e.g. detected 4 days ago, for a
            # 20-day max_hold results/board catalyst) can't have finished
            # playing out — _simulate would just run out of candles and
            # report whatever the last available day looks like as if it
            # were a real exit, silently understating held_days and
            # overstating cut_off_before_peak_rate/time_stop_rate. Skip
            # those rows here rather than let them quietly contaminate the
            # directional signal these stats are meant to give.
            too_recent = [
                e for e in entries
                if (datetime.utcnow() - e.catalyst_ts).days < max_hold
            ]
            usable_entries = [e for e in entries if e not in too_recent]

            results = []
            for e in usable_entries:
                candles = await _fetch_forward_candles(client, e.symbol, e.catalyst_ts, forward_window_days)
                sim = _simulate(e.catalyst_price, profile["entry_band_pct"], exit_profile, candles)
                if sim is not None:
                    results.append(sim)
                await asyncio.sleep(0.1)  # be gentle on market-data-service

            would_enter = [r for r in results if r.get("would_enter")]
            entry_rate = len(would_enter) / len(results) if results else None

            print(f"[{ctype}]  (current entry_band_pct={profile['entry_band_pct']}, "
                  f"max_hold_days={exit_profile.get('max_hold_days', '?')})")
            print(f"  backtested {len(results)}/{len(entries)} rows "
                  f"({len(too_recent)} too recent to have finished a {max_hold}-day hold yet, "
                  f"rest had no history available)")

            if len(results) < MIN_SAMPLES:
                print(f"  -> not enough backtestable rows yet ({len(results)} < {MIN_SAMPLES})\n")
                continue

            print(f"  simulated entry_rate: {entry_rate:.0%}"
                  + ("  -> band may be too tight, most catalysts moved past it before a same-day entry was possible"
                     if entry_rate is not None and entry_rate < 0.4 else ""))

            if len(would_enter) >= MIN_SAMPLES:
                held = [r["held_days"] for r in would_enter]
                pnl = [r["pnl_pct"] for r in would_enter]
                peak_pnl = [r["peak_pnl_pct"] for r in would_enter]
                cut_off_rate = sum(1 for r in would_enter if r["cut_off_before_peak"]) / len(would_enter)
                print(f"  median_held_days: {statistics.median(held):.1f}   "
                      f"median_pnl_at_exit: {statistics.median(pnl):.1f}%   "
                      f"median_peak_pnl: {statistics.median(peak_pnl):.1f}%")
                print(f"  cut_off_before_peak_rate: {cut_off_rate:.0%}"
                      + ("  -> max_hold_days likely too short, position was still climbing when time-stopped"
                         if cut_off_rate > 0.4 else ""))
            else:
                print(f"  -> only {len(would_enter)} rows would have entered, too few to suggest a hold-time change")
            print()

    session.close()
    print("Reminder: directional signal from real historical prices, not a live-fill")
    print("guarantee (no slippage/liquidity modeling). Sanity-check before editing")
    print("watchlist_engine/decay.py. Cross-check against calibrate_decay_profiles.py")
    print("as live 'entered' counts grow — trust live data more once it's available.")


if __name__ == "__main__":
    asyncio.run(main())
