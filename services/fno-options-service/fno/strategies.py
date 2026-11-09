"""fno/strategies.py - named entry strategies for index-option BUYING (CE or PE). Pure: candles and a chain summary in, a signal (or nothing) out. No clock of its own, no network.

WHERE THE NAMES COME FROM. Round 232 took the 12 algo NAMES from an Instagram ad ("MT Algo Portfolio", 8 Nifty + 4 Sensex). The ad shows only names: no rules, no trades, no
results. The rules below are OURS, written from what each name suggests, and are not a copy or a reconstruction of anyone's system. Nothing here is evidence that they make money.
Only the names that can be traded as a plain option BUY got a rule; the rest are listed in NOT_BUILT with the reason (see also docs / CHANGELOG round 232).

EVERY strategy: returns {"direction": UP | DOWN, "score", "reasons", "parts"} when its pattern completes on the latest COMPLETED intraday bar, otherwise None. UP buys a CALL, DOWN
buys a PUT. After that the SAME gates, contract choice, sizing, stop / trail / target, square-off and costs as CORE apply (engine.py); a strategy only decides WHEN and WHICH WAY.
Data rules shared by all of them (CORE is left exactly as it was):
  * only COMPLETED bars: a bar that is still forming at `now` is dropped (the SIM and the live engine then see the same thing);
  * the intraday bars must be fresh (the last completed bar ended within 4 bar-lengths of `now`), else no signal;
  * today's session only; "prior day" levels come from daily candles dated BEFORE the session day;
  * the chain can only VETO: a chain score of 15 or more against the direction cancels the signal (absent chain data never vetoes and never helps);
  * round 236: every window is written in MINUTES and turned into a bar count with the bars' own measured interval (`Ctx.nb`), so the rules are the same whether the feed gives 1-, 5- or
    15-minute bars (the opening range is the first 15 minutes, the weekly-opener range the first 30). With 5-minute bars nothing changed.
Expiry-day strategies (EXPIRY_MOMENTUM, EXPIRY_ALPHA; round 236) only run on a contract's expiry day (`Ctx.expiry_day`), the engine feeds them the chain that expires TODAY, and the gates, the
smaller size and the 14:45 square-off for them live in gates.py / params.py / engine.py. All other strategies are never given an expiry-day chain.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from functools import cached_property
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import analytics, gates, signals
from .params import Params

IST = timezone(timedelta(hours=5, minutes=30))
UP, DOWN, FLAT = signals.UP, signals.DOWN, signals.FLAT

# ── the catalogue ────────────────────────────────────────────────────────────────────────────────────────
# key -> (switch name in config / catalog, label, what the ad called it, one line of what OUR rule does)
CORE = "CORE"


def _d(t: float) -> date:
    return datetime.fromtimestamp(t, IST).date()


def _median(xs: Sequence[float]) -> float:
    ys = sorted(xs)
    return ys[len(ys) // 2] if ys else 0.0


def _vwap_series(bars: Sequence[Sequence[float]]) -> List[float]:
    """Running session VWAP at each bar (typical price weighted by volume; plain average when the index feed carries no volume)."""
    out: List[float] = []
    pv = vv = tp_sum = 0.0
    for k, b in enumerate(bars):
        tp = (b[2] + b[3] + b[4]) / 3.0
        pv += tp * b[5]
        vv += b[5]
        tp_sum += tp
        out.append(pv / vv if vv > 0 else tp_sum / (k + 1))
    return out


class Ctx:
    """Everything a strategy may look at, built once per (underlying, moment). Heavy parts are computed only when a strategy asks."""

    def __init__(self, und: str, daily_raw: Sequence[Any], intraday_raw: Sequence[Any], summary: Optional[Dict[str, Any]], now: datetime, p: Params,
                 holidays: Sequence[str] = (), vix: Optional[float] = None, daily_cache: Optional[Tuple[float, List[str], Optional[float]]] = None,
                 expiry_day: bool = False):
        self.und, self.now, self.p, self.holidays, self.vix = und, now, p, tuple(holidays), vix
        self.daily_raw, self.intraday_raw, self.summary = daily_raw, intraday_raw, summary
        self._daily_cache = daily_cache
        self.expiry_day = bool(expiry_day)          # True: the contract this context is for expires today (only EXPIRY_* strategies look at it)
        self.vetoes: List[Dict[str, Any]] = []      # signals the option chain cancelled (round 236: the SIM has no chain, so this is how live-vs-SIM can be measured)
        self.cur: str = ""                          # the strategy being evaluated (set by `candidates`)

    # CORE's own inputs (unchanged semantics: raw bars, forming bar included)
    @cached_property
    def daily_parts(self) -> Tuple[float, List[str], Optional[float]]:
        return self._daily_cache if self._daily_cache is not None else signals.daily_score(self.daily_raw)

    @property
    def d_score(self) -> float:
        return self.daily_parts[0]

    @property
    def atr(self) -> Optional[float]:
        return self.daily_parts[2]

    @cached_property
    def chain_parts(self) -> Tuple[float, List[str]]:
        return analytics.chain_score(self.summary) if self.summary else (0.0, [])

    @property
    def c_score(self) -> float:
        return self.chain_parts[0]

    # the clean bars the named strategies use
    @cached_property
    def all_bars(self) -> List[List[float]]:
        rows = signals.to_ms_bars(self.intraday_raw)
        if not rows:
            return []
        day = _d(rows[-1][0])
        return [r for r in rows if _d(r[0]) == day]

    @cached_property
    def interval(self) -> float:
        b = self.all_bars
        gaps = [b[k + 1][0] - b[k][0] for k in range(len(b) - 1) if b[k + 1][0] > b[k][0]]
        return _median(gaps) or 300.0

    def nb(self, minutes: float) -> int:
        """How many bars make `minutes` minutes with this feed's measured bar length (at least 1). 5-minute bars: nb(15) = 3, nb(30) = 6."""
        return max(1, int(round(minutes * 60.0 / self.interval)))

    @cached_property
    def bars(self) -> List[List[float]]:
        """Completed bars only: every bar that has not finished by `now` (the forming one, and anything dated after `now`) is dropped."""
        lim = self.now.timestamp() + 1e-6
        return [r for r in self.all_bars if r[0] + self.interval <= lim]

    @cached_property
    def stale(self) -> Optional[str]:
        if len(self.bars) < 3:
            return "too few completed intraday bars"
        end = self.bars[-1][0] + self.interval
        if self.now.timestamp() - end > 4 * self.interval:
            return "intraday bars are stale"
        return None

    @cached_property
    def session_day(self) -> Optional[date]:
        return _d(self.bars[-1][0]) if self.bars else None

    @cached_property
    def prior_day(self) -> Optional[Tuple[float, float, float]]:
        """(high, low, close) of the last daily candle dated before the session day."""
        if self.session_day is None:
            return None
        rows = [r for r in signals.to_ms_bars(self.daily_raw) if _d(r[0]) < self.session_day]
        return (rows[-1][2], rows[-1][3], rows[-1][4]) if rows else None

    @cached_property
    def day_range(self) -> float:
        """A typical daily range: the daily ATR, else the mean of the last 14 daily high-low ranges, else 0.8 % of the price."""
        if self.atr and self.atr > 0:
            return float(self.atr)
        rows = signals.to_ms_bars(self.daily_raw)[-14:]
        if rows:
            return sum(r[2] - r[3] for r in rows) / len(rows)
        return 0.008 * (self.bars[-1][4] if self.bars else 0.0)

    @cached_property
    def vwap(self) -> List[float]:
        return _vwap_series(self.bars)

    @cached_property
    def clean_intraday(self) -> Tuple[float, List[str]]:
        return signals.intraday_score(self.bars)

    @cached_property
    def week_opener(self) -> bool:
        """Is the session day the first trading day of its week (Monday, or the first day after a holiday-only run from Monday)?"""
        d0 = self.session_day
        if d0 is None:
            return False
        monday = d0 - timedelta(days=d0.weekday())
        return all(not gates.is_trading_day(monday + timedelta(days=k), self.holidays) for k in range(d0.weekday()))


