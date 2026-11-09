"""backtest/rolling_sim.py - intraday backtest on REAL option premiums from Dhan's expired-options (rolling) candles (round 246, plan item C1). Reliability: REAL prices, 5-minute path.

Same flow as backtest/sim.py (same direction logic on the history known at that moment, same gates, sizing, stop / trail / target / square-off, same costs); only the premium differs:
sim.py prices options with Black-Scholes, here every premium is a recorded candle of the contract actually traded.

  signal at the CLOSE of bar i (index candles up to bar i, daily candles up to yesterday: no look-ahead)  ->  fill at the OPEN of bar i+1 of the ATM contract of that moment (+ spread)
  the strike then stays FIXED: its later candles are found by strike among every fetched label (ATM, ATM+1, ATM-1 ...), because the "ATM" series itself moves with the spot.

Honest limits (also written into the result's notes):
  * the fetched labels must cover the strikes the trade drifts to; a bar with no candle for the held strike is not managed, and a position that cannot be priced at the square-off is closed at its last known
    candle and counted in `unpriced_exits` (a high count means: fetch more strikes);
  * Dhan serves one expiry code per file: pass the code that matches the entry rule (min_days_to_expiry); the expiry DATE is not in the reply, so the date used for the gates comes from fno/expiry.py;
  * a candle with no volume at the open is treated as tradable (the reply may not carry volume for every bar); thin far-out strikes can fill worse than the candle open;
  * the files are refused when `complete` is false (a window failed in scripts/fno_rolling_fetch.py): a series with a hole must not look like a result.
NOT run on real data yet: timestamps are assumed to be the START of the bar, like the index candles (check with the one-week fetch).
"""
from __future__ import annotations

import glob
import json
import os
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fno import expiry as xp, gates, paper, signals, sizing, strategies
from fno.models import ContractRef
from fno.params import Params
from . import metrics

IST = timezone(timedelta(hours=5, minutes=30))
STEP = {"NIFTY": 50.0, "SENSEX": 100.0}
LOT = {"NIFTY": 65, "SENSEX": 20}
MAX_UNMANAGED_PCT = 5.0        # round 258: more held bars than this without a candle for the held strike -> the verdict cannot be better than NOT_PROVEN
MAX_UNPRICED_PCT = 10.0
MAX_VIX_UNKNOWN_PCT = 10.0     # round 260: more tested days than this with no usable India VIX (none, or older than VIX_STALE_DAYS) -> the VIX gate was not applied on them -> NOT_PROVEN
VIX_STALE_DAYS = 5             # the last VIX close before a day may be at most this many calendar days old (a long weekend plus a holiday fits)


def _d(t: float) -> date:
    return datetime.fromtimestamp(t, IST).date()


class RollingBook:
    """Every fetched candle, found by (CE/PE, strike, bar start) and, for the entry, by (CE/PE, bar start) among the ATM series."""

    def __init__(self) -> None:
        self.by_strike: Dict[Tuple[str, float, float], Dict[str, Any]] = {}
        self.atm: Dict[Tuple[str, float], Dict[str, Any]] = {}
        self.files: List[str] = []
        self.labels: set = set()                                # round 258: the strike labels fetched (ATM, ATM+1 ...)
        self.interval = 5

    def add(self, doc: Dict[str, Any], name: str = "") -> None:
        ot = "CE" if doc.get("type") == "CALL" else "PE"
        self.interval = int(doc.get("interval") or self.interval)
        for r in doc.get("rows") or []:
            k = r.get("strike")
            if k is None or r.get("t") is None:
                continue
            self.by_strike[(ot, float(k), float(r["t"]))] = r
            if doc.get("strike") == "ATM":
                self.atm[(ot, float(r["t"]))] = r
        self.files.append(name)
        if doc.get("strike"):
            self.labels.add(str(doc["strike"]))

    def candle(self, ot: str, strike: float, t: float) -> Optional[Dict[str, Any]]:
        return self.by_strike.get((ot, float(strike), float(t)))

    def entry_row(self, ot: str, t: float) -> Optional[Dict[str, Any]]:
        return self.atm.get((ot, float(t)))


def load_book(folder: str, underlying: str = "NIFTY", interval: int = 5, expiry_flag: str = "WEEK", expiry_code: int = 1, allow_partial: bool = False) -> RollingBook:
    """Read the files scripts/fno_rolling_fetch.py wrote. A file with complete=false (or the .partial copy) is refused unless allow_partial."""
    pat = os.path.join(folder, f"{underlying.upper()}_*_{expiry_flag}{expiry_code}_{interval}m.json")
    paths = sorted(glob.glob(pat))
    if not paths:
        raise FileNotFoundError(f"no rolling files match {pat}: run scripts/fno_rolling_fetch.py first (with --strikes ATM at least)")
    book = RollingBook()
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        if doc.get("complete") is not True and not allow_partial:
            raise ValueError(f"{os.path.basename(path)} is not complete (failed windows: {len(doc.get('failed_windows') or [])}); fetch it again, or pass allow_partial to accept the holes")
        book.add(doc, os.path.basename(path))
    have = {k[0] for k in book.atm}
    if have != {"CE", "PE"}:
        raise ValueError("need the ATM series for BOTH CALL and PUT (the entry is the ATM contract of the moment)")
    return book


