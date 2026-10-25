"""swing/ranker.py - E130 (plan step S1): fetch daily bars and build one momentum snapshot. READ-ONLY: no order, no gate, no write. Never raises.

Bars come from market-data-service GET /history/{symbol}?period=2y&interval=1d. That service keeps at most MAX_HISTORY_ROWS (260) daily rows, so asking for
2y returns the newest 260 rows, enough for the 253 closes the 12-1 measure needs; a stock with a shorter history is ranked on 6-1 only ("limited").
SWING_RANK_MAX_SYMBOLS bounds one snapshot, SWING_RANK_WORKERS bounds parallel fetches, SWING_RANK_TIMEOUT_S bounds each fetch.
"""
from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from swing import momentum

logger = logging.getLogger("real-trade-swing-ranker")


def _envi(name: str, default: int) -> int:
    try:
        v = int((os.getenv(name) or "").strip() or default)
        return v if v > 0 else default
    except ValueError:
        return default


MAX_SYMBOLS = _envi("SWING_RANK_MAX_SYMBOLS", 200)
WORKERS = min(8, _envi("SWING_RANK_WORKERS", 4))
TIMEOUT_S = float(_envi("SWING_RANK_TIMEOUT_S", 8))
DEFAULT_SYMBOLS = [s.strip().upper() for s in (os.getenv("SWING_RANK_DEFAULT_SYMBOLS") or "").split(",") if s.strip()]
_IST = ZoneInfo("Asia/Kolkata")


def _fetch_rows(symbol: str) -> list:
    """Raw daily candle rows from market-data. Replaced in tests."""
    import httpx
    from market_feed.feed import MARKET_DATA_URL
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    with httpx.Client(timeout=TIMEOUT_S) as client:
        r = client.get(f"{MARKET_DATA_URL}/history/{sym}", params={"period": "2y", "interval": "1d"})
    if r.status_code != 200:
        raise RuntimeError(f"history HTTP {r.status_code}")
    return (r.json() or {}).get("candles") or []


def clean_symbols(symbols: Optional[Sequence[str]], cap: Optional[int] = None) -> List[str]:
    """Upper-case, strip .NS / .BO, drop blanks and duplicates, keep order, cap at `cap` (default MAX_SYMBOLS)."""
    seen, out = set(), []
    for s in symbols or []:
        t = str(s or "").strip().upper().replace(".NS", "").replace(".BO", "")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out[: (MAX_SYMBOLS if not cap or cap <= 0 else int(cap))]


def build_snapshot(symbols: Optional[Sequence[str]] = None, fetch: Optional[Callable[[str], list]] = None,
                   benchmark: Optional[str] = None, min_pct_rank: float = 80.0, max_symbols: Optional[int] = None) -> Dict[str, Any]:
    """One ranked snapshot. `fetch(symbol)` returns candle rows (default: market-data). `benchmark` (optional symbol) adds a 6-month strength-vs-benchmark column.
    `max_symbols` (E132, the swing lane's own universe) raises the per-snapshot cap above SWING_RANK_MAX_SYMBOLS; the /swing/rank route does not pass it."""
    started = time.monotonic()
    syms = clean_symbols(symbols if symbols else DEFAULT_SYMBOLS, max_symbols)
    out: Dict[str, Any] = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "read_only": True,
                           "n_requested": len(syms), "n_ok": 0, "n_limited": 0, "n_no_data": 0, "asof_bar_date": None,
                           "rows": [], "failed": [], "notes": []}
    if not syms:
        out["notes"].append("no symbols: pass symbols=A,B,C or set SWING_RANK_DEFAULT_SYMBOLS")
        return out
    getter = fetch or _fetch_rows
    try:
        from stockky_ms.timeframes import rows_to_bars
    except Exception as e:  # noqa: BLE001
        out["notes"].append(f"bar parser unavailable: {str(e)[:80]}")
        return out

    def one(sym: str):
        try:
            return sym, rows_to_bars(getter(sym)), None
        except Exception as e:  # noqa: BLE001 - one failed fetch must not stop the snapshot
            return sym, [], str(e)[:80]

    bench_closes = None
    if benchmark:
        _, bbars, berr = one(benchmark)
        if bbars:
            bench_closes = momentum.closes_of(bbars)
        else:
            out["notes"].append(f"benchmark {benchmark} unavailable ({berr or 'no bars'}): strength vs benchmark left blank")
    scores, latest = [], 0.0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for sym, bars, err in ex.map(one, syms):
            if not bars:
                out["n_no_data"] += 1
                out["failed"].append({"symbol": sym, "reason": err or "no bars"})
                continue
            latest = max(latest, float(bars[-1][0]))
            scores.append(momentum.score_symbol(sym, bars, bench_closes))
    ranked = momentum.rank_scores(scores, min_pct_rank=min_pct_rank)
    out["rows"] = ranked
    out["n_ok"] = sum(1 for r in ranked if r.get("status") == "ok")
    out["n_limited"] = sum(1 for r in ranked if r.get("status") == "limited")
    if latest > 0:
        out["asof_bar_date"] = datetime.fromtimestamp(latest, tz=_IST).date().isoformat()
    if not any(r.get("rank") is not None for r in ranked):
        out["notes"].append("nothing ranked: need 5+ symbols with 127+ daily bars")
    out["took_s"] = round(time.monotonic() - started, 2)
    return out
