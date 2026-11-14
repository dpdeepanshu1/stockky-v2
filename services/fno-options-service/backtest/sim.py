"""backtest/sim.py - SIMULATED intraday backtest: index candles in, option premiums from Black-Scholes. Real prices are NOT used (reliability: SIM).

Per day: from the entry window on, at the close of each intraday bar the SAME direction logic as live runs on the history known at that moment (daily candles up to YESTERDAY
plus today's bars so far: no look-ahead; the chain inputs are unavailable so chain score = 0), then the SAME gates, sizing, fill, stop / trail / target / square-off, and costs.
Contract: the strike nearest spot (ATM) of the next weekly expiry that is `min_days` away; IV = India VIX of that day (or iv_default); a synthetic bid-ask of `spread_pct` around theory.
The premium path inside a bar: CE premium rises with the spot, PE premium falls with it, so the bar's premium high / low come from the spot's low / high."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from fno import bs, expiry as xp, gates, paper, signals, sizing, strategies
from fno.models import ContractRef
from fno.params import Params
from . import metrics

IST = timezone(timedelta(hours=5, minutes=30))
STEP = {"NIFTY": 50.0, "SENSEX": 100.0}
LOT = {"NIFTY": 65, "SENSEX": 20}


def _d(t: float) -> date:
    return datetime.fromtimestamp(t, IST).date()


def check_bar_spacing(intraday: Sequence[dict], bar_minutes: int) -> None:
    """Round 303: the backtests time a bar as start + `bar_minutes` (the signal is taken at the bar's CLOSE, the gates and the square-off use that time). Index candles of another interval (a 15-minute
    or 1-minute file given to a 5-minute run) were accepted silently: the gates then saw a time 10 minutes before the bar had really closed, and a rolling run looked its option candles up at times that never matched.
    The MEDIAN gap inside a day is used, so a few holes in the data do not trip it."""
    by_day: Dict[date, List[float]] = {}
    for b in intraday:
        by_day.setdefault(_d(b["t"]), []).append(b["t"])
    gaps = sorted(y - x for ts in by_day.values() for x, y in zip(sorted(ts), sorted(ts)[1:]))
    if gaps and gaps[len(gaps) // 2] != bar_minutes * 60.0:
        raise ValueError(f"the index candles are {gaps[len(gaps) // 2] / 60:g}-minute bars but this backtest times them as {bar_minutes}-minute bars: pass index candles of {bar_minutes} minutes")


def _prem(spot: float, strike: float, ot: str, t_years: float, iv: float, r: float) -> float:
    return max(0.05, bs.price(spot, strike, t_years, r, iv, ot))


def run(underlying: str, daily: Sequence[dict], intraday: Sequence[dict], vix_by_day: Optional[Dict[str, float]] = None, p: Optional[Params] = None,
        iv_default: float = 0.14, spread_pct: float = 0.6, holidays: Sequence[str] = (), lot_size: Optional[int] = None, bar_minutes: int = 5,
        expiry_weekday: Optional[int] = None, start: Optional[str] = None, end: Optional[str] = None, strategy: str = "CORE") -> Dict[str, Any]:
    if strategy not in strategies.STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; one of {list(strategies.ORDER)}")
    p = p or Params()
    is_exp = strategies.is_expiry(strategy)                          # EXPIRY_* strategies: only on days when the weekly contract expires TODAY, half risk, 1 lot, out by 14:45
    pz = strategies.params_for(strategy, p)                          # sizing / stops for this strategy (== p for every other strategy)
    und = underlying.upper()
    lot = lot_size or LOT[und]
    step = STEP[und]
    vix_by_day = vix_by_day or {}
    check_bar_spacing(intraday, bar_minutes)
    by_day: Dict[date, List[dict]] = {}
    for b in sorted(intraday, key=lambda x: x["t"]):
        by_day.setdefault(_d(b["t"]), []).append(b)
    trades: List[paper.Position] = []
    days_run = 0
    first = last = None
    for day in sorted(by_day):
        if (start and day.isoformat() < start) or (end and day.isoformat() > end) or not gates.is_trading_day(day, holidays):
            continue
        bars = by_day[day]
        if len(bars) < 12:
            continue
        if is_exp and xp.next_weekly_expiry(und, day, holidays, expiry_weekday) != day:
            continue                                                 # not an expiry day: an expiry strategy has nothing to do
        days_run += 1
        first, last = first or day.isoformat(), day.isoformat()
        hist = [c for c in daily if _d(c["t"]) < day]
        dcache = signals.daily_score(hist)                          # the daily picture is fixed for the whole day: computed once
        vixv = vix_by_day.get(day.isoformat())
        iv = (vixv / 100.0) if vixv else iv_default                 # PRICING: the day's level (premiums need a volatility for the day)
        prev = [k for k in vix_by_day if k < day.isoformat()]
        vix_known = vix_by_day[max(prev)] if prev else None         # DECISIONS (VIX gate, strategy inputs): the last close BEFORE this day. The day's own close is not known at 09:30.
        exp = day if is_exp else xp.expiry_for_entry(und, day, p.min_days_to_expiry, p.block_expiry_day, holidays, expiry_weekday)
        state = gates.DayState()
        pos: Optional[paper.Position] = None
        for i, bar in enumerate(bars):
            end_dt = datetime.fromtimestamp(bar["t"], IST) + timedelta(minutes=bar_minutes)
            sq = gates.after_squareoff(end_dt, p, exp) or i == len(bars) - 1
            if pos is not None:                                     # manage on this bar
                t_y = bs.year_fraction(end_dt, exp)
                po, pc = _prem(bar["o"], pos.strike, pos.opt_type, t_y, iv, p.risk_free_rate), _prem(bar["c"], pos.strike, pos.opt_type, t_y, iv, p.risk_free_rate)
                ph, pl = ((_prem(bar["h"], pos.strike, "CE", t_y, iv, p.risk_free_rate), _prem(bar["l"], pos.strike, "CE", t_y, iv, p.risk_free_rate)) if pos.opt_type == "CE"
                          else (_prem(bar["l"], pos.strike, "PE", t_y, iv, p.risk_free_rate), _prem(bar["h"], pos.strike, "PE", t_y, iv, p.risk_free_rate)))
                hit = paper.evaluate_bar(pos, po, max(ph, po, pc), min(pl, po, pc), pc, p, squareoff=sq)
                if hit:
                    ref = hit[1] * (1 - spread_pct / 200.0)
                    fill = paper.sell_fill(ref, ref, p) or ref
                    paper.close_position(pos, fill, end_dt.timestamp(), hit[0], p)
                    trades.append(pos)
                    state.open_positions = 0
                    state.realized_pnl_net += pos.net_pnl or 0.0
                    if hit[0] == paper.SL:
                        state.last_stopout_ts = end_dt.timestamp()
                    if hit[0] != paper.SQUAREOFF:                          # round 495: the same re-entry pause after ANY exit as the live gate (per index + side)
                        state.last_exit_by_key[pos.key] = end_dt.timestamp()
                    pos = None
                continue
            if sq:
                continue
            state.entries_by_strategy = {strategy: state.trades_today}
            ok, _ = gates.entry_gates(end_dt, exp, vix_known if vix_known else 15.0, 0.0, state, p, holidays=holidays, strategy=strategy, expiry_strategy=is_exp, mode="SIM")
            if not ok:
                continue
            ctx = strategies.build_ctx(und, hist, bars[: i + 1], None, end_dt, p, holidays, vix_known, daily_cache=dcache, expiry_day=is_exp)
            b = strategies.evaluate(strategy, ctx)
            if b["direction"] == signals.FLAT:
                continue
            ot = "CE" if b["direction"] == signals.UP else "PE"
            if not gates.entry_gates(end_dt, exp, vix_known if vix_known else 15.0, 0.0, state, p, key=f"{und}:{ot}", holidays=holidays, strategy=strategy, expiry_strategy=is_exp, mode="SIM")[0]:
                continue                                                 # round 495: the per-side gates (re-entry pause) need the side, known only now
            spot = bar["c"]
            strike = round(spot / step) * step
            t_y = bs.year_fraction(end_dt, exp)
            theo = _prem(spot, strike, ot, t_y, iv, p.risk_free_rate)
            ask = theo * (1 + spread_pct / 200.0)
            if ask < p.min_premium:
                continue
            fill = paper.buy_fill(ask, ask, pz)
            pe = sizing.with_noise_stop(pz, fill, None, b.get("atr") or signals.daily_score(hist)[2]) if fill else pz      # round 498 (E518): the same noise floor as live (delta unknown here: at the money)
            lots = sizing.lots_for(fill, lot, pe) if fill else None      # round 250: size on the fill, as the live engine does
            if not lots or not fill:
                continue
            pos = paper.open_position(ContractRef(und, exp.isoformat(), strike, ot, lot, None, "", 0.05), lots, fill, end_dt.timestamp(), pe, mode="SIM", score=b["score"], spot=spot, strategy=strategy)
            pos.entry_ctx = signals.entry_extension(bars[: i + 1])      # round 509
            state.trades_today += 1
            state.open_positions = 1
    m = metrics.summarize(trades, p.capital)
    return {"kind": "SIM", "strategy": strategy, "underlying": und, "from": first, "to": last, "days": days_run, "capital": p.capital, "params": p.to_json(), "metrics": m,
            "verdict": metrics.verdict(m), "trades": [t.to_json() for t in trades],
            "notes": ["premiums are Black-Scholes at the day's India VIX (or the default IV); spread is synthetic; the VIX gate and the strategies see the PREVIOUS day's VIX close (the day's own close is not known at entry)", "the opposite-signal exit and the chain inputs of the live engine are not simulated (a named strategy's chain veto is off here)",
                      "weekly-expiry weekday follows fno/expiry.py; pass expiry_weekday for periods before the Sep-2025 change",
                      "an EXPIRY_* strategy is run only on days the weekly contract expires, on that same-day contract (Black-Scholes at the day's VIX), and its days count is those days only"]}
