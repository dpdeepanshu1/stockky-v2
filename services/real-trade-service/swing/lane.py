"""swing/lane.py - E132 (plan steps S3-S5): the PURE rules of the swing lane. Stdlib only, no database, no network, no clock, never raises.

Everything the lane decides is written here as small functions so it can be tested with plain numbers; swing/engine.py only gathers the facts
(prices, positions, vetoes) and applies the answer. The rules are the ones swing_backtest.py replayed (so what was tested is what runs):

  signal    evening scan: stock on the `screen` (top SWING_TOP_PCT percent by volatility-adjusted momentum AND above its 200-day average)
  entry     the next session's open + slippage, always CNC; stop = ATR14 x SWING_STOP_ATR_MULT clamped SWING_STOP_MIN_PCT..SWING_STOP_MAX_PCT below the entry
  hold      up to SWING_MAX_HOLD_DAYS sessions (the entry session counts as 1)
  stop      ratcheted up after every close to (close - stop %), never lowered. A bar that opens at or under the stop exits at the open (a gap), one that only
            trades through it exits at the stop; the stop is checked before anything else in the bar.
  time exit the close of the last session

Added on top of the replay (all three can only SHORTEN a hold, none can start a trade the replay would not have started; the report says so):
  * the 15:00 re-check sells when the price is under its 10-day average or last night's percentile rank fell under SWING_KEEP_MIN_PCT_RANK;
  * a carry veto (results due, negative news, market selling into the close) sells instead of carrying;
  * a gap-up of more than SWING_MAX_GAP_UP_PCT over the signal close is not bought.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

LABEL = "SWING_MOMENTUM"            # TradeOrder / TradePosition.entry_decision_label of a swing entry
SOURCE_TAB = "swing"                # TradeOrder / TradePosition.source_tab of a swing entry
MODES = ("off", "shadow", "live")
EXIT_REASONS = ("stop", "time")


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def norm_mode(v: Any) -> str:
    """off | shadow | live; anything else is shadow (the safe default: records, orders nothing)."""
    s = str(v or "").strip().lower()
    return s if s in MODES else "shadow"


def live_allowed(swing_mode: Any, trade_mode: str, real_enabled: bool) -> bool:
    """May the lane BUY for `trade_mode` (DEMO / REAL)? live is DEMO-only until SWING_LIVE_REAL_ENABLED."""
    if norm_mode(swing_mode) != "live":
        return False
    m = str(trade_mode or "").upper()
    return m == "DEMO" or (m == "REAL" and bool(real_enabled))


# ───────────────────────────── stop and size ─────────────────────────────

def stop_pct_from_atr(atr_pct: Any, mult: float, lo: float = 2.0, hi: float = 6.0) -> float:
    """Stop distance in % = ATR14 % x mult clamped lo..hi; no usable ATR -> 3.0 (the same fallback as scripts/entry_rules_backtest.stop_target)."""
    a = _f(atr_pct)
    if a is None or a <= 0:
        return 3.0
    return max(lo, min(a * float(mult), hi))


def entry_levels(entry_open: Any, slip_pct: float, stop_pct: float) -> Optional[Dict[str, float]]:
    """Entry price (open + slippage) and its first stop. None when the open is not a usable price."""
    o = _f(entry_open)
    if o is None or o <= 0:
        return None
    entry = o * (1.0 + float(slip_pct) / 100.0)
    return {"entry": entry, "stop_pct": float(stop_pct), "stop_px": entry * (1.0 - float(stop_pct) / 100.0)}


def gap_up_pct(entry_open: Any, signal_close: Any) -> Optional[float]:
    o, c = _f(entry_open), _f(signal_close)
    if o is None or c is None or c <= 0:
        return None
    return (o / c - 1.0) * 100.0


def gap_too_big(entry_open: Any, signal_close: Any, max_gap_pct: float) -> bool:
    """True when the open is more than max_gap_pct above the signal close (0 = the check is off; an unreadable price does not block)."""
    if not max_gap_pct or max_gap_pct <= 0:
        return False
    g = gap_up_pct(entry_open, signal_close)
    return g is not None and g > max_gap_pct


def entry_qty(price: Any, max_trade_value: float, equity: float, max_exposure_pct: float, open_swing_value: float) -> int:
    """Whole shares for one new swing position: the smaller of the per-position value cap and the room left under the total swing exposure cap.
    0 when there is no room, no price or no equity (a swing entry is never forced)."""
    p = _f(price)
    if p is None or p <= 0 or not max_trade_value or max_trade_value <= 0:
        return 0
    room = float(max_trade_value)
    eq = _f(equity)
    if max_exposure_pct and max_exposure_pct > 0:
        if eq is None or eq <= 0:
            return 0
        room = min(room, eq * max_exposure_pct / 100.0 - max(0.0, float(open_swing_value or 0.0)))
    if room <= 0:
        return 0
    return int(room // p)


def entry_slots(max_positions: int, open_swing: int, picks_per_day: int) -> int:
    """New swing entries allowed this morning."""
    return max(0, min(int(max_positions) - int(open_swing), int(picks_per_day)))


# ───────────────────────────── picking ─────────────────────────────

def pick_candidates(rows: Iterable[Dict[str, Any]], exclude: Set[str], n: int, asof: Optional[str] = None) -> List[Dict[str, Any]]:
    """The day's screened stocks, best rank first, without the excluded symbols. Only a row with a rank, a last close and an ATR can be bought
    (the stop needs the ATR); `screen` already says top percentile AND above the 200-day average (swing/momentum.rank_scores).
    `asof` (E137, IST bar date of the scan): a stock whose own last bar is not that day (halted, suspended, stale data) is not a candidate: its rank is from old
    prices and the next session has no bar to settle or buy against. None = no such check (old callers)."""
    out = []
    for r in rows or []:
        if not r.get("screen") or r.get("rank") is None:
            continue
        if asof is not None and r.get("last_bar_day") != asof:
            continue
        if (_f(r.get("last_close")) or 0) <= 0 or (_f(r.get("atr14_pct")) or 0) <= 0:
            continue
        if str(r.get("symbol") or "").upper() in exclude:
            continue
        out.append(r)
    out.sort(key=lambda r: (r["rank"], str(r.get("symbol"))))
    return out[: max(0, int(n))]


def earnings_block(earnings_day: Optional[date], today: date, window_days: int) -> Optional[str]:
    """Results due inside the holding window (calendar days from today, inclusive). None = no block; an unknown date does not block (same fail-open
    rule as entry_engine/event_risk)."""
    if not window_days or window_days <= 0 or earnings_day is None:
        return None
    d = (earnings_day - today).days
    if 0 <= d <= window_days:
        return f"results due {earnings_day.isoformat()} ({d} day(s) away, inside the {window_days}-day hold window)"
    return None


# ───────────────────────────── the replayed trade (shadow ledger) ─────────────────────────────

def advance(state: Dict[str, Any], bar: Dict[str, Any], hold: int) -> Dict[str, Any]:
    """Apply ONE daily bar (o, l, c, day) to an open simulated trade. `state`: entry, stop_pct, stop_px, sessions (sessions already completed).
    Returns a NEW dict: sessions, stop_px, last_close, last_day and, when the trade ended on this bar, closed=True, exit_price, exit_reason ('stop' | 'time'),
    gap_through (the bar opened at or under the stop). Mirrors scripts/swing_backtest.walk_trail: stop first (a gap exits at the open, otherwise at the stop),
    else the stop ratchets to (close - stop %) and the last session exits at its close."""
    st = dict(state)
    o, lo, c = _f(bar.get("o")), _f(bar.get("l")), _f(bar.get("c"))
    if o is None or lo is None or c is None or c <= 0:
        st["closed"] = False
        st["bad_bar"] = True
        return st
    st["bad_bar"] = False
    st["sessions"] = int(st.get("sessions") or 0) + 1
    st["last_close"] = c
    st["last_day"] = bar.get("day")
    stop = float(st["stop_px"])
    if o <= stop:
        st.update(closed=True, exit_price=o, exit_reason="stop", gap_through=True)
        return st
    if lo <= stop:
        st.update(closed=True, exit_price=stop, exit_reason="stop", gap_through=False)
        return st
    st["stop_px"] = max(stop, c * (1.0 - float(st["stop_pct"]) / 100.0))
    if st["sessions"] >= int(hold):
        st.update(closed=True, exit_price=c, exit_reason="time", gap_through=False)
        return st
    st["closed"] = False
    return st


def result_pcts(entry: float, exit_price: float, cost_pct: float) -> Dict[str, float]:
    """Gross % of the trade, the round-trip cost in % and the net % (rounded for storage)."""
    gross = (float(exit_price) / float(entry) - 1.0) * 100.0
    return {"gross_pct": round(gross, 4), "cost_pct": round(float(cost_pct), 4), "net_pct": round(gross - float(cost_pct), 4)}


# ───────────────────────────── live position: the 15:00 decision ─────────────────────────────

def count_sessions(opened_day: date, today: date, is_trading_day: Callable[[date], bool]) -> int:
    """Trading sessions from the entry day through `today`, both inclusive (the entry session is 1). 0 when today is before the entry day."""
    if today < opened_day:
        return 0
    n, d = 0, opened_day
    while d <= today:
        if is_trading_day(d):
            n += 1
        d = date.fromordinal(d.toordinal() + 1)
    return n


def hold_verdict(*, sessions: int, max_hold: int, ltp: Any, stop: Any, sma10: Any, pct_rank: Any, keep_min_pct_rank: float,
                 require_above_sma10: bool, vetoes: Sequence[str] = ()) -> Tuple[bool, str]:
    """(keep, reason) for one swing position at 15:00. Fail-closed: a missing price, a missing 10-day average or a missing rank sells (a carried
    position is exposed to a night of gap risk, so unknown is not good enough). Order: last session, price, stop, vetoes, trend, rank."""
    if sessions >= int(max_hold):
        return False, f"session {sessions} of {int(max_hold)}: last session, sold at the close"
    p = _f(ltp)
    if p is None or p <= 0:
        return False, "no live price to check the position against"
    s = _f(stop)
    if s is not None and s > 0 and p <= s:
        return False, f"price {p:.2f} is at or under the stop {s:.2f}"
    if vetoes:
        return False, "carry veto: " + "; ".join(str(v) for v in vetoes)
    if require_above_sma10:
        m = _f(sma10)
        if m is None or m <= 0:
            return False, "no 10-day average to check the trend against"
        if p < m:
            return False, f"price {p:.2f} is under its 10-day average {m:.2f}: trend not intact"
    if keep_min_pct_rank and keep_min_pct_rank > 0:
        r = _f(pct_rank)
        if r is None:
            return False, "symbol missing from last night's ranking: rank unknown"
        if r < keep_min_pct_rank:
            return False, f"momentum rank {r:.0f}% fell under {keep_min_pct_rank:.0f}%"
    return True, f"session {sessions} of {int(max_hold)}: trend intact, carried"


# ───────────────────────────── E138: exit-engine backstop ─────────────────────────────

def backstop_verdict(*, sessions: int, max_hold: int, opened_day: Optional[date], today: date, past_cutoff: bool, swing_live: bool) -> Tuple[bool, str]:
    """(sell, reason) for one open swing position, asked on EVERY exit tick. The 15:00 square-off is the only normal way a swing position leaves besides its
    stop, and it runs once a day (and only with the gate's EOD switch on), so this is the net under it. Sell when:
      * sessions > max_hold            the position outlived its last session (the square-off failed or never ran),
      * sessions >= max_hold and past_cutoff   it is the last session and the square-off (some minutes earlier) did not sell it,
      * carried from an earlier day while the lane is not live   a leftover that nothing will ever decide on.
    Any doubt (no entry day, no session count) = no sale: the stop keeps watching, exactly as before this function existed."""
    try:
        if opened_day is None or sessions is None or int(sessions) < 1:
            return False, "unknown entry day or session count: no backstop sale"
        n, mx = int(sessions), int(max_hold)
        if n > mx:
            return True, f"session {n} is past the {mx}-session limit: the 15:00 square-off did not sell it"
        if n >= mx and past_cutoff:
            return True, f"last session ({n} of {mx}) and the 15:00 square-off has not sold it"
        if not swing_live and opened_day < today:
            return True, "carried from an earlier day while SWING_MODE is not live: nothing will decide on it"
    except Exception:  # noqa: BLE001 - a rule that cannot be evaluated never sells
        return False, "backstop could not be evaluated: no sale"
    return False, ""


# ───────────────────────────── auto-stop (plan S5) ─────────────────────────────

def pause_state(closed_newest_first: Sequence[Dict[str, Any]], losses_n: int, pause_on_gap: bool, gap_mult: float,
                resumed_at: Optional[str] = None) -> Tuple[bool, str]:
    """(paused, why). `closed_newest_first`: closed LIVE swing trades, each {closed_at (ISO text), net_pct, gross_pct, stop_pct}. Only trades closed after
    `resumed_at` (the last POST /swing/resume, ISO text) count. Paused when the newest `losses_n` trades all lost net (losses_n = 0 turns that rule off)
    or, with pause_on_gap, any counted trade lost more than gap_mult x its planned stop (the stop did not hold: a gap through it)."""
    rows = [r for r in closed_newest_first or [] if not resumed_at or str(r.get("closed_at") or "") > str(resumed_at)]
    if pause_on_gap:
        for r in rows:
            g, sp = _f(r.get("gross_pct")), _f(r.get("stop_pct"))
            if g is not None and sp and sp > 0 and g < -(sp * float(gap_mult)):
                return True, f"a swing exit lost {g:.1f}% against a planned stop of {sp:.1f}%: the stop did not hold (gap)"
    if losses_n and losses_n > 0 and len(rows) >= losses_n:
        last = rows[: int(losses_n)]
        if all((_f(r.get("net_pct")) is not None and _f(r.get("net_pct")) < 0) for r in last):
            return True, f"{int(losses_n)} losing swing trades in a row"
    return False, ""


# ───────────────────────────── ratchet for a live position ─────────────────────────────

def ratcheted_stop(current_stop: Any, close: Any, stop_pct: Any) -> Optional[float]:
    """The new stop after a close: max(current stop, close x (1 - stop %)), rounded to a paisa. None when nothing can be computed. Never lowers a stop."""
    c, sp, cur = _f(close), _f(stop_pct), _f(current_stop)
    if c is None or c <= 0 or sp is None or sp <= 0:
        return None
    cand = round(c * (1.0 - sp / 100.0), 2)
    return cand if cur is None else max(round(cur, 2), cand)
