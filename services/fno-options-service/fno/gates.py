"""fno/gates.py - WHEN a trade is allowed. Pure: pass the facts in, get (ok, reasons) out. A gate only ever says wait; it never creates an entry.

Gates, in order: market day and window, expiry rules, data age, India VIX, daily limits (trades, open positions, loss), cooldown after a stop-out.
Times are IST wall-clock. The holiday list is passed in by the caller (the service has none of its own: the scrip master / calendar owns it)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .params import Params


def parse_hhmm(s: str, default: dtime) -> dtime:
    try:
        h, m = str(s).strip().split(":")
        return dtime(int(h), int(m))
    except (ValueError, AttributeError):
        return default


@dataclass
class DayState:
    """What has happened today (the engine rebuilds it from its journal each cycle)."""
    trades_today: int = 0
    open_positions: int = 0
    realized_pnl_net: float = 0.0
    last_stopout_ts: Optional[float] = None        # epoch seconds of the latest stop-loss exit
    open_same_side: Dict[str, int] = field(default_factory=dict)   # "NIFTY:CE" -> count, so one idea is not stacked twice
    entries_by_strategy: Dict[str, int] = field(default_factory=dict)   # today's entries per strategy (round 232)
    last_exit_by_key: Dict[str, float] = field(default_factory=dict)    # round 495: "NIFTY:CE" -> epoch seconds of its latest exit of ANY kind except the end-of-day square-off


def is_trading_day(d: date, holidays: Sequence[str] = ()) -> bool:
    return d.weekday() < 5 and d.isoformat() not in set(holidays)


def in_entry_window(now: datetime, p: Params, end: Optional[str] = None) -> bool:
    t = now.time()
    return parse_hhmm(p.entry_start, dtime(9, 30)) <= t <= parse_hhmm(end or p.entry_end, dtime(14, 30))


def settle_until(p: Params) -> Optional[dtime]:
    """Round 497 (E517): the first time an entry is allowed once the settle period after the window opens is over (entry_start + entry_settle_min), or None when the period is off.
    A settle that would run past midnight is capped at 23:59 (param_problems reports it)."""
    if p.entry_settle_min <= 0:
        return None
    t = parse_hhmm(p.entry_start, dtime(9, 30))
    m = min(t.hour * 60 + t.minute + int(p.entry_settle_min), 23 * 60 + 59)
    return dtime(m // 60, m % 60)


def after_squareoff(now: datetime, p: Params, expiry: Optional[object] = None) -> bool:
    """Time to close everything. A position whose contract expires TODAY (`expiry` = its date or ISO string) is closed at the earlier of the normal time and
    `expiry_squareoff_at` (default 14:45): from 7 Dec 2026 an expiring index contract is settled on the last-30-minute VWAP of its constituents, so nothing is held into 15:00."""
    t = now.time()
    if t >= parse_hhmm(p.squareoff_at, dtime(15, 15)):
        return True
    if expiry is not None and str(expiry)[:10] == now.date().isoformat():
        return t >= parse_hhmm(p.expiry_squareoff_at, dtime(14, 45))
    return False


MARKET_OPEN, MARKET_CLOSE = dtime(9, 15), dtime(15, 31)      # the runner's own "market hours" (runtime.market_open); the manual tools use the same edges


def after_close(now: datetime) -> bool:
    """Round 298: outside 09:15-15:31. Dhan squares INTRADAY positions off itself at the close and the chain often has no quotes afterwards, so a SELL sent now may land on a position that is already gone and open a short."""
    t = now.time()
    return t > MARKET_CLOSE or t < MARKET_OPEN


def carried_over(entry_day: str, now: datetime, holidays: Sequence[str] = ()) -> bool:
    """True for a position that was opened on an EARLIER trading day and is still open once today's market has opened (09:15 on a trading day). This service is intraday only,
    so such a position is a leftover (a square-off that failed, a service that was down at 15:15) and is closed at once instead of waiting for its stop (round 248)."""
    return str(entry_day) < now.date().isoformat() and is_trading_day(now.date(), holidays) and now.time() >= dtime(9, 15)


def _opposite(key: str) -> str:
    return key[:-2] + ("PE" if key.endswith("CE") else "CE") if key[-2:] in ("CE", "PE") else ""


def param_problems(p: Params) -> List[str]:
    """Round 252: hard sanity limits on the numbers that protect money. The Calibration page already keeps its switches inside the catalog bounds, but a value that comes in through the environment at boot
    (or a backtest override) is not bounded by anything. A stop of 150 % gives a stop price below zero (it can never fire); a trail of 0 or 100, a risk of 0 or over 100 %, a negative slippage all break the exits or the sizing.
    These are NOT calibration limits (those are tighter): they only refuse numbers that cannot work. An entry is refused with the reason; nothing is changed silently."""
    bad: List[str] = []
    if not (0 < p.sl_pct < 100):
        bad.append(f"stop loss {p.sl_pct:g} % must be between 0 and 100 (FNO_SL_PCT)")
    if p.sl_noise_atr_frac < 0 or p.sl_noise_atr_frac > 2:
        bad.append(f"stop noise floor {p.sl_noise_atr_frac:g} of a daily ATR must be between 0 and 2 (FNO_SL_NOISE_ATR_FRAC)")
    if p.sl_noise_atr_frac > 0 and not (0 < p.sl_max_pct < 100):
        bad.append(f"widest stop {p.sl_max_pct:g} % must be between 0 and 100 (FNO_SL_MAX_PCT)")
    if not p.target_pct > 0:
        bad.append(f"target {p.target_pct:g} % must be above 0 (FNO_TARGET_PCT)")
    if not p.trail_arm_pct > 0:
        bad.append(f"trail arm {p.trail_arm_pct:g} % must be above 0 (FNO_TRAIL_ARM_PCT)")
    if not (0 < p.trail_pct < 100):
        bad.append(f"trail distance {p.trail_pct:g} % must be between 0 and 100 (FNO_TRAIL_PCT)")
    if not (0 < p.risk_per_trade_pct <= 100):
        bad.append(f"risk per trade {p.risk_per_trade_pct:g} % must be above 0 and at most 100 (FNO_RISK_PER_TRADE_PCT)")
    if not (0 < p.max_daily_loss_pct <= 100):
        bad.append(f"daily loss limit {p.max_daily_loss_pct:g} % must be above 0 and at most 100 (FNO_MAX_DAILY_LOSS_PCT)")
    if not (0 <= p.slippage_pct < 50):
        bad.append(f"slippage {p.slippage_pct:g} % must be from 0 to under 50 (FNO_SLIPPAGE_PCT)")
    if p.max_lots < 1:
        bad.append(f"max lots {p.max_lots} must be at least 1 (FNO_MAX_LOTS)")
    # Round 284: the times must fit together. An entry window that closes at or after the square-off time opens a position that the same cycle closes again (two flat brokerages, a
    # spread paid, nothing earned); a window that ends before it starts never opens. An unreadable time falls back to the shipped default, as `parse_hhmm` does everywhere else.
    t_start, t_end = parse_hhmm(p.entry_start, dtime(9, 30)), parse_hhmm(p.entry_end, dtime(14, 30))
    t_off, t_xend, t_xoff = parse_hhmm(p.squareoff_at, dtime(15, 15)), parse_hhmm(p.expiry_entry_end, dtime(13, 30)), parse_hhmm(p.expiry_squareoff_at, dtime(14, 45))
    t_settle = settle_until(p)
    if t_settle is not None and t_settle > t_end:
        bad.append(f"settle time {p.entry_settle_min} min after {p.entry_start} runs to {t_settle:%H:%M}, past the last entry {p.entry_end}: nothing could ever be entered (FNO_ENTRY_SETTLE_MIN)")
    if t_start > t_end:
        bad.append(f"entry window opens {p.entry_start} after it closes {p.entry_end} (FNO_ENTRY_START / FNO_ENTRY_END)")
    if t_end >= t_off:
        bad.append(f"last entry {p.entry_end} must be before the square-off {p.squareoff_at}: a position opened then would be closed in the same cycle (FNO_ENTRY_END / FNO_SQUAREOFF_AT)")
    if t_xend >= min(t_off, t_xoff):
        bad.append(f"expiry-day last entry {p.expiry_entry_end} must be before the expiry-day square-off {p.expiry_squareoff_at} (FNO_EXPIRY_ENTRY_END / FNO_EXPIRY_SQUAREOFF_AT)")
    return bad


def settings_step(p: Params) -> Dict[str, Any]:
    """Round 290: the pipeline-check line for `param_problems`. A time (or a number) that came in through the environment at boot is not seen by the Calibration page's guard (round 286), so the engine
    refused every entry with the reason only in the skip log. This puts the same reasons where you look: ok False and the reasons in `detail`; ok True and the window in `detail`."""
    bad = param_problems(p)
    if bad:
        return {"step": "settings fit together", "ok": False, "detail": "the engine refuses every entry until this is fixed: " + "; ".join(bad)}
    return {"step": "settings fit together", "ok": True, "detail": f"entry {p.entry_start}-{p.entry_end}, square-off {p.squareoff_at}; expiry day entry until {p.expiry_entry_end}, out {p.expiry_squareoff_at}"}


def entry_gates(now: datetime, expiry: date, vix: Optional[float], chain_age_s: float, state: DayState, p: Params, key: str = "",
                holidays: Sequence[str] = (), cooldown_s: float = 900.0, strategy: str = "", expiry_strategy: bool = False, mode: str = "DEMO") -> Tuple[bool, List[str]]:
    """`expiry_strategy=True` (EXPIRY_MOMENTUM / EXPIRY_ALPHA, round 236) turns the expiry rules round: the contract MUST expire today, the window ends at
    `expiry_entry_end`, and in REAL mode `expiry_real_ok` must be on. Every other strategy is still refused on expiry day."""
    why: List[str] = list(param_problems(p))
    if not is_trading_day(now.date(), holidays):
        why.append("not a trading day")
    if expiry_strategy:
        if not in_entry_window(now, p, p.expiry_entry_end):
            why.append(f"outside the expiry-day entry window {p.entry_start}-{p.expiry_entry_end}")
    elif not in_entry_window(now, p):
        why.append(f"outside the entry window {p.entry_start}-{p.entry_end}")
    t_settle = settle_until(p)      # round 497 (E517): the first minutes after the window opens are the open's own noise; the window itself is reported above, this only adds the settle reason
    if t_settle is not None and parse_hhmm(p.entry_start, dtime(9, 30)) <= now.time() < t_settle:
        why.append(f"settling after the open: no entry until {t_settle:%H:%M} ({p.entry_settle_min} min after {p.entry_start}; FNO_ENTRY_SETTLE_MIN)")
    days = (expiry - now.date()).days
    if days < 0:
        why.append("expiry already passed")
    elif expiry_strategy:
        if days != 0:
            why.append("an expiry-day strategy only enters a contract that expires today")
        if str(mode).upper() == "REAL" and not p.expiry_real_ok:
            why.append("expiry-day entries are DEMO-only (FNO_EXPIRY_REAL is off)")
    elif days == 0 and p.block_expiry_day:
        why.append("expiry day: no new entries")
    elif days < p.min_days_to_expiry:
        why.append(f"{days} day(s) to expiry < minimum {p.min_days_to_expiry}")
    if chain_age_s > p.max_data_age_s:
        why.append(f"chain data {chain_age_s:.0f}s old > {p.max_data_age_s}s")
    if vix is None:
        why.append("India VIX unknown")
    elif vix > p.vix_max:
        why.append(f"India VIX {vix:.1f} > {p.vix_max:g}")
    if state.trades_today >= p.max_trades_per_day:
        why.append(f"{state.trades_today} trades today (max {p.max_trades_per_day})")
    elif p.early_trades_max > 0 and state.trades_today >= p.early_trades_max and now.time() < parse_hhmm(p.early_until, dtime(12, 0)):     # round 497 (E517): keep the rest of the day's cap for later
        why.append(f"{state.trades_today} trade(s) already taken before {p.early_until} (max {p.early_trades_max}): the other {p.max_trades_per_day - state.trades_today} of today's {p.max_trades_per_day} are kept for later (FNO_EARLY_TRADES_MAX)")
    if state.open_positions >= p.max_open_positions:
        why.append(f"{state.open_positions} positions open (max {p.max_open_positions})")
    if p.capital > 0 and state.realized_pnl_net <= -p.capital * p.max_daily_loss_pct / 100.0:
        why.append(f"daily loss limit reached ({state.realized_pnl_net:,.0f})")
    if key and state.open_same_side.get(key, 0) > 0:
        why.append(f"already holding {key}")
    if key and _opposite(key) and state.open_same_side.get(_opposite(key), 0) > 0:
        why.append(f"already holding the opposite side ({_opposite(key)}): two strategies must not hedge each other and pay twice")
    if key and p.max_same_dir_open > 0 and key[-2:] in ("CE", "PE"):       # round 495: NIFTY and SENSEX are one bet: count this side on every index (this key itself is refused above)
        side = key[-2:]
        held = sum(n for k, n in state.open_same_side.items() if k[-2:] == side and k != key)
        if held >= p.max_same_dir_open:
            why.append(f"{held} {side} position(s) already open on another index (max {p.max_same_dir_open} per side): NIFTY and SENSEX are one bet")
    if key and p.reentry_cooldown_min > 0 and key in state.last_exit_by_key:      # round 495: after ANY exit a fresh look is needed before the same index + side is bought again
        left = p.reentry_cooldown_min * 60.0 - (now.timestamp() - state.last_exit_by_key[key])
        if 0 < left:
            why.append(f"cooling down after the last {key} exit ({left:.0f}s left)")
    if strategy and strategy != "CORE" and state.entries_by_strategy.get(strategy, 0) >= p.strategy_max_per_day:
        why.append(f"{strategy} already entered {state.entries_by_strategy.get(strategy, 0)} time(s) today (max {p.strategy_max_per_day})")
    if state.last_stopout_ts is not None:
        since = now.timestamp() - state.last_stopout_ts
        if 0 <= since < cooldown_s:
            why.append(f"cooling down after a stop-out ({cooldown_s - since:.0f}s left)")
    return (not why), why
