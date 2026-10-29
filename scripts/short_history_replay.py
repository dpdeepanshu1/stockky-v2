#!/usr/bin/env python3
"""short_history_replay.py - E107 P2 item 18: would the mirrored SHORT rule have made money on recent 5-minute bars? (read-only, evidence only, gates nothing)

  python3 scripts/short_history_replay.py [--base http://127.0.0.1:8001] [--symbols A,B,C] [--stop-pct 1.0] [--target-pct 2.0] [--charges-pct 0.11]
                                          [--entry-times 10:00,10:30,...] [--min-sessions 10] [--min-trades 200]

For every symbol and every recent session it pretends to SELL at the open of each entry-time bar and walks the following 5-minute bars: a bar that reaches
the stop first, or touches both, counts as the stop (the cautious call); the target is booked at its level; anything still open at the short square-off time
(14:55) is covered at that bar's close. Charges are one flat round-trip % of the traded value. The same replay is run for BUY as a control: if the long mirror
earns and the short loses, the window was a rising market, not a bad short rule.
Break-even aware: with a target of RR times the stop, a short only has to win 1 in (1+RR) trades, so a 30% target rate at 2:1 is NOT a loser.
Verdict (last line): `SHORT REPLAY: PASS | FAIL - reason | NOT_ENOUGH_DATA - progress`. Exit 1 only on FAIL. Stdlib only. Places and changes nothing.
Honest limits: ~1 month of one market mood, no gaps or volatility regime in the entry choice, fixed % levels (not Stockky's ATR levels), stocks of your list only,
overlapping entries on one stock are not independent. A reality check and a filter, never a forecast.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Fetch = Callable[[str], Tuple[int, Any]]
DEFAULT_SYMBOLS = ["RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "AXISBANK", "ITC", "LT", "BHARTIARTL", "KOTAKBANK", "TATAMOTORS",
                   "MARUTI", "SUNPHARMA", "BAJFINANCE", "ADANIPORTS", "TATASTEEL", "ONGC", "NTPC", "WIPRO"]
DEFAULT_ENTRY_TIMES = ["09:45", "10:15", "10:45", "11:15", "11:45", "12:15", "12:45", "13:15"]
SQUAREOFF_MIN = 14 * 60 + 55
Bar = Tuple[int, float, float, float, float]          # (minute of day, open, high, low, close)


def http_fetch(timeout: float = 30.0) -> Fetch:
    def fetch(url: str) -> Tuple[int, Any]:
        try:
            with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
                return r.status, json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return e.code, None
        except Exception:  # noqa: BLE001
            return 0, None
    return fetch


def _hhmm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def parse_candles(candles: Any) -> Dict[str, List[Bar]]:
    """market-data candle rows ({"date": "YYYY-MM-DD HH:MM" IST, open, high, low, close}) -> {day: [bars in time order]}, 09:15 to 15:30 only. Bad rows dropped. Pure."""
    days: Dict[str, Dict[int, Bar]] = {}
    for c in candles or []:
        try:
            ds = str(c.get("date"))[:16]
            day, hm = ds[:10], ds[11:16]
            mod = _hhmm(hm)
            o, h, l, cl = (float(c[k]) for k in ("open", "high", "low", "close"))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if not (555 <= mod < 930) or not (o > 0 and h > 0 and l > 0 and cl > 0) or h < l or any(not math.isfinite(x) for x in (o, h, l, cl)):
            continue
        days.setdefault(day, {})[mod] = (mod, o, h, l, cl)
    return {d: [b[k] for k in sorted(b)] for d, b in sorted(days.items())}


def replay(sessions: Dict[str, List[Bar]], side: str, entry_times: Sequence[int], stop_pct: float, target_pct: float, charges_pct: float) -> List[dict]:
    """One result per (session, entry time): {outcome: target|stop|eod, net_pct}. net_pct is % of the entry value after the flat round-trip charge. Pure."""
    short = side.upper() == "SELL"
    sp, tp = stop_pct / 100.0, target_pct / 100.0
    out: List[dict] = []
    for day, bars in sessions.items():
        by_min = {b[0]: i for i, b in enumerate(bars)}
        for et in entry_times:
            i = by_min.get(et)
            if i is None:
                continue
            entry = bars[i][1]
            stop, target = (entry * (1 + sp), entry * (1 - tp)) if short else (entry * (1 - sp), entry * (1 + tp))
            res: Optional[Tuple[str, float]] = None
            last_close = entry
            for b in bars[i:]:
                if b[0] >= SQUAREOFF_MIN:
                    break
                last_close = b[4]
                hit_stop = b[2] >= stop if short else b[3] <= stop
                hit_tgt = b[3] <= target if short else b[2] >= target
                if hit_stop:                                    # stop first, also when the same bar touches both
                    res = ("stop", stop)
                    break
                if hit_tgt:
                    res = ("target", target)
                    break
            outcome, px = res if res else ("eod", last_close)
            gross = ((entry - px) if short else (px - entry)) / entry * 100.0
            out.append({"day": day, "outcome": outcome, "net_pct": gross - charges_pct})
    return out


MIN_PASS_R = 0.05   # E257: smallest average R that can count as a PASS


def summarize(trades: Sequence[dict], stop_pct: float, target_pct: float) -> Dict[str, Any]:
    n = len(trades)
    if n == 0:
        return {"n": 0}
    rr = target_pct / stop_pct
    c = {k: sum(1 for t in trades if t["outcome"] == k) for k in ("target", "stop", "eod")}
    avg = sum(t["net_pct"] for t in trades) / n
    # E257: trades of one session share the same bars, so the honest sample size is the number of sessions. Standard error of the mean across per-session averages.
    by_day: Dict[str, List[float]] = {}
    for t in trades:
        by_day.setdefault(t["day"], []).append(t["net_pct"])
    day_means = [sum(v) / len(v) for v in by_day.values()]
    if len(day_means) > 1:
        m = sum(day_means) / len(day_means)
        day_se = (sum((x - m) ** 2 for x in day_means) / (len(day_means) - 1) / len(day_means)) ** 0.5
    else:
        day_se = float("inf")
    return {"n": n, "day_se": day_se, "target": c["target"], "stop": c["stop"], "eod": c["eod"], "p_target": c["target"] / n, "be_win": 1.0 / (1.0 + rr), "rr": rr,
            "avg_net_pct": avg, "avg_r": avg / stop_pct, "win_rate": sum(1 for t in trades if t["net_pct"] > 0) / n,
            "sessions": len({t["day"] for t in trades})}


def evaluate(sell: Dict[str, Any], buy: Dict[str, Any], min_sessions: int, min_trades: int) -> Tuple[str, str, List[str]]:
    """Pure. (verdict, reason, lines) with verdict in PASS | FAIL | NOT_ENOUGH_DATA."""
    lines: List[str] = []
    n, ses = int(sell.get("n", 0)), int(sell.get("sessions", 0))
    if n < min_trades or ses < min_sessions:
        return "NOT_ENOUGH_DATA", f"{n} of {min_trades} replayed shorts over {ses} of {min_sessions} sessions (the data source may not serve enough 5-minute history)", lines
    lines.append(f"SELL {n} replays over {ses} sessions: target {sell['target']} ({sell['p_target']*100:.1f}%, break-even {sell['be_win']*100:.1f}%), "
                 f"stop {sell['stop']}, eod {sell['eod']}, average {sell['avg_net_pct']:+.3f}% ({sell['avg_r']:+.2f} R) after charges")
    if buy.get("n"):
        lines.append(f"BUY control {buy['n']} replays: target {buy['p_target']*100:.1f}%, average {buy['avg_net_pct']:+.3f}% ({buy['avg_r']:+.2f} R) after charges")
    drift = bool(buy.get("n")) and buy["avg_net_pct"] > 0 >= sell["avg_net_pct"]
    se = float(sell.get("day_se", 0.0))
    if sell["avg_net_pct"] > 0 and (sell["avg_r"] < MIN_PASS_R or sell["avg_net_pct"] - 1.64 * se <= 0):
        # E257: a mean a hair above zero is not evidence (the 28 Oct run passed at +0.007% with a 3.3% target-hit rate against 33% break-even).
        lines.append(f"average is {sell['avg_net_pct']:+.3f}% with a session-level standard error of {se:.3f}%; PASS needs the 90% lower bound above zero and at least {MIN_PASS_R:+.2f} R")
        return "NOT_ENOUGH_DATA", f"the short rule's edge ({sell['avg_net_pct']:+.3f}%, {sell['avg_r']:+.2f} R) is not distinguishable from zero over {ses} sessions", lines
    if sell["avg_net_pct"] > 0:
        return "PASS", f"the short rule averages {sell['avg_net_pct']:+.3f}% per trade after charges ({sell['avg_r']:+.2f} R) over {n} replays", lines
    why = "the window was a rising market (the long mirror earned), so this is weak evidence against the short rule" if drift else "the short mirror does not earn after charges on these bars"
    return "FAIL", f"average {sell['avg_net_pct']:+.3f}% per trade after charges ({sell['avg_r']:+.2f} R): {why}", lines


def load_sessions(fetch: Fetch, base: str, symbols: Sequence[str], period: str, interval: str) -> Tuple[Dict[str, List[Bar]], List[str]]:
    """Merge every symbol's sessions under keys 'DAY|SYMBOL' so one stock-day is one session. Returns (sessions, failed symbols)."""
    out: Dict[str, List[Bar]] = {}
    failed: List[str] = []
    for s in symbols:
        code, body = fetch(f"{base.rstrip('/')}/history/{urllib.parse.quote(s, safe='&')}?" + urllib.parse.urlencode({"period": period, "interval": interval}))
        days = parse_candles((body or {}).get("candles") if isinstance(body, dict) else None) if code == 200 else {}
        if not days:
            failed.append(s)
            continue
        for d, bars in days.items():
            if len(bars) >= 20:                                       # a half-empty session is not replayed
                out[f"{d}|{s}"] = bars
    return out, failed


