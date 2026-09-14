#!/usr/bin/env python3
"""
test_position_stocks_pipeline_offline.py — run the REAL position-stocks-
service screening + quality-gate code against real historical NSE bars,
with no live WebSocket feed and no market hours required.

WHY THIS EXISTS
────────────────
Requested after the 2026-09-14 Ganesh Chaturthi holiday incident: "let's
test ~50 stocks to see what timing it spends, how it processes, and how
detailed the values are — not live data, but at least actual stock data,
since the market isn't open." The previous session already noted that the
0.5%/1.0%/1.5%/2.5% window floors, the 40/40 fundamental/technical floors,
and the top-3 quality gate can't be responsibly retuned without a live
signal to check against. This script IS that signal: it feeds real
historical price bars through the unmodified screening/engine.py and
screening/quality_gate.py modules (imported directly, not reimplemented —
same principle as real-trade-service/offline_test_harness.py) and prints
every number the pipeline actually computed, so you can look at the
Pipeline tab's thresholds next to real output instead of guessing.

WHAT IS REAL vs SYNTHETIC
──────────────────────────
Two data sources are supported — pick with --data-source (default: angelone):

  --data-source angelone (DEFAULT — this is the broker the deployed
  service actually trades through, so it's the more faithful test, and it
  needs no extra pip installs: it reuses feed/angelone_session.py and
  feed/scrip_master.py exactly as main.py does, over the same httpx/pyotp
  your requirements.txt already installs). Needs ANGELONE_CLIENT_ID,
  ANGELONE_MPIN, ANGELONE_API_KEY, ANGELONE_TOTP_SECRET set in your
  environment — the same ones the live service uses. Pulls real 1-minute
  historical candles via AngelOne's SmartAPI getCandleData endpoint for
  the last `--days` days (default 5, so a holiday/weekend still has a
  completed session in range).

  --data-source yfinance (fallback, no broker credentials needed, but
  requires `pip install yfinance` separately — see USAGE below).

Either way:
  - Actual OHLCV bars for every symbol requested, most recent available
    session(s) — neither source requires the market to be open right now.
  - Every threshold, every score, every gate decision: config.py's real
    MIN_PCT_CHANGE_1M/5M/15M/60M, MIN_AVG_VOLUME, MIN_FUNDAMENTAL_SCORE,
    MIN_TECHNICAL_SCORE, MIN_MARKET_CAP_CR, QUALITY_GATE_TOP_N — read
    from the SAME config.py the live service uses, not copied here.
  - The window pct-change math: engine._rolling_pct_change(), called
    directly, unmodified.
  - The quality-gate scoring: screening/quality_gate.py's check(), called
    directly, unmodified — this makes REAL network calls to whatever
    FUNDAMENTAL_URL/TECHNICAL_URL/EVENT_URL config.py resolves to (your
    deployed analysis-intelligence-service, unless overridden below), so
    it needs network access to actually score anything; with
    --skip-quality-gate this stage is skipped entirely.

SYNTHETIC (clearly labeled, and the one place this harness is NOT a 1:1
stand-in for live conditions, regardless of which --data-source you use):
  - The live WS feed pushes a tick roughly every time LTP changes — often
    many per second. Both data sources above only give one bar per minute.
    To avoid engine.py's tick-activity/liquidity gate (a ticks-per-5-
    minutes proxy for volume) reading artificially low just because of
    this sampling gap, each historical bar is expanded into
    `--ticks-per-bar` evenly-spaced synthetic ticks (default 12 — one
    every ~5s across a 1-minute bar) with linearly-interpolated price from
    that bar's open to its close. This density is a deliberate choice to
    approximate live conditions, not a measurement — treat the liquidity-
    gate PASS/FAIL column as illustrative, and the pct-change numbers
    (computed from real open/close prices either way) as the trustworthy
    part of the output.

USAGE
──────
    cd services/position-stocks-service

    # Default: AngelOne historical candles (needs your usual ANGELONE_*
    # env vars already set — no extra pip installs):
    python3 ../../scripts/test_position_stocks_pipeline_offline.py

    # Fewer symbols, skip the network-dependent quality-gate stage:
    python3 ../../scripts/test_position_stocks_pipeline_offline.py \\
        --count 15 --skip-quality-gate

    # Point the quality gate at your deployed analysis-intelligence-service
    # (defaults to whatever config.py/ANALYSIS_INTELLIGENCE_URL resolves to):
    python3 ../../scripts/test_position_stocks_pipeline_offline.py \\
        --analysis-intelligence-url https://analysis-intelligence-service.onrender.com

    # Fallback data source (no AngelOne creds needed, but requires a venv
    # since most Debian/Ubuntu Pythons are PEP-668 "externally managed"):
    python3 -m venv /tmp/harness-venv
    /tmp/harness-venv/bin/pip install yfinance
    /tmp/harness-venv/bin/python3 ../../scripts/test_position_stocks_pipeline_offline.py \\
        --data-source yfinance

Must be run with position-stocks-service's own directory as the working
directory (or pass --service-dir) so `import config` etc. resolve to the
real modules, exactly like main.py does.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# ── 50 liquid NSE large/mid-cap symbols (yfinance ".NS" suffix added below) ──
# Not a curated "good stocks" list — just a broad, liquid sample so the
# screener/quality-gate stages have enough real symbols to say something
# meaningful about timing and gate behavior. Override with --symbols/--symbols-file.
DEFAULT_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "HINDUNILVR",
    "ITC", "SBIN", "BHARTIARTL", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "ASIANPAINT", "MARUTI", "SUNPHARMA", "TITAN", "WIPRO",
    "ULTRACEMCO", "NESTLEIND", "HCLTECH", "NTPC", "POWERGRID", "M&M",
    "TATAMOTORS", "TATASTEEL", "JSWSTEEL", "ADANIENT", "ADANIPORTS",
    "COALINDIA", "BAJAJFINSV", "HDFCLIFE", "SBILIFE", "GRASIM", "DRREDDY",
    "CIPLA", "DIVISLAB", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO",
    "BRITANNIA", "TECHM", "INDUSINDBK", "APOLLOHOSP", "UPL", "BPCL",
    "ONGC", "SHREECEM", "VEDANTA", "PIDILITIND",
]

# AngelOne SmartAPI historical-candle interval codes — see
# https://smartapi.angelbroking.com/docs/Historical (getCandleData).
_ANGELONE_INTERVAL_MAP = {
    "1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE",
    "10m": "TEN_MINUTE", "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE",
    "1h": "ONE_HOUR", "1d": "ONE_DAY",
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_service_modules(service_dir: Path):
    """Import config / feed.ws_client / feed.angelone_session /
    feed.scrip_master / screening.engine / screening.quality_gate from the
    REAL position-stocks-service source tree — same modules main.py
    imports, not copies. Nothing at import time needs a DB, Dhan, or Angel
    One credentials (see those modules' own docstrings) so this is safe to
    import standalone."""
    sys.path.insert(0, str(service_dir))
    import config  # noqa
    from feed import ws_client, angelone_session, scrip_master  # noqa
    from screening import engine, quality_gate  # noqa
    return config, ws_client, angelone_session, scrip_master, engine, quality_gate


@dataclass
class Bar:
    ts: float
    open: float
    close: float
    volume: int


def fetch_symbol_bars(symbol: str, period: str, interval: str) -> Tuple[List[Bar], float]:
    """Real historical OHLCV via yfinance. Returns (bars, fetch_seconds).
    Works regardless of whether the market is open right now — yfinance
    serves the last completed session's intraday bars either way."""
    import yfinance as yf

    t0 = time.perf_counter()
    bars: List[Bar] = []
    try:
        df = yf.Ticker(f"{symbol}.NS").history(period=period, interval=interval)
        for idx, row in df.iterrows():
            bars.append(Bar(
                ts=idx.timestamp(),
                open=float(row["Open"]),
                close=float(row["Close"]),
                volume=int(row.get("Volume", 0) or 0),
            ))
    except Exception as e:
        _log(f"  ! {symbol}: fetch failed ({e})")
    return bars, time.perf_counter() - t0


def fetch_symbol_bars_angelone(
    symbol: str, token: str, session, base_url: str, interval_code: str,
    from_dt: datetime, to_dt: datetime,
) -> Tuple[List[Bar], float]:
    """Real historical OHLCV via AngelOne's SmartAPI getCandleData — the
    same broker/account main.py's live feed authenticates against (see
    feed/angelone_session.py). Returns (bars, fetch_seconds). Works
    regardless of whether the market is open right now; it just returns
    whatever completed candles exist in [from_dt, to_dt]."""
    import httpx

    t0 = time.perf_counter()
    bars: List[Bar] = []
    try:
        resp = httpx.post(
            f"{base_url}/rest/secure/angelbroking/historical/v1/getCandleData",
            headers=session.rest_headers(),
            json={
                "exchange": "NSE",
                "symboltoken": token,
                "interval": interval_code,
                "fromdate": from_dt.strftime("%Y-%m-%d %H:%M"),
                "todate": to_dt.strftime("%Y-%m-%d %H:%M"),
            },
            timeout=20.0,
        )
        resp.raise_for_status()
        body = resp.json()
        if not body.get("status"):
            _log(f"  ! {symbol}: AngelOne historical API error ({body.get('message')})")
        else:
            for row in body.get("data") or []:
                ts_str, o, h, l, c, v = row  # noqa: E741
                bars.append(Bar(
                    ts=datetime.fromisoformat(ts_str).timestamp(),
                    open=float(o), close=float(c), volume=int(v or 0),
                ))
    except Exception as e:
        _log(f"  ! {symbol}: fetch failed ({e})")
    return bars, time.perf_counter() - t0


def synthesize_ticks(symbol: str, bars: List[Bar], ticks_per_bar: int) -> List[Tuple[float, str, float]]:
    """Expand each real bar into `ticks_per_bar` evenly-spaced synthetic
    ticks, linearly interpolating open->close, so engine.py's tick-count
    liquidity proxy sees something closer to live tick density. See the
    module docstring's SYNTHETIC section — this is the one approximation
    in this harness; the prices themselves are still real bar data."""
    ticks: List[Tuple[float, str, float]] = []
    if len(bars) < 2:
        return ticks
    bar_span_s = bars[1].ts - bars[0].ts if len(bars) > 1 else 60.0
    step = max(bar_span_s / max(ticks_per_bar, 1), 1.0)
    for bar in bars:
        for i in range(ticks_per_bar):
            frac = i / max(ticks_per_bar - 1, 1)
            price = bar.open + (bar.close - bar.open) * frac
            ticks.append((bar.ts + i * step, symbol, round(price, 2)))
    return ticks


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--service-dir", default=".", help="Path to position-stocks-service/ (default: cwd)")
    ap.add_argument("--symbols", default="", help="Comma-separated NSE symbols (overrides default 50)")
    ap.add_argument("--symbols-file", default="", help="File with one NSE symbol per line")
    ap.add_argument("--count", type=int, default=50, help="Trim symbol list to this many (default 50)")
    ap.add_argument("--data-source", choices=["angelone", "yfinance"], default="angelone",
                     help="Historical-data source (default: angelone — your live broker, no extra pip installs)")
    ap.add_argument("--days", type=int, default=5, help="[angelone] lookback window in days (default 5, so a holiday/weekend still has a completed session)")
    ap.add_argument("--request-delay", type=float, default=0.4, help="[angelone] seconds to sleep between historical-candle calls, to stay under SmartAPI's rate limit (default 0.4)")
    ap.add_argument("--period", default="5d", help="[yfinance] history period (default 5d)")
    ap.add_argument("--interval", default="1m", help="Bar interval — 1m/5m/15m/1h/1d (default 1m). Applies to both data sources.")
    ap.add_argument("--ticks-per-bar", type=int, default=12, help="Synthetic sub-ticks per real bar (default 12) — see module docstring")
    ap.add_argument("--checkpoints", type=int, default=5, help="How many evenly-spaced scan() checkpoints across the session (default 5)")
    ap.add_argument("--skip-quality-gate", action="store_true", help="Skip the network-dependent fundamental/technical/event stage")
    ap.add_argument("--analysis-intelligence-url", default="", help="Override ANALYSIS_INTELLIGENCE_URL for the quality-gate stage")
    args = ap.parse_args()

    if args.analysis_intelligence_url:
        import os
        os.environ["ANALYSIS_INTELLIGENCE_URL"] = args.analysis_intelligence_url

    if args.data_source == "yfinance":
        try:
            import yfinance  # noqa: F401 — fail fast with one clear message,
            # not one confusing "fetch failed" line per symbol below.
        except ImportError:
            _log("yfinance is not installed. Either install it in a venv:\n"
                 "    python3 -m venv /tmp/harness-venv\n"
                 "    /tmp/harness-venv/bin/pip install yfinance\n"
                 "    /tmp/harness-venv/bin/python3 <this script> --data-source yfinance\n"
                 "...or, simpler: drop --data-source yfinance and use the default "
                 "AngelOne data source instead — it needs no extra installs, just your "
                 "usual ANGELONE_CLIENT_ID / ANGELONE_MPIN / ANGELONE_API_KEY / "
                 "ANGELONE_TOTP_SECRET env vars.")
            return 1

    service_dir = Path(args.service_dir).resolve()
    config, ws_client, angelone_session, scrip_master, engine, quality_gate = _load_service_modules(service_dir)

    symbols = DEFAULT_SYMBOLS
    if args.symbols_file:
        symbols = [s.strip().upper() for s in Path(args.symbols_file).read_text().splitlines() if s.strip()]
    elif args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    symbols = symbols[: args.count]

    _log(f"=== position-stocks-service offline pipeline test — {len(symbols)} symbols "
         f"(data source: {args.data_source}) ===")
    _log(f"Thresholds (from config.py, live values): "
         f"1m>{config.MIN_PCT_CHANGE_1M}% 5m>{config.MIN_PCT_CHANGE_5M}% "
         f"15m>{config.MIN_PCT_CHANGE_15M}% 60m>{config.MIN_PCT_CHANGE_60M}% "
         f"| MIN_AVG_VOLUME={config.MIN_AVG_VOLUME} "
         f"| quality gate top {config.QUALITY_GATE_TOP_N}, "
         f"fund>={config.MIN_FUNDAMENTAL_SCORE} tech>={config.MIN_TECHNICAL_SCORE} "
         f"mcap>=₹{config.MIN_MARKET_CAP_CR}cr\n")

    # ── Stage 1: fetch real historical bars ─────────────────────────────────
    stage1_t0 = time.perf_counter()
    bars_by_symbol: Dict[str, List[Bar]] = {}
    per_symbol_fetch_s: Dict[str, float] = {}

    if args.data_source == "angelone":
        session = angelone_session.get_session()
        if not session.is_configured():
            _log("AngelOne not configured — set ANGELONE_CLIENT_ID, ANGELONE_MPIN, "
                 "ANGELONE_API_KEY, ANGELONE_TOTP_SECRET in your environment (the same "
                 "ones the live service uses), or pass --data-source yfinance instead.")
            return 1
        _log("Logging in to AngelOne (TOTP)...")
        asyncio.run(session.ensure_session())
        interval_code = _ANGELONE_INTERVAL_MAP.get(args.interval, "ONE_MINUTE")
        base_url = "https://apiconnect.angelone.in"
        # AngelOne's fromdate/todate are plain "YYYY-MM-DD HH:MM" strings with
        # no timezone field — it interprets them as IST wall-clock, always.
        # datetime.now() alone returns the CONTAINER's local time, which on
        # most cloud hosts is UTC, not IST — silently shifting the requested
        # window by 5:30h. Anchor explicitly to IST before dropping tzinfo.
        to_dt = datetime.now(IST).replace(tzinfo=None)
        from_dt = to_dt - timedelta(days=args.days)
        token_map = scrip_master.get_tokens_bulk(symbols)
        for i, sym in enumerate(symbols, 1):
            token = token_map.get(sym)
            if not token:
                _log(f"  [{i}/{len(symbols)}] {sym}: no AngelOne token found (delisted/renamed/typo?), skipping")
                bars_by_symbol[sym] = []
                per_symbol_fetch_s[sym] = 0.0
                continue
            bars, secs = fetch_symbol_bars_angelone(sym, token, session, base_url, interval_code, from_dt, to_dt)
            bars_by_symbol[sym] = bars
            per_symbol_fetch_s[sym] = secs
            _log(f"  [{i}/{len(symbols)}] {sym}: {len(bars)} bars in {secs:.2f}s")
            time.sleep(args.request_delay)
    else:
        for i, sym in enumerate(symbols, 1):
            bars, secs = fetch_symbol_bars(sym, args.period, args.interval)
            bars_by_symbol[sym] = bars
            per_symbol_fetch_s[sym] = secs
            _log(f"  [{i}/{len(symbols)}] {sym}: {len(bars)} bars in {secs:.2f}s")

    stage1_s = time.perf_counter() - stage1_t0
    ok_symbols = [s for s in symbols if len(bars_by_symbol[s]) >= 2]
    _log(f"\nStage 1 (data fetch): {stage1_s:.1f}s total, "
         f"{stage1_s / max(len(symbols), 1):.2f}s/symbol avg, "
         f"{len(ok_symbols)}/{len(symbols)} symbols had usable bars\n")

    # ── Stage 2: replay ticks through the REAL screener, at N checkpoints ──
    all_ticks: List[Tuple[float, str, float]] = []
    for sym in ok_symbols:
        all_ticks.extend(synthesize_ticks(sym, bars_by_symbol[sym], args.ticks_per_bar))
    all_ticks.sort(key=lambda t: t[0])

    if not all_ticks:
        _log("No usable bars for any symbol — nothing to replay. Exiting.")
        return 1

    # Checkpoints are spaced by TICK INDEX, not wall-clock time. Real trading
    # sessions only cover ~6.25h/day with ~18h gaps between them (overnight +
    # weekends) — no ticks exist in those gaps at all. Evenly-spaced
    # wall-clock marks land inside those empty gaps more often than not,
    # which just repeats the previous checkpoint's tick index (and its
    # candidates) with nothing new to show — this is why an earlier version
    # of this script could print the same "tick N/M" checkpoint twice in a
    # row. Index-based spacing guarantees each checkpoint covers a distinct,
    # evenly-sized slice of the actual replayed ticks.
    checkpoint_indices = [
        min(len(all_ticks), (len(all_ticks) * (i + 1)) // args.checkpoints)
        for i in range(args.checkpoints)
    ]

    stage2_t0 = time.perf_counter()
    idx = 0
    checkpoint_results = []
    for target_idx in checkpoint_indices:
        cp_t0 = time.perf_counter()
        while idx < target_idx:
            ts, sym, ltp = all_ticks[idx]
            ws_client._tick_buffers[sym].append((ts, ltp))
            engine.on_tick_hook(sym, ltp, 0, ts)
            idx += 1
        candidates = engine.scan(open_symbols=set())
        cp_s = time.perf_counter() - cp_t0
        cp_date = (
            datetime.fromtimestamp(all_ticks[idx - 1][0], tz=IST).strftime("%Y-%m-%d %H:%M")
            if idx else "n/a"
        )
        checkpoint_results.append((idx, candidates, cp_s))
        _log(f"  checkpoint @ tick {idx}/{len(all_ticks)} ({cp_date} IST): "
             f"{len(candidates)} candidate(s) found in {cp_s * 1000:.1f}ms")
    stage2_s = time.perf_counter() - stage2_t0
    _log(f"\nStage 2 (tick replay + {args.checkpoints} scan() calls): {stage2_s:.2f}s total\n")

    # ── Full-detail window table (every symbol × every window, pass or not) ─
    # This is the "spot it yourself" visibility promised last session: not
    # just the candidates that cleared every gate, but the raw pct-change
    # engine._rolling_pct_change() actually computed for every symbol, so a
    # near-miss threshold is visible, not just a pass/fail count.
    _log("=== Per-symbol / per-window detail (final state, all ticks replayed) ===")
    _log(f"{'SYMBOL':<12}{'WINDOW':>7}{'PCT_CHANGE':>12}{'THRESHOLD':>11}{'PASS':>6}{'TICKS/5M':>10}")
    thresholds = {1: config.MIN_PCT_CHANGE_1M, 5: config.MIN_PCT_CHANGE_5M,
                  15: config.MIN_PCT_CHANGE_15M, 60: config.MIN_PCT_CHANGE_60M}
    for sym in ok_symbols:
        tick_count = engine._volume_accum.get(sym, 0)
        for win, floor in thresholds.items():
            pct = engine._rolling_pct_change(sym, win)
            pct_s = f"{pct:.3f}%" if pct is not None else "n/a"
            passed = pct is not None and pct >= floor
            _log(f"{sym:<12}{win:>6}m{pct_s:>12}{floor:>10}%{'YES' if passed else 'no':>6}{tick_count:>10}")

    final_candidates = checkpoint_results[-1][1] if checkpoint_results else []
    _log(f"\n{len(final_candidates)} candidate(s) cleared BOTH the pct-change threshold "
         f"AND the liquidity gate at the final checkpoint.")

    # ── Stage 3: quality gate (real network calls, real config floors) ─────
    if args.skip_quality_gate:
        _log("\nStage 3 (quality gate): skipped (--skip-quality-gate)")
    elif not final_candidates:
        _log("\nStage 3 (quality gate): skipped — no candidates cleared Stage 2 to test")
    else:
        top_n = final_candidates[: max(1, config.QUALITY_GATE_TOP_N)]
        _log(f"\n=== Stage 3: quality gate for top {len(top_n)} candidate(s) "
             f"({config.FUNDAMENTAL_URL} / {config.TECHNICAL_URL} / {config.EVENT_URL}) ===")

        async def _run_all():
            results = []
            for c in top_n:
                t0 = time.perf_counter()
                signal = await quality_gate.check(c.symbol)
                secs = time.perf_counter() - t0
                results.append((c, signal, secs))
            return results

        stage3_t0 = time.perf_counter()
        results = asyncio.run(_run_all())
        stage3_s = time.perf_counter() - stage3_t0

        for candidate, signal, secs in results:
            ok, reason = signal.passes()
            _log(f"  {candidate.symbol:<12} {secs * 1000:>7.0f}ms  "
                 f"fund={signal.fundamental_score} tech={signal.technical_score} "
                 f"mcap=₹{signal.market_cap_cr}cr catalyst={signal.has_positive_catalyst} "
                 f"-> {'PASS' if ok else 'REJECT'} ({reason})")
        _log(f"\nStage 3 (quality gate): {stage3_s:.2f}s total, "
             f"{stage3_s / max(len(top_n), 1):.2f}s/symbol avg")

    total_s = stage1_s + stage2_s
    _log(f"\n=== Summary === data fetch {stage1_s:.1f}s + screening {stage2_s:.2f}s "
         f"= {total_s:.1f}s for {len(symbols)} symbols "
         f"(quality-gate stage timed separately above; it depends on your "
         f"analysis-intelligence-service's own latency, not this script).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