def build_ctx(und: str, daily_raw: Sequence[Any], intraday_raw: Sequence[Any], summary: Optional[Dict[str, Any]], now: datetime, p: Params,
              holidays: Sequence[str] = (), vix: Optional[float] = None, daily_cache: Optional[Tuple[float, List[str], Optional[float]]] = None,
              expiry_day: bool = False) -> Ctx:
    return Ctx(und, daily_raw, intraday_raw, summary, now, p, holidays, vix, daily_cache, expiry_day)


# ── CORE ─────────────────────────────────────────────────────────────────────────────────────────────────
def core_bias(x: Ctx) -> Dict[str, Any]:
    """The original signal (fno/signals.py), unchanged: raw bars, summed score against p.min_score. Always returns a dict (direction FLAT when nothing fires); the engine also uses it
    to flip a held position and to show the bias on the Signals page, whether or not CORE entries are switched on."""
    i, iwhy = signals.intraday_score(x.intraday_raw) if x.intraday_raw else (0.0, [])
    d, dwhy, atr = x.daily_parts
    c, cwhy = x.chain_parts
    return signals.combine_bias(d, dwhy, atr, i, iwhy, c, cwhy, x.p)


# ── helpers shared by the named strategies ──────────────────────────────────────────────────────────────────
def _sig(x: Ctx, direction: str, base: float, why: List[str], parts: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Finish a signal: the chain may only VETO (>= 15 against); the daily trend and the chain nudge the score (it is a journal number, not a threshold)."""
    sgn = 1 if direction == UP else -1
    c = x.c_score
    if c * sgn <= -x.p.strat_chain_veto:
        x.vetoes.append({"strategy": x.cur, "direction": direction, "chain": round(c, 1), "base_score": base, "reasons": why[:2]})
        return None
    score = base
    d = x.d_score
    if abs(d) >= 20:
        score += 10 if d * sgn > 0 else -10
        why = why + [f"daily trend {'agrees' if d * sgn > 0 else 'is against'} ({d:+.0f})"]
    if c * sgn >= 10:
        score += 5
        why = why + [f"chain agrees ({c:+.0f})"]
    score = max(0.0, min(100.0, score))
    return {"direction": direction, "score": round(sgn * score, 1), "reasons": why, "parts": {"daily": round(d, 1), "chain": round(c, 1), **(parts or {})}}


def trend_rider(x: Ctx) -> Optional[Dict[str, Any]]:
    """TREND_RIDER (ad: IA-11, Nifty). Daily AND intraday must both lean the same way, each on its own (CORE only needs their sum): daily score >= 40 and the clean intraday score >= 15."""
    i, iwhy = x.clean_intraday
    d = x.d_score
    if d >= x.p.strat_trend_d and i >= x.p.strat_trend_i:
        return _sig(x, UP, 55.0, [f"daily trend strong up ({d:+.0f})", f"intraday agrees ({i:+.0f})"] + iwhy[:2])
    if d <= -x.p.strat_trend_d and i <= -x.p.strat_trend_i:
        return _sig(x, DOWN, 55.0, [f"daily trend strong down ({d:+.0f})", f"intraday agrees ({i:+.0f})"] + iwhy[:2])
    return None


def momentum_surge(x: Ctx) -> Optional[Dict[str, Any]]:
    """MOMENTUM_SURGE (ad: IA-8, Sensex). The latest completed bar is an expansion bar (range >= 1.5x the session median, body >= 60 % of the range, closing in the outer 30 %) that
    breaks BOTH the opening range and every earlier bar's high / low. With volume in the feed it must also be >= 1.3x the average. Not against a strong daily trend (|40|)."""
    b = x.bars
    n_or = x.nb(15)
    if len(b) < x.nb(40):
        return None
    orh, orl = max(r[2] for r in b[:n_or]), min(r[3] for r in b[:n_or])
    last, prev = b[-1], b[:-1]
    med = _median([r[2] - r[3] for r in prev])
    rng = last[2] - last[3]
    if med <= 0 or rng < x.p.strat_surge_range_mult * med or abs(last[4] - last[1]) < x.p.strat_surge_body * rng:
        return None
    vols = [r[5] for r in prev]
    if sum(vols) > 0 and last[5] < x.p.strat_surge_vol_mult * (sum(vols) / len(vols)):
        return None
    d = x.d_score
    if last[4] > max(r[2] for r in prev) and last[4] > orh and last[4] >= last[3] + x.p.strat_surge_close * rng and d > -x.p.strat_trend_d:
        return _sig(x, UP, 55.0, [f"surge bar up: range {rng:.0f} vs median {med:.0f}", "broke the opening range and the session high"])
    if last[4] < min(r[3] for r in prev) and last[4] < orl and last[4] <= last[3] + (1.0 - x.p.strat_surge_close) * rng and d < x.p.strat_trend_d:
        return _sig(x, DOWN, 55.0, [f"surge bar down: range {rng:.0f} vs median {med:.0f}", "broke the opening range and the session low"])
    return None


def bear_sweep(x: Ctx) -> Optional[Dict[str, Any]]:
    """BEAR_SWEEP (ad: IA-12, Sensex) - PUT only. A bar poked ABOVE a level (the prior-day high, else the opening-range high) by no more than 0.6 daily range (a sweep of stops, not a
    real breakout) within the last 6 bars; the LATEST bar is the first to close below that sweep bar's low, is bearish, and no bar after the sweep made a higher high. Skipped when the
    daily score is 50 or more (a strong uptrend sweeps and keeps going)."""
    b = x.bars
    n_or, n_back = x.nb(15), x.nb(30)
    if len(b) < n_back or x.d_score >= x.p.strat_sweep_max_d:
        return None
    n = len(b)
    refs: List[Tuple[str, float, int]] = []
    if x.prior_day:
        refs.append(("prior-day high", x.prior_day[0], 1))
    refs.append(("opening-range high", max(r[2] for r in b[:n_or]), n_or))
    last = b[-1]
    for name, ref, first_k in refs:
        for k in range(max(first_k, n - n_back), n - 1):
            s = b[k]
            if not (ref < s[2] <= ref + 0.6 * x.day_range):
                continue
            mid = b[k + 1:n - 1]
            if last[4] < s[3] and last[4] < last[1] and all(r[4] >= s[3] for r in mid) and all(r[2] <= s[2] for r in b[k + 1:]):
                return _sig(x, DOWN, 55.0, [f"swept the {name} {ref:g} (high {s[2]:g}) and fell back", f"closed below the sweep bar's low {s[3]:g} {n - 1 - k} bar(s) later"])
    return None


def bear_pivot(x: Ctx) -> Optional[Dict[str, Any]]:
    """BEAR_PIVOT (ad: IA-10, Nifty) - PUT only. Classic floor pivot of the prior day, P = (H + L + C) / 3. The latest bar is the FIRST to close below P (by 0.03 %) after the bar before
    it closed at or above; below the session VWAP; the daily score is not bullish (<= 15); and there is room: the close is at least 0.25 daily range above the first support S1 = 2P - H."""
    b = x.bars
    if len(b) < x.nb(20) or not x.prior_day or x.d_score > x.p.strat_pivot_max_d:
        return None
    h, l, c = x.prior_day
    pivot = (h + l + c) / 3.0
    s1 = 2 * pivot - h
    last, prev = b[-1], b[-2]
    if not (prev[4] >= pivot and last[4] < pivot - x.p.strat_pivot_break_pct / 100.0 * last[4]) or last[4] >= x.vwap[-1]:
        return None
    if last[4] - s1 < 0.25 * x.day_range:
        return None
    return _sig(x, DOWN, 50.0, [f"first close below the prior-day pivot {pivot:.0f}", f"below session VWAP; first support S1 {s1:.0f} is {last[4] - s1:.0f} away"])


def drift_edge(x: Ctx) -> Optional[Dict[str, Any]]:
    """DRIFT_EDGE (ad: IA-9, Nifty). A steady drift day: at least 10 of the last 12 closes on one side of the running VWAP and the VWAP itself sloping (>= 0.04 % over 6 bars). Enter on the
    RESUMPTION: one of the 3 bars before the latest touched the VWAP (within 0.08 %), and the latest closes beyond all three of their closes, on the drift side of the VWAP."""
    b, n = x.bars, len(x.bars)
    w, k_slope, k_win = x.nb(60), x.nb(30), x.nb(15)            # 12 closes, a 6-bar slope, the 3 bars before the latest (5-minute bars)
    if n < w + 2 or n < k_slope + 2:
        return None
    need = int(round(w * x.p.strat_drift_closes / 12.0))        # default 10 of 12
    sl = x.p.strat_drift_slope_pct / 100.0
    tc = x.p.strat_drift_touch_pct / 100.0
    vw = x.vwap
    last, win = b[-1], b[-(k_win + 1):-1]
    above = sum(1 for k in range(n - w, n) if b[k][4] > vw[k])
    below = sum(1 for k in range(n - w, n) if b[k][4] < vw[k])
    slope = (vw[-1] - vw[-(k_slope + 1)]) / last[4]
    if above >= need and slope >= sl and min(r[3] for r in win) <= vw[-1] * (1.0 + tc) and last[4] > max(r[4] for r in win) and last[4] > vw[-1]:
        return _sig(x, UP, 50.0, [f"{above}/{w} closes above a rising VWAP", "pulled back to the VWAP and resumed up"])
    if below >= need and slope <= -sl and max(r[2] for r in win) >= vw[-1] * (1.0 - tc) and last[4] < min(r[4] for r in win) and last[4] < vw[-1]:
        return _sig(x, DOWN, 50.0, [f"{below}/{w} closes below a falling VWAP", "pulled back to the VWAP and resumed down"])
    return None


def weekly_opener(x: Ctx) -> Optional[Dict[str, Any]]:
    """WEEKLY_OPENER (ad: IA-1, Sensex). Only on the first trading day of the week: the first close beyond the first 6 bars' range (30 minutes), in the direction of the daily trend
    (daily score >= +15 for a CALL, <= -15 for a PUT)."""
    if not x.week_opener:
        return None
    b = x.bars
    k30 = x.nb(30)
    if len(b) < k30 + 1:
        return None
    hi, lo = max(r[2] for r in b[:k30]), min(r[3] for r in b[:k30])
    last, prev = b[-1], b[-2]
    d = x.d_score
    if last[4] > hi and prev[4] <= hi and d >= x.p.strat_opener_d:
        return _sig(x, UP, 50.0, [f"first trading day of the week: broke the 30-minute high {hi:g}"])
    if last[4] < lo and prev[4] >= lo and d <= -x.p.strat_opener_d:
        return _sig(x, DOWN, 50.0, [f"first trading day of the week: broke the 30-minute low {lo:g}"])
    return None


def expiry_momentum(x: Ctx) -> Optional[Dict[str, Any]]:
    """EXPIRY_MOMENTUM (ad: IA-3, Nifty) - expiry day ONLY (the contract expires today). The first close beyond the opening 30-minute range, on an expansion bar (range >= 1.3x the session
    median, body >= half of it), on the right side of the session VWAP, and not against a strong daily trend (|40|). Same exits as everything else, but half the risk, one lot and out by
    14:45 (gates / params). Rule is OURS from the name only; unproven, DEMO only."""
    if not x.expiry_day:
        return None
    b, k30 = x.bars, x.nb(30)
    if len(b) < k30 + 2:
        return None
    hi, lo = max(r[2] for r in b[:k30]), min(r[3] for r in b[:k30])
    last, prev = b[-1], b[-2]
    med = _median([r[2] - r[3] for r in b[:-1]])
    rng = last[2] - last[3]
    if med <= 0 or rng < x.p.strat_expiry_range_mult * med or abs(last[4] - last[1]) < x.p.strat_expiry_body * rng:
        return None
    vw, d = x.vwap[-1], x.d_score
    if last[4] > hi and prev[4] <= hi and last[4] > vw and d > -x.p.strat_trend_d:
        return _sig(x, UP, 50.0, [f"expiry day: first close above the 30-minute high {hi:g}", f"expansion bar {rng:.0f} vs median {med:.0f}, above VWAP"])
    if last[4] < lo and prev[4] >= lo and last[4] < vw and d < x.p.strat_trend_d:
        return _sig(x, DOWN, 50.0, [f"expiry day: first close below the 30-minute low {lo:g}", f"expansion bar {rng:.0f} vs median {med:.0f}, below VWAP"])
    return None


def expiry_alpha(x: Ctx) -> Optional[Dict[str, Any]]:
    """EXPIRY_ALPHA (ad: IA-5, Sensex) - expiry day ONLY. A trend-day CONTINUATION: the day has already moved (session range >= 0.5 daily range), then the last 30 minutes were a tight pause
    (range <= 0.4 of the session range) and the latest bar closes beyond the whole session's high (CALL) or low (PUT) with a body in that direction; not against a strong daily trend.
    At least 90 minutes of bars are needed. Rule is OURS from the name only; unproven, DEMO only."""
    if not x.expiry_day:
        return None
    b, k30 = x.bars, x.nb(30)
    if len(b) < 3 * k30:
        return None
    base, last = b[:-1], b[-1]
    hi, lo = max(r[2] for r in base), min(r[3] for r in base)
    full = hi - lo
    pause = b[-(k30 + 1):-1]
    prange = max(r[2] for r in pause) - min(r[3] for r in pause)
    if full <= 0 or full < x.p.strat_alpha_move * x.day_range or prange > x.p.strat_alpha_pause * full:
        return None
    d = x.d_score
    if last[4] > hi and last[4] > last[1] and d > -x.p.strat_trend_d:
        return _sig(x, UP, 50.0, [f"expiry day: a {prange:.0f}-point pause after a {full:.0f}-point move, then a close above the session high {hi:g}"])
    if last[4] < lo and last[4] < last[1] and d < x.p.strat_trend_d:
        return _sig(x, DOWN, 50.0, [f"expiry day: a {prange:.0f}-point pause after a {full:.0f}-point move, then a close below the session low {lo:g}"])
    return None


# ── registry ─────────────────────────────────────────────────────────────────────────────────────────────
STRATEGIES: Dict[str, Dict[str, Any]] = {
    "CORE": {"flag": "FNO_STRAT_CORE", "label": "Core signal", "ad": None, "sides": "CE / PE", "fn": None,
             "rule": "Original signal: daily structure + trend + intraday VWAP / opening range / EMAs + option chain, summed to a score of 45 or more."},
    "TREND_RIDER": {"flag": "FNO_STRAT_TREND_RIDER", "label": "Trend Rider", "ad": "IA-11 Trend Rider (Nifty)", "sides": "CE / PE", "fn": trend_rider,
                    "rule": "Daily trend score >= 40 AND the intraday score >= 15 on the same side (both must agree, no summing)."},
    "MOMENTUM_SURGE": {"flag": "FNO_STRAT_MOMENTUM_SURGE", "label": "Momentum Surge", "ad": "IA-8 Momentum Surge (Sensex)", "sides": "CE / PE", "fn": momentum_surge,
                       "rule": "An expansion bar (>= 1.5x median range, strong body) that breaks the opening range and the session high / low."},
    "BEAR_SWEEP": {"flag": "FNO_STRAT_BEAR_SWEEP", "label": "Bear Sweep", "ad": "IA-12 Bear Sweep (Sensex)", "sides": "PE only", "fn": bear_sweep,
                   "rule": "Stops swept above the prior-day / opening-range high, then the first close below the sweep bar's low."},
    "BEAR_PIVOT": {"flag": "FNO_STRAT_BEAR_PIVOT", "label": "Bear Pivot", "ad": "IA-10 Bear Pivot (Nifty)", "sides": "PE only", "fn": bear_pivot,
                   "rule": "First close below the prior-day floor pivot, under VWAP, daily not bullish, room to the first support."},
    "DRIFT_EDGE": {"flag": "FNO_STRAT_DRIFT_EDGE", "label": "Drift Edge", "ad": "IA-9 Drift Edge (Nifty)", "sides": "CE / PE", "fn": drift_edge,
                   "rule": "Steady drift day (10 of 12 closes on one side of a sloping VWAP): enter when a pullback to VWAP resumes."},
    "WEEKLY_OPENER": {"flag": "FNO_STRAT_WEEKLY_OPENER", "label": "Weekly Opener", "ad": "IA-1 Weekly Opener (Sensex)", "sides": "CE / PE", "fn": weekly_opener,
                      "rule": "First trading day of the week only: first close beyond the 30-minute opening range, with the daily trend."},
}
STRATEGIES["EXPIRY_MOMENTUM"] = {"flag": "FNO_STRAT_EXPIRY_MOMENTUM", "label": "Expiry Momentum", "ad": "IA-3 Expiry Momentum (Nifty)", "sides": "CE / PE", "fn": expiry_momentum, "expiry": True,
                                 "rule": "EXPIRY DAY ONLY (DEMO): first close beyond the 30-minute range on an expansion bar, with VWAP. Half risk, 1 lot, entry by 13:30, out by 14:45."}
STRATEGIES["EXPIRY_ALPHA"] = {"flag": "FNO_STRAT_EXPIRY_ALPHA", "label": "Expiry Alpha", "ad": "IA-5 Expiry Alpha (Sensex)", "sides": "CE / PE", "fn": expiry_alpha, "expiry": True,
                              "rule": "EXPIRY DAY ONLY (DEMO): after a big move and a tight 30-minute pause, a close beyond the session high / low. Half risk, 1 lot, entry by 13:30, out by 14:45."}
ORDER: Tuple[str, ...] = tuple(STRATEGIES)

# The ad's other four names: NOT built, DECIDED in round 236 (research in docs/FNO_DECISIONS_ROUND236.md). Inferred from the NAME only: the ad does not say what they do.
NOT_BUILT: List[Dict[str, str]] = [
    {"ad": "IA-4 Premium Harvester (Nifty)", "why": "DECIDED: never. The name says SELLING premium: unlimited-risk naked legs, margin blocks, and a gap or a spike can lose more than the account. This system only BUYS options."},
    {"ad": "IA-2 Contra Shield (Nifty)", "why": "DECIDED: later (phase 2). A 'shield' is a hedged structure (a debit spread). Needs two-leg orders, leg-risk on part fills and a margin check, none of which the single-leg REAL path has. Revisit after 1 lot REAL has run clean."},
    {"ad": "IA-6 Expiry Guard (Nifty)", "why": "DECIDED: later (phase 2), together with Contra Shield: a protective expiry-day structure = expiry-day entries (now built, DEMO) plus a hedge leg (not built)."},
    {"ad": "IA-7 Range Edge (Nifty)", "why": "DECIDED: not built. A range is usually traded by SELLING premium; buying a fade at the range edge pays theta both ways and had no support in the research. Revisit only if DEMO shows range days that the others lose on."},
]


def flag_of(key: str) -> str:
    return STRATEGIES[key]["flag"]


def is_expiry(key: str) -> bool:
    return bool(STRATEGIES.get(key, {}).get("expiry"))


def enabled_keys(cfg: Any, expiry: Optional[bool] = None) -> List[str]:
    """The switched-on strategies, in registry order. Read from `cfg` at call time (the Calibration page changes them live). CORE defaults ON, the rest OFF.
    `expiry=False` leaves out the expiry-day strategies, `expiry=True` returns only them, None returns all."""
    return [k for k in ORDER if bool(getattr(cfg, flag_of(k), k == CORE)) and (expiry is None or is_expiry(k) == expiry)]


def params_for(key: str, p: Params) -> Params:
    """The Params an entry of strategy `key` is sized with: an expiry-day strategy gets `expiry_risk_mult` x the risk per trade and at most one lot (gamma risk)."""
    if not is_expiry(key):
        return p
    return p.with_(risk_per_trade_pct=p.risk_per_trade_pct * p.expiry_risk_mult, max_lots=min(int(p.max_lots), 1))


def candidates(x: Ctx, keys: Sequence[str], core: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """The signals that fire right now, in registry order, each tagged with its strategy. Named strategies are skipped on stale / thin intraday data."""
    out: List[Dict[str, Any]] = []
    for k in keys:
        if k == CORE:
            b = core if core is not None else core_bias(x)
            if b["direction"] != FLAT:
                out.append({**b, "strategy": CORE})
            continue
        spec = STRATEGIES.get(k)
        if not spec or x.stale:
            continue
        x.cur = k
        try:
            s = spec["fn"](x)
        except Exception:  # noqa: BLE001 - one broken strategy must never stop the others or the cycle
            s = None
        if s:
            out.append({**s, "strategy": k})
    return out


def evaluate(key: str, x: Ctx) -> Dict[str, Any]:
    """One strategy as a bias dict (direction FLAT when it does not fire): what the backtests use."""
    if key == CORE:
        return core_bias(x)
    hit = candidates(x, [key])
    return hit[0] if hit else {"direction": FLAT, "score": 0.0, "reasons": [], "parts": {}, "strategy": key}