def run(underlying: str, daily: Sequence[dict], intraday: Sequence[dict], book: RollingBook, vix_by_day: Optional[Dict[str, float]] = None, p: Optional[Params] = None,
        spread_pct: float = 0.6, holidays: Sequence[str] = (), lot_size: Optional[int] = None, expiry_weekday: Optional[int] = None, start: Optional[str] = None,
        end: Optional[str] = None, strategy: str = "CORE") -> Dict[str, Any]:
    if strategy not in strategies.STRATEGIES:
        raise ValueError(f"unknown strategy {strategy!r}; one of {list(strategies.ORDER)}")
    p = p or Params()
    is_exp = strategies.is_expiry(strategy)
    pz = strategies.params_for(strategy, p)
    und = underlying.upper()
    lot = lot_size or LOT[und]
    bar_min = book.interval
    vix_by_day = vix_by_day or {}
    by_day: Dict[date, List[dict]] = {}
    for b in sorted(intraday, key=lambda x: x["t"]):
        by_day.setdefault(_d(b["t"]), []).append(b)
    covered = {_d(k[1]) for k in book.atm}                     # round 256: the days the fetched ATM series has any candle for
    uncovered: List[str] = []
    trades: List[paper.Position] = []
    days_run = skipped_no_quote = unpriced_exits = skipped_bars = held_bars = vix_unknown_days = 0
    first = last = None
    for day in sorted(by_day):
        if (start and day.isoformat() < start) or (end and day.isoformat() > end) or not gates.is_trading_day(day, holidays):
            continue
        bars = by_day[day]
        if len(bars) < 12:
            continue
        if is_exp and xp.next_weekly_expiry(und, day, holidays, expiry_weekday) != day:
            continue
        if day not in covered:                                  # round 256: nothing was fetched for this day (outside the fetched range, a holiday, or a hole): it was not tested, so it is not a tested day
            uncovered.append(day.isoformat())
            continue
        days_run += 1
        first, last = first or day.isoformat(), day.isoformat()
        hist = [c for c in daily if _d(c["t"]) < day]
        dcache = signals.daily_score(hist)
        prev = [k for k in vix_by_day if k < day.isoformat()]
        vix_known = vix_by_day[max(prev)] if prev else None
        if vix_known is None or (day - date.fromisoformat(max(prev))).days > VIX_STALE_DAYS:       # round 260: the live engine refuses to trade on an unknown VIX; here a missing value is read as 15 below, so count the day
            vix_unknown_days += 1
        exp = day if is_exp else xp.expiry_for_entry(und, day, p.min_days_to_expiry, p.block_expiry_day, holidays, expiry_weekday)
        state = gates.DayState()
        pos: Optional[paper.Position] = None
        last_c: Optional[float] = None
        for i, bar in enumerate(bars):
            end_dt = datetime.fromtimestamp(bar["t"], IST) + timedelta(minutes=bar_min)
            sq = gates.after_squareoff(end_dt, p, exp) or i == len(bars) - 1
            if pos is not None:
                held_bars += 1
                row = book.candle(pos.opt_type, pos.strike, bar["t"])
                if row is None or None in (row.get("o"), row.get("h"), row.get("l")):
                    if sq:                                              # cannot be priced at the square-off: close at the last known candle, and say so
                        ref = (last_c if last_c is not None else pos.entry_premium) * (1 - spread_pct / 200.0)
                        paper.close_position(pos, paper.sell_fill(ref, ref, p) or ref, end_dt.timestamp(), paper.SQUAREOFF, p)
                        unpriced_exits += 1
                        trades.append(pos)
                        state.open_positions = 0
                        pos = None
                    else:
                        skipped_bars += 1
                    continue
                last_c = row["c"]
                hit = paper.evaluate_bar(pos, row["o"], max(row["h"], row["o"], row["c"]), min(row["l"], row["o"], row["c"]), row["c"], p, squareoff=sq)
                if hit:
                    ref = hit[1] * (1 - spread_pct / 200.0)
                    paper.close_position(pos, paper.sell_fill(ref, ref, p) or ref, end_dt.timestamp(), hit[0], p)
                    trades.append(pos)
                    state.open_positions = 0
                    state.realized_pnl_net += pos.net_pnl or 0.0
                    if hit[0] == paper.SL:
                        state.last_stopout_ts = end_dt.timestamp()
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
            nxt = bars[i + 1] if i + 1 < len(bars) else None
            erow = book.entry_row(ot, nxt["t"]) if nxt else None          # the fill is at the OPEN of the NEXT bar: the signal needed this bar's close
            if erow is None or erow.get("o") is None or erow.get("strike") is None or erow["o"] <= 0:
                skipped_no_quote += 1
                continue
            ask = erow["o"] * (1 + spread_pct / 200.0)
            if ask < p.min_premium:
                continue
            fill = paper.buy_fill(ask, ask, pz)
            lots = sizing.lots_for(fill, lot, pz) if fill else None      # round 250: size on the fill, as the live engine does
            if not lots or not fill:
                continue
            pos = paper.open_position(ContractRef(und, exp.isoformat(), float(erow["strike"]), ot, lot, None, "", 0.05), lots, fill, float(nxt["t"]), pz, mode="SIM", score=b["score"],
                                      spot=bar["c"], strategy=strategy)
            state.trades_today += 1
            state.open_positions = 1
            # the entry bar itself is managed on the next loop pass (its own candle, entered at its open)
    m = metrics.summarize(trades, p.capital)
    unmanaged_pct = round(100.0 * skipped_bars / held_bars, 1) if held_bars else 0.0
    unpriced_pct = round(100.0 * unpriced_exits / len(trades), 1) if trades else 0.0
    dq: List[str] = []                                                  # round 258: a held strike with no candle is not managed (no stop, no target): the result then flatters the strategy
    if unmanaged_pct > MAX_UNMANAGED_PCT:
        dq.append(f"the held strike had no candle on {unmanaged_pct}% of held bars (limit {MAX_UNMANAGED_PCT}%): stops / targets were not applied on those bars, so the result is too good to trust; fetch more strikes (ATM+-1, +-2)")
    if unpriced_pct > MAX_UNPRICED_PCT:
        dq.append(f"{unpriced_pct}% of exits had no candle at the square-off and used the last known price (limit {MAX_UNPRICED_PCT}%)")
    vix_unknown_pct = round(100.0 * vix_unknown_days / days_run, 1) if days_run else 0.0
    if vix_unknown_pct > MAX_VIX_UNKNOWN_PCT:                          # round 260: without a VIX the gate (FNO_VIX_MAX) never blocks, so the high-volatility days are traded and the result flatters the strategy
        dq.append(f"India VIX was unknown or older than {VIX_STALE_DAYS} days on {vix_unknown_pct}% of the tested days (limit {MAX_VIX_UNKNOWN_PCT}%): the VIX gate was not applied on them (a missing value counts as 15), so volatile days were traded; pass --vix with a daily close for the whole range")
    verdict = metrics.verdict(m)
    if dq:
        verdict = {"status": "NOT_PROVEN", "reasons": dq + [x for x in verdict["reasons"] if not x.startswith("positive net")]}
    return {"kind": "ROLLING", "strategy": strategy, "underlying": und, "from": first, "to": last, "days": days_run, "capital": p.capital, "params": p.to_json(), "metrics": m,
            "verdict": verdict, "data_quality": {"held_bars": held_bars, "unmanaged_bars": skipped_bars, "unmanaged_pct": unmanaged_pct, "unpriced_exit_pct": unpriced_pct, "vix_unknown_days": vix_unknown_days, "vix_unknown_pct": vix_unknown_pct, "labels": sorted(book.labels), "ok": not dq}, "trades": [t.to_json() for t in trades], "unpriced_exits": unpriced_exits, "uncovered_days": uncovered, "skipped_no_quote": skipped_no_quote, "unmanaged_bars": skipped_bars,
            "notes": ["premiums are Dhan's recorded expired-option candles (real prices, 5-minute path); the spread is still synthetic (no bid / ask in the data)",
                      "entry = the open of the bar AFTER the signal, in the ATM contract of that moment; the strike is then held and its candles are found by strike",
                      f"expiry code of the files vs the entry rule (min_days_to_expiry={p.min_days_to_expiry}) must match: the expiry date is not in the reply",
                      f"{len(uncovered)} trading day(s) of the index data had no fetched option candle at all and were NOT tested" + (f" (first: {uncovered[:3]})" if uncovered else ""),
                      f"{skipped_no_quote} signal(s) had no ATM candle at the fill bar; {skipped_bars} bar(s) of a held strike had no candle; {unpriced_exits} exit(s) closed at the last known candle",
                      f"{vix_unknown_days} of {days_run} tested day(s) had no usable India VIX (none before the day, or older than {VIX_STALE_DAYS} days); the VIX gate was not applied on them",
                      "timestamps assumed to be the START of each bar: check with the one-week fetch before trusting a result",
                      "the opposite-signal exit and the chain inputs of the live engine are not simulated"]}