def run(fetch: Fetch, base: str, symbols: Sequence[str], entry_times: Sequence[int], stop_pct: float, target_pct: float, charges_pct: float,
        min_sessions: int = 10, min_trades: int = 200, period: str = "1mo", interval: str = "5m") -> Tuple[int, str]:
    sessions, failed = load_sessions(fetch, base, symbols, period, interval)
    lines = [f"data: {len(sessions)} stock-sessions from {len(symbols) - len(failed)} of {len(symbols)} symbols" + (f" (no 5-minute bars for: {', '.join(failed)})" if failed else "")]
    # sessions are keyed DAY|SYMBOL; the day count for the verdict is the number of distinct days
    sell_t = replay({k: v for k, v in sessions.items()}, "SELL", entry_times, stop_pct, target_pct, charges_pct)
    buy_t = replay({k: v for k, v in sessions.items()}, "BUY", entry_times, stop_pct, target_pct, charges_pct)
    for t in sell_t + buy_t:
        t["day"] = str(t["day"]).split("|")[0]
    verdict, reason, more = evaluate(summarize(sell_t, stop_pct, target_pct), summarize(buy_t, stop_pct, target_pct), min_sessions, min_trades)
    lines += more + [f"levels: stop {stop_pct}% target {target_pct}% (RR {target_pct / stop_pct:.2f}), charges {charges_pct}% round trip, square-off 14:55",
                     f"SHORT REPLAY: {verdict}" + (f" - {reason}" if reason else "")]
    return (1 if verdict == "FAIL" else 0), "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base", default="http://127.0.0.1:8001", help="market-data service URL")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--stop-pct", type=float, default=1.0)
    ap.add_argument("--target-pct", type=float, default=2.0)
    ap.add_argument("--charges-pct", type=float, default=0.11)   # E257: live intraday charges measured at about 0.106% of one leg (0.08 understated them)
    ap.add_argument("--entry-times", default=",".join(DEFAULT_ENTRY_TIMES))
    ap.add_argument("--min-sessions", type=int, default=10)
    ap.add_argument("--min-trades", type=int, default=200)
    ap.add_argument("--period", default="1mo")
    a = ap.parse_args(argv)
    if a.stop_pct <= 0 or a.target_pct <= 0 or a.charges_pct < 0 or a.min_sessions < 1 or a.min_trades < 1:
        print("stop, target must be above 0; charges not below 0; minimums at least 1", file=sys.stderr)
        return 2
    try:
        times = [_hhmm(x) for x in a.entry_times.split(",") if x.strip()]
    except ValueError:
        print("--entry-times must look like 10:00,10:30", file=sys.stderr)
        return 2
    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    rc, text = run(http_fetch(), a.base, syms, times, a.stop_pct, a.target_pct, a.charges_pct, a.min_sessions, a.min_trades, a.period)
    print(text)
    return rc


if __name__ == "__main__":
    sys.exit(main())
