"""timemachine/engine.py - E111: the pure core of the Time Machine. Stdlib only, no network, no database, no clock.

Question it answers: if a trade is opened at a given time of day with a given stop % and target % (LONG or SHORT) and must be flat before the square-off,
how often does the target come first, and what is the average result in R after charges and slippage?  Two independent answers are given:

  HISTORY     the exact rule replayed on every real recent session of this stock (5-minute bars): what really happened, day by day.
  SIMULATION  thousands of made-up sessions. Each synthetic session is built slot by slot (09:15, 09:20 ... 15:25): for every slot one bar SHAPE
              (gap from the previous close, high, low and close relative to its own open) is drawn at random from the recent sessions at that
              SAME time of day, then chained. It keeps this stock's time-of-day volatility (busy open and close, quiet middle) but forgets any
              trend or reversal inside a day. The same rule is walked over every synthetic session.

Reading the two together: if HISTORY beats SIMULATION by a wide margin the stock has recently trended or reversed inside the day more than a coin flip
would (the rule has something to work with, or got lucky); if they agree the result is mostly the stock's volatility and the break-even arithmetic.
Break-even aware: a target of RR times the stop only has to be reached 1 time in (1 + RR) (2:1 needs 33%), and charges raise that a little.

Fixed conventions (also in LIMITS, shown to the person): the entry fills at the OPEN of the entry bar; a bar that touches both the stop and the target counts as the
STOP (the cautious call); the stop and target fill at their levels (no gap-through; slippage is added for market legs); a trade still open at the flat-by
time is closed at the close of the last bar before it. Entry slots at or after the flat-by time are not tested.
Honest limits: ~1-2 months of one market mood, one stock, overlapping entry times are not independent, no overnight gaps and no volatility regime in the
simulation, no order-book or queue effects, fixed % levels (not Stockky's ATR levels). A reality check and a filter, never a forecast.
"""
from __future__ import annotations

import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

Bar = Tuple[int, float, float, float, float]            # (minute of day, open, high, low, close)

SLOT_MIN = 5
OPEN_MIN = 9 * 60 + 15                                   # 09:15
CLOSE_MIN = 15 * 60 + 30                                 # 15:30 (exclusive)
N_SLOTS = (CLOSE_MIN - OPEN_MIN) // SLOT_MIN             # 75
MIN_SESSION_BARS = 70                                    # a session with fewer 5-minute bars is half-empty and not used
MIN_HISTORY_SESSIONS = 10                                # fewer real sessions: the history side is shown but cannot vote
MARGIN_R_SIM = 0.05                                      # |average net R| within this = "near break-even" on the simulation
MARGIN_R_HIST = 0.10                                     # the history is noisier, so its band is wider
Z95 = 1.96

LIMITS = (
    "Entry fills at the open of the entry bar; a bar that touches both stop and target counts as the stop.",
    "Stop and target fill at their levels (no gap-through); slippage is charged on market legs (entry, stop, flat-by exit), not on the target.",
    "The simulation draws each time-of-day slot independently: it keeps the stock's busy-open, quiet-middle volatility but no trend or reversal within a day, and no overnight gap.",
    "Real history is one to two months of one market mood; entry times on the same stock-day are not independent.",
    "Fixed percent levels, not Stockky's ATR levels; no order-book, queue or circuit effects. A reality check, never a forecast.",
)


# ------------------------------------------------------------------ small helpers

def hhmm(s: str) -> int:
    """'09:45' -> minute of day. Raises ValueError on anything else."""
    parts = str(s).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"bad time {s!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"bad time {s!r}")
    return h * 60 + m


def fmt(mod: int) -> str:
    return f"{mod // 60:02d}:{mod % 60:02d}"


def slot_of(mod: int) -> int:
    return (mod - OPEN_MIN) // SLOT_MIN


def wilson(k: int, n: int, z: float = Z95) -> Tuple[Optional[float], Optional[float]]:
    """95% Wilson interval for k hits in n tries; (None, None) when n is 0."""
    if n <= 0:
        return None, None
    p = k / n
    d = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, centre - half), min(1.0, centre + half)


