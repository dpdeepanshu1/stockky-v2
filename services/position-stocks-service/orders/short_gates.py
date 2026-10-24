"""orders/short_gates.py - the mirrored entry gates for a SHORT (short selling step 5, 2026-10-23). Position Stocks only.

Every long gate lets a trade through when its data is missing (fail OPEN). A short has no ceiling on loss, so every gate here FAILS CLOSED: a missing
tick, VWAP, day range, circuit band, book, board row, structure summary or quality answer is a refusal, never a pass. A gate that itself raises is also a
refusal. The long gates are NOT edited; this module only reads the same config and the same data.

`evaluate(facts, now)` is pure (no network, no clock except `now`): it walks the gates strictest first and returns (code, reason) of the FIRST refusal, or
None when the short may go on. `gather_facts(symbol, ltp)` builds `facts` from the live feed / cached quote / structure and board caches (best effort;
anything it cannot read is left None, which the gates then refuse).

facts keys: ltp, signal_ltp, tick_age_s, live, day_open, day_high, day_low, prev_close, vwap, quote(dict|None), board_row(dict|None),
structure(dict|None), quality(obj|None), today(YYYY-MM-DD), tomorrow(YYYY-MM-DD).

Mirror metrics the daily structure summary does not carry (room down to daily support, chase below a supply zone): not judged here, see the change note.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Tuple

import config
from tz_utils import ist_time_at_or_after, parse_hhmm

logger = logging.getLogger("position-stocks-short-gates")

Verdict = Optional[Tuple[str, str]]


def _pos(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _cfg(name: str, dflt: float) -> float:
    try:
        v = float(getattr(config, name, dflt))
        return v if math.isfinite(v) else dflt
    except (TypeError, ValueError):
        return dflt


def time_window(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    start = parse_hhmm(str(getattr(config, "SHORT_ENTRY_START_IST", "09:45")), 9, 45)
    cut = parse_hhmm(str(getattr(config, "SHORT_ENTRY_CUTOFF_IST", "13:30")), 13, 30)
    if not ist_time_at_or_after(start, now):
        return ("SHORT_TOO_EARLY", f"no short before {start.strftime('%H:%M')} IST (VWAP rests on too few trades)")
    if ist_time_at_or_after(cut, now):
        return ("SHORT_CUTOFF", f"no new short after {cut.strftime('%H:%M')} IST (squeeze risk, little time to work)")
    return None


def price_guard(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """Stale / missing live tick, slippage (live price BELOW the signal: a short sells lower than planned), day fall."""
    live, age = _pos(f.get("live")), _num(f.get("tick_age_s"))
    if live is None:
        return ("SHORT_NO_LIVE_TICK", "no live tick: current price unknown")
    max_age = _cfg("ENTRY_MAX_TICK_AGE_S", 0.0)
    if max_age > 0 and (age is None or age > max_age):
        return ("SHORT_STALE_TICK", f"last tick is {'unknown' if age is None else f'{age:.0f}s'} old (max {max_age:.0f}s)")
    sig = _pos(f.get("signal_ltp"))
    slip_max = _cfg("ENTRY_MAX_SLIPPAGE_PCT", 0.0)
    if sig is not None and slip_max > 0:
        slip = (sig - live) / sig * 100.0
        if slip > slip_max:
            return ("SHORT_SLIPPAGE", f"live {live:.2f} is {slip:.2f}% below signal {sig:.2f} (max {slip_max:.2f}%)")
    fall_max = _cfg("SHORT_MAX_DAY_FALL_PCT", 0.0)
    if fall_max > 0:
        pc = _pos(f.get("prev_close"))
        if pc is None:
            return ("SHORT_NO_PREV_CLOSE", "previous close unknown: day fall cannot be judged")
        fall = (pc - live) / pc * 100.0
        if fall > fall_max:
            return ("SHORT_DAY_FALL", f"already down {fall:.2f}% on the day (max {fall_max:.2f}%)")
    return None


def range_gate(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    lo, hi, live = _pos(f.get("day_low")), _pos(f.get("day_high")), _pos(f.get("live"))
    if lo is None or hi is None or live is None or hi - lo <= 1e-6:
        return ("SHORT_NO_DAY_RANGE", "day high / low unknown: range position cannot be judged")
    pos = max(0.0, min(1.0, (live - lo) / (hi - lo)))
    floor = _cfg("SHORT_MIN_ENTRY_RANGE_POSITION", 0.08)
    if pos <= floor:
        return ("NEAR_DAY_LOW", f"range_pos={pos:.2f} <= {floor:.2f} (day_low={lo:.2f} day_high={hi:.2f}): already at the bottom")
    return None


def gap_gate(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """Refuse a stock that gapped UP and still trades above its open. A gap down that keeps falling is allowed."""
    lim = _cfg("SHORT_GAP_UP_REFUSE_PCT", 0.0)
    if lim <= 0:
        return None
    op, pc, live = _pos(f.get("day_open")), _pos(f.get("prev_close")), _pos(f.get("live"))
    if op is None or pc is None or live is None:
        return ("SHORT_NO_GAP_DATA", "day open / previous close unknown: gap cannot be judged")
    gap = (op - pc) / pc * 100.0
    if gap >= lim and live > op:
        return ("SHORT_GAP_UP", f"gapped up {gap:+.2f}% and still above the open {op:.2f} (live {live:.2f})")
    return None


def vwap_gate(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """Mirror of VWAP_BELOW: a short needs price at or under VWAP (within tolerance). Missing or implausible VWAP refuses."""
    live, v = _pos(f.get("live")), _pos(f.get("vwap"))
    if live is None or v is None:
        return ("SHORT_NO_VWAP", "day VWAP unknown: cannot confirm sellers are in control")
    if abs(live - v) / v > 0.15:
        return ("SHORT_VWAP_IMPLAUSIBLE", f"VWAP {v:.2f} is more than 15% from price {live:.2f}: stale or wrong value")
    tol = max(0.0, _cfg("SHORT_VWAP_TOLERANCE_PCT", 0.15))
    if live > v * (1.0 + tol / 100.0):
        return ("VWAP_ABOVE", f"price {live:.2f} is {(live - v) / v * 100:.2f}% over the day's VWAP {v:.2f} (buyers in control) - not a short entry")
    return None


def circuit_gate(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """Roles swap vs the long gate: near the LOWER circuit = no room and it can lock; near the UPPER circuit = a locked stock cannot be bought back
    (wider buffer). Missing / inconsistent band refuses."""
    q = f.get("quote")
    live = _pos(f.get("live"))
    up, lo = _pos((q or {}).get("upper_circuit")), _pos((q or {}).get("lower_circuit"))
    if live is None or up is None or lo is None or up <= lo:
        return ("SHORT_NO_CIRCUIT_BAND", "circuit band unknown or inconsistent: a short is not entered without it")
    if live > up * 1.01 or live < lo * 0.99:
        return ("SHORT_CIRCUIT_STALE", f"price {live:.2f} is outside the stated band {lo:.2f}-{up:.2f}: stale limits")
    lo_buf = max(0.0, _cfg("CIRCUIT_LOWER_BUFFER_PCT", 2.0))
    up_buf = max(0.0, _cfg("SHORT_UPPER_CIRCUIT_BUFFER_PCT", 3.0))
    if lo_buf > 0 and live <= lo * (1.0 + lo_buf / 100.0):
        return ("CIRCUIT_LOWER", f"price {live:.2f} within {lo_buf:.1f}% of the lower circuit {lo:.2f} (no room for the target, may lock)")
    if up_buf > 0 and live >= up * (1.0 - up_buf / 100.0):
        return ("CIRCUIT_UPPER", f"price {live:.2f} within {up_buf:.1f}% of the upper circuit {up:.2f} (a locked stock cannot be bought back)")
    return None


def depth_gate_short(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """Same spread limit as a long; the book must be readable (the cover order must find sellers)."""
    q = f.get("quote")
    spread = _num((q or {}).get("spread_pct")) if q else None
    book = _num((q or {}).get("book_value_5")) if q else None
    if spread is None and book is None:
        return ("SHORT_DEPTH_MISSING", "no spread / book reading: the cover order's liquidity is unknown")
    max_spread = _cfg("ENTRY_DEPTH_MAX_SPREAD_PCT", 0.0)
    if max_spread > 0 and spread is not None and spread > max_spread:
        return ("DEPTH_SPREAD", f"{spread:.2f}% > {max_spread:.2f}%")
    min_book = _cfg("ENTRY_MIN_BOOK_VALUE", 0.0)
    if min_book > 0 and book is not None and book < min_book:
        return ("DEPTH_THIN_BOOK", f"best-5 book Rs{book:,.0f} < Rs{min_book:,.0f}")
    return None


def board_gate_short(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """The Market Board must rank it BEARISH with enough conviction, and it must not already be extended on the way down."""
    row = f.get("board_row")
    if not isinstance(row, dict):
        return ("SHORT_NO_BOARD_ROW", "the Market Board has no row for this symbol")
    if str(row.get("side") or "") != "bear":
        return ("SHORT_BOARD_NOT_BEAR", f"board side is {row.get('side')!r}, not 'bear'")
    conv = _num(row.get("conviction"))
    need = _cfg("SHORT_MIN_BOARD_CONVICTION", 50.0)
    if conv is None or conv < need:
        return ("SHORT_BOARD_CONVICTION", f"board conviction {conv if conv is not None else 'unknown'} < {need:g}")
    chase = _num(row.get("chase_atr"))
    lim = _cfg("BOARD_CHASE_ATR", 0.0)
    if lim > 0 and chase is not None and chase > lim:
        return ("BOARD_CHASE", f"{chase:.2f} ATR past the zone > {lim:.2f}")
    return None


def _structure_age_ok(s: Dict[str, Any]) -> Optional[str]:
    try:
        from orders import structure_gate as sg
        return sg.no_data_reason(s, s.get("_live_price"))
    except Exception as e:  # noqa: BLE001
        return f"structure check error: {type(e).__name__}"


def structure_gate_short(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """Mirror of the long structure gate, on the fields the daily summary actually has: a bullish daily trend, a fresh bullish break of structure,
    a thin-volume bearish breakdown, or a stretch far BELOW the daily EMA20 refuses. Data is required."""
    s = f.get("structure")
    live = _pos(f.get("live"))
    if not isinstance(s, dict):
        return ("SHORT_STRUCTURE_NODATA", "no daily structure summary")
    s = dict(s, _live_price=live)
    nd = _structure_age_ok(s)
    if nd:
        return ("SHORT_STRUCTURE_NODATA", nd)
    if getattr(config, "STRUCTURE_BEARISH_BLOCK", True):
        if s.get("ms_trend") == "bullish":
            return ("SHORT_STRUCTURE_BULLISH", "daily structure is bullish")
        bars = _num(s.get("ms_bos_bars"))
        if (s.get("ms_bos_dir") == "bullish" and not s.get("ms_bos_failed") and bars is not None
                and bars <= int(_cfg("STRUCTURE_BEARISH_BOS_BARS", 5))):
            return ("SHORT_STRUCTURE_BULLISH_BOS", f"fresh bullish break of structure {int(bars)} bar(s) ago")
    msp, atr, st = _pos(s.get("ms_price")), _pos(s.get("ms_atr")), _num(s.get("ms_stretch_atr"))
    lim = _cfg("STRUCTURE_MAX_STRETCH_ATR", 0.0)
    if lim > 0:
        if st is None or msp is None or atr is None or live is None:
            return ("SHORT_STRUCTURE_NODATA", "EMA20 stretch cannot be computed")
        stretch_now = st + (live - msp) / atr
        if stretch_now < -lim:
            return ("SHORT_STRUCTURE_STRETCHED", f"stretched {abs(stretch_now):.1f} daily ATR below EMA20 (max {lim:g})")
    if str(getattr(config, "STRUCTURE_BREAKOUT_VOLUME_MODE", "enforce")).lower() == "enforce":
        ratio, bars = _num(s.get("ms_bos_vol_ratio")), _num(s.get("ms_bos_bars"))
        vlim = _cfg("STRUCTURE_BREAKOUT_MIN_VOL_RATIO", 1.2)
        if (vlim > 0 and ratio is not None and bars is not None and s.get("ms_bos_dir") == "bearish" and not s.get("ms_bos_failed")
                and bars <= int(_cfg("STRUCTURE_BREAKOUT_BARS", 5)) and ratio < vlim):
            return ("SHORT_STRUCTURE_THIN_BREAKDOWN", f"breakdown {int(bars)} bar(s) ago on thin volume ({ratio:.2f}x, wants {vlim:g}x)")
    return None


def quality_gate_short(f: Dict[str, Any], now: Optional[datetime]) -> Verdict:
    """Weak fundamentals are FINE for a short. Refuse on a positive catalyst, a bulk deal, results today or tomorrow, or when the event service
    did not answer (no news check = no short)."""
    q = f.get("quality")
    if q is None:
        return ("SHORT_NO_QUALITY", "no news / event check available")
    if getattr(q, "event_unchecked", False):
        return ("SHORT_EVENT_UNCHECKED", "the news / earnings service did not answer: not shorting without a news check")
    if getattr(q, "has_positive_catalyst", None):
        return ("SHORT_POSITIVE_CATALYST", "a positive catalyst is on record")
    if getattr(q, "bulk_deal_flag", False):
        return ("SHORT_BULK_DEAL", "a bulk deal is on record")
    ed = getattr(q, "earnings_date", None)
    if ed and ed in (f.get("today"), f.get("tomorrow")):
        return ("SHORT_EARNINGS", f"results dated {ed} (today or tomorrow): gap risk a stop cannot cover")
    return None


# strictest / cheapest first
GATES: Tuple[Tuple[str, Callable[[Dict[str, Any], Optional[datetime]], Verdict]], ...] = (
    ("time", time_window),
    ("price", price_guard),
    ("range", range_gate),
    ("gap", gap_gate),
    ("vwap", vwap_gate),
    ("circuit", circuit_gate),
    ("depth", depth_gate_short),
    ("board", board_gate_short),
    ("structure", structure_gate_short),
    ("quality", quality_gate_short),
)


def evaluate(facts: Dict[str, Any], now: Optional[datetime] = None) -> Verdict:
    """(code, reason) of the first refusing gate, or None. A gate that raises refuses (fail closed). Never raises."""
    for name, fn in GATES:
        try:
            v = fn(facts, now)
        except Exception as e:  # noqa: BLE001
            logger.warning("short gate %s failed (refusing): %s", name, e)
            return ("SHORT_GATE_ERROR", f"{name}: {type(e).__name__}")
        if v:
            return v
    return None


def gather_facts(symbol: str, ltp: float, board_row: Optional[dict] = None, quality: Any = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Build the facts dict from the live feed and caches. Best effort: anything unreadable stays None (and is then refused)."""
    import time as _t
    from datetime import timedelta
    from tz_utils import ist_now
    f: Dict[str, Any] = {"ltp": ltp, "signal_ltp": ltp, "board_row": board_row, "quality": quality, "live": None, "tick_age_s": None,
                         "day_open": None, "day_high": None, "day_low": None, "prev_close": None, "vwap": None, "quote": None, "structure": None}
    try:
        n = ist_now(now)
        f["today"] = n.strftime("%Y-%m-%d")
        f["tomorrow"] = (n + timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception as _e:  # noqa: BLE001
        logger.debug("short_gates.py: ignored %s", _e)
    try:
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        if buf and buf[-1][1] and buf[-1][1] > 0:
            f["live"] = float(buf[-1][1])
            f["tick_age_s"] = _t.time() - float(buf[-1][0])
        ds = ws_client.get_day_stats(symbol)
        if ds:
            f["day_open"], f["day_high"], f["day_low"], f["prev_close"] = ds
        v = ws_client.get_exchange_vwap(symbol)
        if v:
            f["vwap"] = v
    except Exception as e:  # noqa: BLE001
        logger.debug("short facts: feed read failed for %s: %s", symbol, e)
    try:
        from orders import depth_gate
        q = depth_gate._fresh_quote(symbol)
        f["quote"] = q
        if f["vwap"] is None and q:
            f["vwap"] = q.get("avg_price")
    except Exception as e:  # noqa: BLE001
        logger.debug("short facts: quote read failed for %s: %s", symbol, e)
    try:
        from orders import structure_gate
        f["structure"] = structure_gate.fetch_summary(symbol)
    except Exception as e:  # noqa: BLE001
        logger.debug("short facts: structure read failed for %s: %s", symbol, e)
    return f
