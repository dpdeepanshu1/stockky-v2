"""swing/momentum.py - E130 (plan step S1): pure momentum measures on daily bars. READ-ONLY, stdlib only, never raises.

Bars are rows of (t, o, h, l, c, v) in ascending time, t = epoch seconds (the shape stockky_ms.timeframes.rows_to_bars returns).

Measures (trading-day counts: 12 months = 252, 6 months = 126, skip month = 21):
  ret_12_1    return from 252 days ago to 21 days ago (the academic 12-1 measure: the last month is skipped because of short-term reversal)
  ret_6_1     the same over 126 days
  vol_pct     standard deviation of daily % returns over the last year (needs 60+ returns)
  mr_12/mr_6  momentum ratio = return / vol_pct (the NSE Nifty200 Momentum 30 idea: return adjusted for its own volatility)
  above_sma200, dist_52w_high_pct, adx14, rs_6m_vs_bench_pct   trend context
Ranking (rank_scores): z-score each ratio across the universe, average the z-scores that exist (50 / 50 when both exist), then the NSE
normalised score (1 + z when z >= 0, else 1 / (1 - z)). `screen` is INFORMATION ONLY: the swing candidate rule is to be fixed by the S2 backtest,
and nothing reads `screen` to decide anything.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

TD_12M = 252
TD_6M = 126
SKIP = 21
MIN_RETURNS_FOR_VOL = 60
MIN_UNIVERSE_FOR_RANK = 5


def _fin(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def closes_of(bars: Sequence[Sequence[float]]) -> List[float]:
    out: List[float] = []
    for b in bars or []:
        try:
            c = _fin(b[4])
        except (IndexError, TypeError):
            c = None
        if c is not None and c > 0:
            out.append(c)
    return out


def skip_return_pct(closes: Sequence[float], lookback: int, skip: int = SKIP) -> Optional[float]:
    """% return from `lookback` days ago to `skip` days ago. None when there are not lookback + 1 closes."""
    if lookback <= skip or len(closes) < lookback + 1:
        return None
    start, end = closes[-1 - lookback], closes[-1 - skip]
    if start <= 0 or end <= 0:
        return None
    return (end / start - 1.0) * 100.0


def daily_vol_pct(closes: Sequence[float], n: int = TD_12M) -> Optional[float]:
    """Sample standard deviation of daily % returns over the last n returns (None under MIN_RETURNS_FOR_VOL)."""
    c = list(closes)[-(n + 1):]
    rets = [(c[i] / c[i - 1] - 1.0) * 100.0 for i in range(1, len(c)) if c[i - 1] > 0]
    if len(rets) < MIN_RETURNS_FOR_VOL:
        return None
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def sma(closes: Sequence[float], n: int) -> Optional[float]:
    return sum(closes[-n:]) / n if len(closes) >= n else None


def dist_from_high_pct(bars: Sequence[Sequence[float]], n: int = TD_12M) -> Optional[float]:
    """% below the highest high of the last n bars (0 or negative). None without bars."""
    highs = [h for h in (_fin(b[2]) for b in (bars or [])[-n:]) if h is not None and h > 0]
    last = closes_of(bars)
    if not highs or not last:
        return None
    return (last[-1] / max(highs) - 1.0) * 100.0


def adx14(bars: Sequence[Sequence[float]], period: int = 14) -> Optional[float]:
    """Wilder ADX. Needs 2 * period + 1 bars. High = a trend exists (about 20-25 and up); low = range-bound."""
    rows = []
    for b in bars or []:
        h, l, c = _fin(b[2]), _fin(b[3]), _fin(b[4])
        if h is None or l is None or c is None or h < l or c <= 0:
            continue
        rows.append((h, l, c))
    if len(rows) < 2 * period + 1:
        return None
    tr, pdm, mdm = [], [], []
    for i in range(1, len(rows)):
        h, l, _ = rows[i]
        ph, pl, pc = rows[i - 1]
        up, down = h - ph, pl - l
        pdm.append(up if (up > down and up > 0) else 0.0)
        mdm.append(down if (down > up and down > 0) else 0.0)
        tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    s_tr, s_p, s_m = sum(tr[:period]), sum(pdm[:period]), sum(mdm[:period])
    dxs = []
    for i in range(period, len(tr) + 1):
        if i > period:
            s_tr = s_tr - s_tr / period + tr[i - 1]
            s_p = s_p - s_p / period + pdm[i - 1]
            s_m = s_m - s_m / period + mdm[i - 1]
        if s_tr <= 0:
            dxs.append(0.0)
            continue
        pdi, mdi = 100.0 * s_p / s_tr, 100.0 * s_m / s_tr
        dxs.append(0.0 if (pdi + mdi) == 0 else 100.0 * abs(pdi - mdi) / (pdi + mdi))
    if len(dxs) < period:
        return None
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return adx


def atr_pct_simple(bars: Sequence[Sequence[float]], n: int = 14) -> Optional[float]:
    """E132: simple ATR(n) as % of the last close, from the bars given only (the SAME formula scripts/entry_rules_backtest.atr_pct uses, so the live stop and the
    replayed stop come from one definition). None under n + 1 bars or a bad last close."""
    rows = []
    for b in bars or []:
        h, l, c = _fin(b[2]), _fin(b[3]), _fin(b[4])
        if h is None or l is None or c is None or c <= 0 or h < l:
            continue
        rows.append((h, l, c))
    if len(rows) < n + 1:
        return None
    trs = []
    for k in range(len(rows) - n, len(rows)):
        h, l, _ = rows[k]
        pc = rows[k - 1][2]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return (sum(trs) / n) / rows[-1][2] * 100.0


def rs_vs_bench_pct(closes: Sequence[float], bench_closes: Optional[Sequence[float]], n: int = TD_6M) -> Optional[float]:
    """Stock return minus benchmark return over the last n days, in points. None when either series is too short."""
    if not bench_closes or len(closes) < n + 1 or len(bench_closes) < n + 1:
        return None
    s = closes[-1] / closes[-1 - n] - 1.0
    b = bench_closes[-1] / bench_closes[-1 - n] - 1.0
    return (s - b) * 100.0


def score_symbol(symbol: str, bars: Sequence[Sequence[float]], bench_closes: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """All measures for one symbol. status: ok (253+ closes, 12-1 and 6-1), limited (127+ closes, 6-1 only), insufficient."""
    out: Dict[str, Any] = {"symbol": str(symbol or "").upper(), "status": "insufficient", "n_bars": 0, "last_close": None, "sma10": None, "atr14_pct": None,
                           "ret_12_1": None, "ret_6_1": None, "vol_pct": None, "mr_12": None, "mr_6": None,
                           "above_sma200": None, "sma200_gap_pct": None, "dist_52w_high_pct": None, "adx14": None,
                           "rs_6m_vs_bench_pct": None, "reasons": []}
    try:
        cl = closes_of(bars)
        out["n_bars"] = len(cl)
        if not cl:
            out["reasons"].append("no usable bars")
            return out
        out["last_close"] = round(cl[-1], 4)
        out["sma10"] = None if len(cl) < 10 else round(sum(cl[-10:]) / 10.0, 4)       # E132: the swing lane's "trend intact" check
        a14 = atr_pct_simple(bars)
        out["atr14_pct"] = None if a14 is None else round(a14, 4)                       # E132: the swing lane's stop size
        try:                                                                              # E132: the last bar itself (shadow settle replays it)
            lb = bars[-1]
            out["last_t"] = float(lb[0])
            out["last_open"], out["last_high"], out["last_low"] = round(float(lb[1]), 4), round(float(lb[2]), 4), round(float(lb[3]), 4)
        except (IndexError, TypeError, ValueError):
            pass
        r12, r6 = skip_return_pct(cl, TD_12M), skip_return_pct(cl, TD_6M)
        vol = daily_vol_pct(cl)
        out["ret_12_1"] = None if r12 is None else round(r12, 3)
        out["ret_6_1"] = None if r6 is None else round(r6, 3)
        out["vol_pct"] = None if vol is None else round(vol, 4)
        if vol and vol > 0:
            out["mr_12"] = None if r12 is None else round(r12 / vol, 4)
            out["mr_6"] = None if r6 is None else round(r6 / vol, 4)
        s200 = sma(cl, 200)
        if s200:
            out["above_sma200"] = bool(cl[-1] > s200)
            out["sma200_gap_pct"] = round((cl[-1] / s200 - 1.0) * 100.0, 3)
        d = dist_from_high_pct(bars)
        out["dist_52w_high_pct"] = None if d is None else round(d, 3)
        a = adx14(bars)
        out["adx14"] = None if a is None else round(a, 2)
        rs = rs_vs_bench_pct(cl, bench_closes)
        out["rs_6m_vs_bench_pct"] = None if rs is None else round(rs, 3)
        if out["mr_12"] is not None and out["mr_6"] is not None:
            out["status"] = "ok"
        elif out["mr_6"] is not None:
            out["status"] = "limited"
            out["reasons"].append("under 253 closes: 12-1 not available, ranked on 6-1 only")
        else:
            out["reasons"].append("under 127 closes or too few returns for volatility")
    except Exception as e:  # noqa: BLE001 - one bad series must not stop a ranking
        out["status"] = "insufficient"
        out["reasons"].append(f"error: {str(e)[:80]}")
    return out


def _z(values: List[Optional[float]]) -> List[Optional[float]]:
    vs = [v for v in values if v is not None]
    if len(vs) < 2:
        return [None] * len(values)
    m = sum(vs) / len(vs)
    sd = math.sqrt(sum((v - m) ** 2 for v in vs) / len(vs))
    if sd <= 0:
        return [None] * len(values)
    return [None if v is None else (v - m) / sd for v in values]


def normalised(z: float) -> float:
    """The NSE normalised momentum score: 1 + z for z >= 0, else 1 / (1 - z) (always positive)."""
    return 1.0 + z if z >= 0 else 1.0 / (1.0 - z)


def rank_scores(scores: List[Dict[str, Any]], min_pct_rank: float = 80.0) -> List[Dict[str, Any]]:
    """Adds z12, z6, norm_score, rank (1 = strongest), pct_rank (0-100) and screen to each rankable row; returns rows best first, unrankable rows last.
    `screen` = pct_rank >= min_pct_rank AND above the 200-day average. INFORMATION ONLY (see module docstring)."""
    rows = [dict(s) for s in scores or []]
    elig = [r for r in rows if r.get("status") in ("ok", "limited")]
    if len(elig) >= MIN_UNIVERSE_FOR_RANK:
        z12 = _z([r.get("mr_12") for r in elig])
        z6 = _z([r.get("mr_6") for r in elig])
        for r, a, b in zip(elig, z12, z6):
            parts = [x for x in (a, b) if x is not None]
            r["z12"] = None if a is None else round(a, 4)
            r["z6"] = None if b is None else round(b, 4)
            if not parts:
                r["norm_score"] = None
                continue
            r["norm_score"] = round(normalised(sum(parts) / len(parts)), 4)
        ranked = sorted([r for r in elig if r.get("norm_score") is not None], key=lambda r: (-r["norm_score"], r["symbol"]))
        n = len(ranked)
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
            r["pct_rank"] = round(100.0 * (n - i) / (n - 1), 1) if n > 1 else 100.0
            r["screen"] = bool(r["pct_rank"] >= min_pct_rank and r.get("above_sma200") is True)
    else:
        for r in elig:
            r["reasons"] = list(r.get("reasons") or []) + [f"universe too small to rank (need {MIN_UNIVERSE_FOR_RANK}+ symbols with data)"]
    ranked_rows = sorted([r for r in rows if r.get("rank") is not None], key=lambda r: r["rank"])
    rest = sorted([r for r in rows if r.get("rank") is None], key=lambda r: r["symbol"])
    return ranked_rows + rest