def quantile(sorted_vals: Sequence[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    i = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[i]


# ------------------------------------------------------------------ data shaping

def parse_candles(candles: Any) -> Dict[str, List[Bar]]:
    """market-data candle rows ({"date": "YYYY-MM-DD HH:MM" IST, open, high, low, close}) -> {day: [bars in time order]}, 09:15 to 15:30 only.
    Bad rows (unreadable, non-positive, high below low, not finite, off the 5-minute grid) are dropped. Pure."""
    days: Dict[str, Dict[int, Bar]] = {}
    for c in candles or []:
        try:
            ds = str(c.get("date"))[:16]
            day, hm = ds[:10], ds[11:16]
            mod = hhmm(hm)
            o, h, l, cl = (float(c[k]) for k in ("open", "high", "low", "close"))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if not (OPEN_MIN <= mod < CLOSE_MIN) or (mod - OPEN_MIN) % SLOT_MIN:
            continue
        if any(not math.isfinite(x) for x in (o, h, l, cl)) or not (o > 0 and h > 0 and l > 0 and cl > 0) or h < l:
            continue
        days.setdefault(day, {})[mod] = (mod, o, h, l, cl)
    return {d: [b[k] for k in sorted(b)] for d, b in sorted(days.items())}


def complete_sessions(days: Dict[str, List[Bar]], min_bars: int = MIN_SESSION_BARS) -> Dict[str, List[Bar]]:
    """Only sessions with enough 5-minute bars (today's half-finished session and half-day sessions drop out)."""
    return {d: b for d, b in days.items() if len(b) >= min_bars}


# ------------------------------------------------------------------ the rule and one walk

class Rule:
    """One trade rule. `charges` is the round-trip charge as % of the entry value for each way a trade can end: {"target": x, "stop": y, "flat": z}."""
    __slots__ = ("side", "stop_pct", "target_pct", "flat_by", "slippage_pct", "charges")

    def __init__(self, side: str, stop_pct: float, target_pct: float, flat_by: int, slippage_pct: float = 0.0,
                 charges: Optional[Dict[str, float]] = None):
        s = str(side or "").upper()
        if s not in ("LONG", "SHORT"):
            raise ValueError("side must be LONG or SHORT")
        if not (stop_pct > 0 and target_pct > 0):
            raise ValueError("stop and target must be above 0")
        self.side = s
        self.stop_pct = float(stop_pct)
        self.target_pct = float(target_pct)
        self.flat_by = int(flat_by)
        self.slippage_pct = max(0.0, float(slippage_pct))
        self.charges = dict(charges or {"target": 0.0, "stop": 0.0, "flat": 0.0})

    @property
    def rr(self) -> float:
        return self.target_pct / self.stop_pct

    @property
    def short(self) -> bool:
        return self.side == "SHORT"

    def opposite(self) -> "Rule":
        return Rule("LONG" if self.short else "SHORT", self.stop_pct, self.target_pct, self.flat_by, self.slippage_pct, self.charges)

    def be_win(self) -> float:
        """Share of trades that must reach the target to break even before charges."""
        return 1.0 / (1.0 + self.rr)

    def be_win_net(self) -> float:
        """Same, after charges and slippage, counting target and stop trades only."""
        rt, rs = self._r_for("target"), self._r_for("stop")
        return -rs / (rt - rs) if (rt - rs) else self.be_win()

    def _r_for(self, outcome: str) -> float:
        gross = self.target_pct if outcome == "target" else (-self.stop_pct if outcome == "stop" else 0.0)
        slip = self.slippage_pct * (1 if outcome == "target" else 2)
        return (gross - slip - self.charges.get(outcome, 0.0)) / self.stop_pct

    def result(self, outcome: str, gross_pct: float) -> Tuple[float, float, float]:
        """(gross %, net %, net R) of a finished trade. Slippage: one market leg on a target (the entry), two on a stop or flat exit. Pure."""
        slip = self.slippage_pct * (1 if outcome == "target" else 2)
        net = gross_pct - slip - self.charges.get(outcome, 0.0)
        return gross_pct, net, net / self.stop_pct


def walk(bars: Sequence[Bar], i: int, rule: Rule) -> Optional[Tuple[str, int, float]]:
    """Open a trade at the OPEN of bars[i] and walk bars[i:] until the stop or target is touched or the flat-by time arrives.
    Returns (outcome, exit minute, gross %) with outcome target | stop | flat, or None when bars[i] is at or after the flat-by time. Pure."""
    entry_bar = bars[i]
    if entry_bar[0] >= rule.flat_by:
        return None
    entry = entry_bar[1]
    sp, tp = rule.stop_pct / 100.0, rule.target_pct / 100.0
    if rule.short:
        stop, target = entry * (1.0 + sp), entry * (1.0 - tp)
    else:
        stop, target = entry * (1.0 - sp), entry * (1.0 + tp)
    last_close, last_mod = entry, entry_bar[0]
    for j in range(i, len(bars)):
        m, _o, h, l, c = bars[j]
        if m >= rule.flat_by:
            break
        last_close, last_mod = c, m
        if (h >= stop) if rule.short else (l <= stop):            # stop first, also when the same bar touches both
            return "stop", m, (-rule.stop_pct)
        if (l <= target) if rule.short else (h >= target):
            return "target", m, rule.target_pct
    gross = ((entry - last_close) if rule.short else (last_close - entry)) / entry * 100.0
    return "flat", last_mod, gross


# ------------------------------------------------------------------ real history

def replay_history(sessions: Dict[str, List[Bar]], rule: Rule, entry_mods: Sequence[int]) -> List[Dict[str, Any]]:
    """The rule on every real session at every entry time. One row per (day, entry time) that has that bar. Pure."""
    rows: List[Dict[str, Any]] = []
    for day, bars in sessions.items():
        idx = {b[0]: k for k, b in enumerate(bars)}
        for em in entry_mods:
            k = idx.get(em)
            if k is None:
                continue
            res = walk(bars, k, rule)
            if res is None:
                continue
            outcome, exit_mod, gross = res
            g, n, r = rule.result(outcome, gross)
            rows.append({"day": day, "entry": fmt(em), "outcome": outcome, "exit": fmt(exit_mod), "gross_pct": round(g, 4), "net_pct": round(n, 4),
                         "r_net": round(r, 4), "r_gross": round(g / rule.stop_pct, 4)})
    return rows


# ------------------------------------------------------------------ simulation

def build_pools(sessions: Dict[str, List[Bar]]) -> List[List[Tuple[float, float, float, float]]]:
    """For each of the 75 slots, the bar SHAPES seen at that time of day: (gap from the previous close, high, low, close), the last three relative to
    that bar's own open. The first slot's gap is 0 (no overnight gap is simulated). A slot nobody has takes the nearest slot's shapes."""
    pools: List[List[Tuple[float, float, float, float]]] = [[] for _ in range(N_SLOTS)]
    for bars in sessions.values():
        prev_close: Optional[float] = None
        prev_slot = -2
        for m, o, h, l, c in bars:
            s = slot_of(m)
            if not (0 <= s < N_SLOTS):
                continue
            gap = (o / prev_close - 1.0) if (prev_close and s == prev_slot + 1) else 0.0
            pools[s].append((gap, h / o - 1.0, l / o - 1.0, c / o - 1.0))
            prev_close, prev_slot = c, s
    for s in range(N_SLOTS):
        if not pools[s]:
            for d in range(1, N_SLOTS):
                for t in (s - d, s + d):
                    if 0 <= t < N_SLOTS and pools[t]:
                        pools[s] = pools[t]
                        break
                if pools[s]:
                    break
    return pools


def synth_session(pools: Sequence[Sequence[Tuple[float, float, float, float]]], rng: random.Random, start_price: float = 100.0) -> List[Bar]:
    """One made-up session: for every slot draw one real bar shape at that time of day and chain it onto the previous synthetic close. A bar keeps its own
    shape (high >= open and close, low <= open and close) so it is always a valid bar. Returns [] when any slot has no shapes at all."""
    out: List[Bar] = []
    px = start_price
    for s in range(N_SLOTS):
        pool = pools[s]
        if not pool:
            return []
        g, hi, lo, cl = pool[rng.randrange(len(pool))]
        o = px * (1.0 + g)
        out.append((OPEN_MIN + s * SLOT_MIN, o, o * (1.0 + hi), o * (1.0 + lo), o * (1.0 + cl)))
        px = o * (1.0 + cl)
    return out


class _Acc:
    __slots__ = ("target", "stop", "flat", "sum_rg", "sum_rn", "sum_net_pct", "wins_net", "r_list")

    def __init__(self, keep_r: bool):
        self.target = self.stop = self.flat = 0
        self.sum_rg = self.sum_rn = self.sum_net_pct = 0.0
        self.wins_net = 0
        self.r_list: Optional[List[float]] = [] if keep_r else None

    def add(self, outcome: str, rule: Rule, gross: float) -> None:
        g, n, r = rule.result(outcome, gross)
        if outcome == "target":
            self.target += 1
        elif outcome == "stop":
            self.stop += 1
        else:
            self.flat += 1
        self.sum_rg += g / rule.stop_pct
        self.sum_rn += r
        self.sum_net_pct += n
        if n > 0:
            self.wins_net += 1
        if self.r_list is not None:
            self.r_list.append(r)


def simulate(sessions: Dict[str, List[Bar]], plans: Sequence[Tuple[Rule, Sequence[int]]], primary_mod: int, n: int, seed: int) -> Dict[str, Any]:
    """n synthetic sessions. `plans` is a list of (rule, entry minutes): every rule is walked over every synthetic session at its own entry times (the SAME
    sessions for every rule, so a LONG against SHORT comparison is fair). The first plan's result at `primary_mod` keeps every net R (for the spread and the
    histogram). Returns {"sessions": n, "per_plan": [ {entry_mod: _Acc} ... ]} in the order of `plans`. Pure and deterministic for a given seed."""
    pools = build_pools(sessions)
    rng = random.Random(int(seed))
    accs: List[Dict[int, _Acc]] = [{em: _Acc(keep_r=(pi == 0 and em == primary_mod)) for em in mods} for pi, (_r, mods) in enumerate(plans)]
    done = 0
    for _ in range(int(n)):
        bars = synth_session(pools, rng)
        if len(bars) < N_SLOTS:
            continue
        done += 1
        for pi, (rule, mods) in enumerate(plans):
            for em in mods:
                k = slot_of(em)
                if 0 <= k < len(bars):
                    res = walk(bars, k, rule)
                    if res is not None:
                        accs[pi][em].add(res[0], rule, res[2])
    return {"sessions": done, "per_plan": accs}


def window_drift_pct(sessions: Dict[str, List[Bar]]) -> Optional[float]:
    """Average open-to-close move of one real session in the window, in %. A big positive number means the window was a rising market (the simulation
    inherits that drift, which is why the opposite side is shown beside it)."""
    moves = [(b[-1][4] / b[0][1] - 1.0) * 100.0 for b in sessions.values() if b]
    return sum(moves) / len(moves) if moves else None


# ------------------------------------------------------------------ summaries and verdict

def summarize_acc(a: _Acc, rule: Rule) -> Dict[str, Any]:
    n = a.target + a.stop + a.flat
    if n == 0:
        return {"n": 0}
    lo, hi = wilson(a.target, n)
    return {"n": n, "target": a.target, "stop": a.stop, "flat": a.flat, "p_target": a.target / n, "p_stop": a.stop / n, "p_flat": a.flat / n,
            "ci_low": lo, "ci_high": hi, "avg_r_gross": a.sum_rg / n, "avg_r_net": a.sum_rn / n, "avg_net_pct": a.sum_net_pct / n,
            "win_rate_net": a.wins_net / n}


def summarize_rows(rows: Sequence[Dict[str, Any]], rule: Rule) -> Dict[str, Any]:
    n = len(rows)
    if n == 0:
        return {"n": 0, "sessions": 0}
    t = sum(1 for r in rows if r["outcome"] == "target")
    s = sum(1 for r in rows if r["outcome"] == "stop")
    f = n - t - s
    lo, hi = wilson(t, n)
    return {"n": n, "sessions": len({r["day"] for r in rows}), "target": t, "stop": s, "flat": f, "p_target": t / n, "p_stop": s / n, "p_flat": f / n,
            "ci_low": lo, "ci_high": hi, "avg_r_gross": sum(r["r_gross"] for r in rows) / n, "avg_r_net": sum(r["r_net"] for r in rows) / n,
            "avg_net_pct": sum(r["net_pct"] for r in rows) / n, "win_rate_net": sum(1 for r in rows if r["net_pct"] > 0) / n}


def r_histogram(rs: Sequence[float], rule: Rule, width: float = 0.25) -> List[Dict[str, Any]]:
    """Counts of net R in fixed bins from just below -1R (a stop) to just above the target in R. Empty list when there is nothing."""
    if not rs:
        return []
    lo = math.floor((min(min(rs), -1.0 - 0.001)) / width) * width
    hi = math.ceil((max(max(rs), rule.rr)) / width) * width
    nb = max(1, int(round((hi - lo) / width)))
    counts = [0] * nb
    for r in rs:
        k = min(nb - 1, max(0, int((r - lo) / width)))
        counts[k] += 1
    tot = float(len(rs))
    return [{"lo": round(lo + i * width, 4), "hi": round(lo + (i + 1) * width, 4), "n": c, "share": c / tot} for i, c in enumerate(counts)]


def _sgn(x: float, margin: float) -> int:
    return 1 if x > margin else (-1 if x < -margin else 0)


def verdict(sim: Dict[str, Any], hist: Dict[str, Any], rule: Rule, sessions_n: int) -> Dict[str, Any]:
    """The one-line answer. Code is FAVOURS | AGAINST | NEAR_BREAKEVEN | MIXED | NOT_ENOUGH_DATA. Break-even aware (a 2R target needs 1 win in 3, more after charges).
    The simulation votes with a +/-0.05 R band; the history votes only with at least 10 real sessions and a wider +/-0.10 R band; disagreement is MIXED. Pure."""
    be, be_net = rule.be_win(), rule.be_win_net()
    if sessions_n < MIN_HISTORY_SESSIONS or not sim.get("n"):
        return {"code": "NOT_ENOUGH_DATA", "line": f"only {sessions_n} complete real sessions of 5-minute bars (need {MIN_HISTORY_SESSIONS}): no verdict",
                "be_win": be, "be_win_net": be_net}
    s = _sgn(sim["avg_r_net"], MARGIN_R_SIM)
    h_vote = hist.get("n", 0) >= MIN_HISTORY_SESSIONS
    h = _sgn(hist["avg_r_net"], MARGIN_R_HIST) if h_vote else s
    if s == h:
        code = "FAVOURS" if s > 0 else ("AGAINST" if s < 0 else "NEAR_BREAKEVEN")
    else:
        code = "MIXED"
    sim_txt = f"simulated target first {sim['p_target'] * 100:.0f}% vs {be_net * 100:.0f}% needed after charges (avg {sim['avg_r_net']:+.2f} R)"
    hist_txt = (f"real history {hist['target']} of {hist['n']} ({hist['p_target'] * 100:.0f}%, avg {hist['avg_r_net']:+.2f} R)" if hist.get("n")
                else "no real history at this entry time")
    word = {"FAVOURS": "the odds favour this trade", "AGAINST": "the odds are against this trade", "NEAR_BREAKEVEN": "near break-even",
            "MIXED": "simulation and real history disagree"}[code]
    tail = "" if h_vote else f" (history has under {MIN_HISTORY_SESSIONS} trades, so it does not vote)"
    return {"code": code, "line": f"{word}: {sim_txt}; {hist_txt}{tail}. A reality check, not a forecast.", "be_win": be, "be_win_net": be_net}
