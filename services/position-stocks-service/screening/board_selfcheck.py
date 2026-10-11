"""screening/board_selfcheck.py - group311: the two live-data checks the Market Board was written against but never run.

  A  F&O underlyings in the Dhan instrument file  (feed/fno_list.diagnose: layout recognised? plausible count? names sane?)
  B  `open` and `avg_price` in market-data POST /quotes/bulk  (the board's day open and exchange VWAP on the market_data feed)

Run on the VM: `python3 scripts/check_board_data.py` (calls GET /board/self-check and prints a plain verdict). Needs the live network;
B only means something while the market is open. Read-only: one instrument-file download and one small bulk quote request. The
evaluation functions are pure so tests pin them without a network."""
from __future__ import annotations

import time
from typing import Callable, List, Optional

PROBE_SYMBOLS = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "SBIN", "ICICIBANK", "ITC", "LT"]


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f and f > 0 else None
    except (TypeError, ValueError):
        return None


def evaluate_bulk(rows: list, market_open: bool) -> dict:
    """Check B verdict from /quotes/bulk rows: open, avg_price (exchange VWAP), day_high / day_low, previous_close present and mutually
    consistent (low <= open <= high; VWAP inside the day's range, 0.5% slack)."""
    n = with_open = with_avg = with_range = with_prev = consistent = 0
    problems: List[str] = []
    sample = []
    for r in rows or []:
        if not isinstance(r, dict) or _num(r.get("price")) is None:
            continue
        n += 1
        op, avg = _num(r.get("open")), _num(r.get("avg_price"))
        hi, lo, prev = _num(r.get("day_high")), _num(r.get("day_low")), _num(r.get("previous_close"))
        with_open += op is not None
        with_avg += avg is not None
        with_prev += prev is not None
        if hi is not None and lo is not None and hi >= lo:
            with_range += 1
            ok = True
            if op is not None and not (lo * 0.995 <= op <= hi * 1.005):
                ok = False; problems.append(f"{r.get('symbol')}: open {op} outside day range {lo}-{hi}")
            if avg is not None and not (lo * 0.995 <= avg <= hi * 1.005):
                ok = False; problems.append(f"{r.get('symbol')}: avg_price {avg} outside day range {lo}-{hi}")
            consistent += ok
        if len(sample) < 3:
            sample.append({k: r.get(k) for k in ("symbol", "price", "open", "avg_price", "day_high", "day_low", "previous_close")})
    if n == 0:
        return {"ok": False, "verdict": "no priced rows came back from /quotes/bulk", "rows": 0}
    miss = []
    if with_open < n * 0.8:
        miss.append(f"open present on {with_open}/{n} rows (on the market_data feed there is then no day open: the opening gate fails closed 09:15-10:00 and the board must use the first bar)")
    if with_avg < n * 0.8:
        miss.append(f"avg_price present on {with_avg}/{n} rows (the board falls back to a VWAP built from its own bars)")
    if problems:
        miss.append(f"{len(problems)} inconsistent value(s), e.g. {problems[0]}")
    ok = not miss
    verdict = f"ok: open and avg_price present and consistent on {n}/{n} rows" if ok else "; ".join(miss)
    if not market_open:
        verdict += " (market closed: values describe the last session)"
    return {"ok": ok, "verdict": verdict, "rows": n, "with_open": with_open, "with_avg_price": with_avg, "with_range": with_range,
            "with_previous_close": with_prev, "consistent": consistent, "problems": problems[:5], "sample": sample, "market_open": market_open}


def _fetch_bulk(symbols: List[str]) -> list:
    import httpx
    import config
    with httpx.Client(timeout=10.0) as c:
        r = c.post(f"{config.MARKET_DATA_URL}/quotes/bulk", json={"symbols": symbols})
        r.raise_for_status()
        body = r.json()
    q = body.get("quotes") if isinstance(body, dict) else None
    return q if isinstance(q, list) else []


def check_b(market_open: bool, fetch: Optional[Callable[[List[str]], list]] = None) -> dict:
    try:
        return evaluate_bulk((fetch or _fetch_bulk)(list(PROBE_SYMBOLS)), market_open)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "verdict": f"/quotes/bulk request failed ({type(e).__name__}: {str(e)[:120]})", "rows": 0}


def check_a(report: Optional[dict] = None) -> dict:
    from feed import fno_list
    return fno_list.diagnose(report)


def run(market_open: bool, fno_report: Optional[dict] = None, fetch_bulk: Optional[Callable[[List[str]], list]] = None) -> dict:
    a = check_a(fno_report)
    b = check_b(market_open, fetch_bulk)
    return {"as_of": time.time(), "A_fno_underlyings": a, "B_open_and_avg_price": b, "ok": bool(a.get("ok") and b.get("ok")),
            "note": "Check B only means something while the market is open; run it between 09:20 and 15:20 IST."}
