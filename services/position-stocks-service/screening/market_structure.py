"""screening/market_structure.py - group305/307: pure market-structure maths for the Market Board. No I/O, no config, no clock.

A bar is a tuple/list (t, open, high, low, close, volume), t = epoch seconds of the bar start. These are HYPOTHESES, not a proven
edge: every parameter is passed in (the caller reads config and logs a params_version); nothing here decides a trade.
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

Bar = Sequence[float]


def _finite(v) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def strength(ltp, day_open, day_high, day_low, avg_move_pct, ratio: float = 2.0) -> Optional[Dict]:
    """|move from open| in units of the symbol's normal daily move, zero unless the move is one-sided
    (distance on the move's side of the open > ratio x distance on the other side). None on missing/non-positive inputs."""
    for v in (ltp, day_open, day_high, day_low):
        if not _finite(v) or v <= 0:
            return None
    if day_high < day_low:
        return None
    if not _finite(avg_move_pct) or avg_move_pct <= 0:
        return None
    up = max(0.0, day_high - day_open)
    down = max(0.0, day_open - day_low)
    move_pct = (ltp - day_open) / day_open * 100.0
    if move_pct > 0:
        side, clean = "bull", up > ratio * down
    elif move_pct < 0:
        side, clean = "bear", down > ratio * up
    else:
        return {"side": None, "move_pct": 0.0, "score": 0.0, "clean": False, "up": up, "down": down}
    score = abs(move_pct) / avg_move_pct if clean else 0.0
    return {"side": side, "move_pct": move_pct, "score": score, "clean": clean, "up": up, "down": down}


def rank_rows(rows: List[Dict], side: str) -> List[Dict]:
    """Rank one side's rows by score (best first); adds rank, side_total, percentile. Rows with score <= 0 are dropped."""
    pool = [r for r in rows if r.get("side") == side and _finite(r.get("score")) and r["score"] > 0]
    pool.sort(key=lambda r: (-r["score"], r.get("symbol", "")))
    n = len(pool)
    for i, r in enumerate(pool, start=1):
        r["rank"] = i
        r["side_total"] = n
        r["percentile"] = round(100.0 * (n - i + 0.5) / n, 1)
    return pool


DEFAULT_CONVICTION_WEIGHTS = {"strength": 0.45, "volume": 0.20, "vwap": 0.15, "sector": 0.10, "zone": 0.10}


def parse_weights(text: str) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for part in (text or "").split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k = k.strip().lower()
        try:
            f = float(v)
        except ValueError:
            continue
        if k in DEFAULT_CONVICTION_WEIGHTS and math.isfinite(f) and f >= 0:
            out[k] = f
    return out


def conviction(percentile, vol_pace, ltp, vwap, side, sector_agree, zone_quality,
               weights: Optional[Dict[str, float]] = None) -> int:
    """0-100 heuristic blend (NOT a fitted model; weights are config and logged). Unknown inputs count as 0.5."""
    w = dict(DEFAULT_CONVICTION_WEIGHTS)
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w and _finite(v) and v >= 0})
    total = sum(w.values()) or 1.0
    s = max(0.0, min(1.0, (percentile or 0.0) / 100.0))
    v = max(0.0, min(1.0, (vol_pace - 0.5) / 1.5)) if _finite(vol_pace) else 0.5
    if _finite(vwap) and vwap > 0 and _finite(ltp) and ltp > 0:
        dist = (ltp - vwap) / vwap * 100.0
        if side == "bear":
            dist = -dist
        a = 1.0 if dist >= 0 else max(0.0, 1.0 + dist / 2.0)
    else:
        a = 0.5
    sec = max(0.0, min(1.0, sector_agree)) if _finite(sector_agree) else 0.5
    zq = max(0.0, min(1.0, zone_quality)) if _finite(zone_quality) else 0.5
    blend = (w["strength"] * s + w["volume"] * v + w["vwap"] * a + w["sector"] * sec + w["zone"] * zq) / total
    return int(round(max(0.0, min(1.0, blend)) * 100))


def aggregate(bars: Sequence[Bar], minutes: int) -> List[Tuple[float, float, float, float, float, float]]:
    """1-minute bars -> `minutes`-minute bars aligned to the epoch (the last bucket may be partial)."""
    if minutes <= 1:
        return [tuple(b) for b in bars]  # type: ignore[misc]
    span = minutes * 60
    out: List[list] = []
    for b in bars:
        t = float(int(b[0] // span) * span)
        if out and out[-1][0] == t:
            o = out[-1]
            o[2] = max(o[2], b[2]); o[3] = min(o[3], b[3]); o[4] = b[4]; o[5] += b[5]
        else:
            out.append([t, b[1], b[2], b[3], b[4], b[5]])
    return [tuple(o) for o in out]  # type: ignore[misc]


def atr(bars: Sequence[Bar], period: int = 14, min_bars: int = 5) -> Optional[float]:
    if len(bars) < max(2, min_bars):
        return None
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    trs = trs[-period:]
    return sum(trs) / len(trs) if trs else None


def vwap_from_bars(bars: Sequence[Bar]) -> Optional[float]:
    pv = v = 0.0
    for b in bars:
        vol = b[5] or 0.0
        if vol > 0:
            pv += (b[2] + b[3] + b[4]) / 3.0 * vol
            v += vol
    return pv / v if v > 0 else None


def volume_pace(bars: Sequence[Bar], window: int = 15) -> Optional[float]:
    """Volume/min over the last `window` bars divided by volume/min before them. None if fewer than 2 windows."""
    if len(bars) < 2 * window:
        return None
    recent = sum(b[5] for b in bars[-window:]) / window
    earlier_bars = bars[:-window]
    earlier = sum(b[5] for b in earlier_bars) / len(earlier_bars)
    if earlier <= 0:
        return None
    return recent / earlier


def opening_range(bars: Sequence[Bar], session_open_ts: float, minutes: int = 15) -> Optional[Tuple[float, float]]:
    end = session_open_ts + minutes * 60
    sel = [b for b in bars if session_open_ts <= b[0] < end]
    if not sel:
        return None
    return (min(b[3] for b in sel), max(b[2] for b in sel))


def profile(bars: Sequence[Bar], buckets: int = 16) -> List[float]:
    """Volume-at-price histogram (time-at-price when no volume), normalised so the biggest bucket is 1.0."""
    if not bars or buckets < 2:
        return []
    lo = min(b[3] for b in bars)
    hi = max(b[2] for b in bars)
    if hi <= lo:
        return []
    acc = [0.0] * buckets
    use_vol = any((b[5] or 0) > 0 for b in bars)
    for b in bars:
        idx = min(buckets - 1, int((b[4] - lo) / (hi - lo) * buckets))
        acc[idx] += (b[5] or 0.0) if use_vol else 1.0
    m = max(acc)
    return [round(a / m, 3) for a in acc] if m > 0 else []


def spark(bars: Sequence[Bar], points: int = 60) -> List[float]:
    closes = [b[4] for b in bars]
    if len(closes) <= points:
        return closes
    step = len(closes) / points
    out = [closes[int(i * step)] for i in range(points)]
    out[-1] = closes[-1]
    return out


def swings(bars: Sequence[Bar], left: int = 2, right: int = 2) -> List[Dict]:
    """Fractal swing highs / lows."""
    out = []
    n = len(bars)
    for i in range(left, n - right):
        h, l = bars[i][2], bars[i][3]
        if all(h > bars[j][2] for j in range(i - left, i)) and all(h > bars[j][2] for j in range(i + 1, i + right + 1)):
            out.append({"idx": i, "t": bars[i][0], "kind": "high", "price": h})
        if all(l < bars[j][3] for j in range(i - left, i)) and all(l < bars[j][3] for j in range(i + 1, i + right + 1)):
            out.append({"idx": i, "t": bars[i][0], "kind": "low", "price": l})
    return out


def _is_displacement(b: Bar, a: float, k: float, body_frac: float = 0.6) -> int:
    rng = b[2] - b[3]
    body = abs(b[4] - b[1])
    if rng <= 0 or body < k * a or body < body_frac * rng:
        return 0
    return 1 if b[4] > b[1] else -1


def find_zones(bars: Sequence[Bar], atr_mult: float = 1.5, max_zones: int = 6, full_range: bool = False,
               atr_period: int = 14, swing_n: int = 2) -> List[Dict]:
    """Order-block zones: the last opposite-colour candle (within 5 bars) before a displacement candle (body >= k x ATR and
    >= 60% of range). fvg = gap across the displacement; bos = displacement closes beyond the last prior swing; valid = bos or fvg.
    status: fresh / tested (traded into) / broken (closed through -> dropped). Oldest first, newest `max_zones` kept."""
    n = len(bars)
    a = atr(bars, atr_period)
    if n < 6 or a is None or a <= 0:
        return []
    sw = swings(bars, swing_n, swing_n)
    zones: List[Dict] = []
    used = set()
    for i in range(1, n):
        d = _is_displacement(bars[i], a, atr_mult)
        if d == 0:
            continue
        j = None
        for k in range(i - 1, max(-1, i - 6), -1):
            c = bars[k]
            if (d > 0 and c[4] < c[1]) or (d < 0 and c[4] > c[1]):
                j = k
                break
        if j is None or (j, d) in used:
            continue
        used.add((j, d))
        ob = bars[j]
        body_lo, body_hi = min(ob[1], ob[4]), max(ob[1], ob[4])
        if full_range:
            bottom, top = ob[3], ob[2]
        elif d > 0:
            bottom, top = body_lo, ob[2]
        else:
            bottom, top = ob[3], body_hi
        if top <= bottom:
            continue
        fvg = False
        if i + 1 < n:
            fvg = (bars[i - 1][2] < bars[i + 1][3]) if d > 0 else (bars[i - 1][3] > bars[i + 1][2])
        prior = [s for s in sw if s["idx"] + swing_n <= j]
        bos = False
        if d > 0:
            hs = [s for s in prior if s["kind"] == "high"]
            if hs:
                lvl = hs[-1]["price"]
                bos = any(bars[m][4] > lvl for m in range(i, min(n, i + 3)))
        else:
            ls = [s for s in prior if s["kind"] == "low"]
            if ls:
                lvl = ls[-1]["price"]
                bos = any(bars[m][4] < lvl for m in range(i, min(n, i + 3)))
        status = "fresh"
        for m in range(i + 2, n):
            c = bars[m]
            if d > 0:
                if c[4] < bottom:
                    status = "broken"; break
                if c[3] <= top:
                    status = "tested"
            else:
                if c[4] > top:
                    status = "broken"; break
                if c[2] >= bottom:
                    status = "tested"
        if status == "broken":
            continue
        zones.append({"side": "bull" if d > 0 else "bear", "top": top, "bottom": bottom, "t": ob[0], "idx": j,
                      "status": status, "bos": bos, "fvg": fvg, "valid": bos or fvg,
                      "impulse": round(abs(bars[i][4] - bars[i][1]) / a, 2), "atr": a})
    zones.sort(key=lambda z: z["t"])
    if max_zones is not None and max_zones >= 0:
        zones = zones[-max_zones:] if max_zones else []
    return zones


def zone_quality(zone: Optional[Dict]) -> Optional[float]:
    if not zone:
        return None
    q = 0.4 + (0.3 if zone.get("bos") else 0.0) + (0.2 if zone.get("fvg") else 0.0) + (0.1 if zone.get("status") == "fresh" else 0.0)
    return round(min(1.0, q), 2)


def pick_zone(zones: Sequence[Dict], side: str, ltp: float) -> Optional[Dict]:
    pool = [z for z in zones if z["side"] == side]
    if not pool:
        return None
    pool.sort(key=lambda z: (0 if z.get("valid") else 1, abs(ltp - (z["top"] + z["bottom"]) / 2.0), -z["t"]))
    return pool[0]


def chase_atr(side: str, ltp: float, zone: Optional[Dict]) -> Optional[float]:
    """ATRs price has run past the zone in the trade direction (bull (ltp-top)/atr, bear (bottom-ltp)/atr)."""
    if not zone or not _finite(zone.get("atr")) or zone["atr"] <= 0 or not _finite(ltp):
        return None
    dist = (ltp - zone["top"]) if side == "bull" else (zone["bottom"] - ltp)
    return round(dist / zone["atr"], 3)


def levels(ltp: float, swing_points: Sequence[Dict], bars: Sequence[Bar], prev_day: Optional[Dict] = None,
           or_range: Optional[Tuple[float, float]] = None, n: int = 4, merge_pct: float = 0.1) -> Dict[str, List[Dict]]:
    """S1.. below / R1.. above the price from unfilled swing points, previous-day high/low/close and the opening range."""
    cands: List[Tuple[float, str]] = []
    for s in swing_points:
        later = bars[s["idx"] + 1:]
        if s["kind"] == "high" and not any(b[2] >= s["price"] for b in later):
            cands.append((s["price"], "swing"))
        if s["kind"] == "low" and not any(b[3] <= s["price"] for b in later):
            cands.append((s["price"], "swing"))
    if prev_day:
        for key, label in (("high", "prev high"), ("low", "prev low"), ("close", "prev close")):
            if _finite(prev_day.get(key)) and prev_day[key] > 0:
                cands.append((float(prev_day[key]), label))
    if or_range:
        cands.append((or_range[0], "OR low"))
        cands.append((or_range[1], "OR high"))

    def pick(side_up: bool) -> List[Dict]:
        pool = sorted({round(p, 4): src for p, src in cands if (p > ltp if side_up else p < ltp)}.items(),
                      key=lambda kv: kv[0] if side_up else -kv[0])
        out: List[Dict] = []
        for price, src in pool:
            if any(abs(price - o["price"]) / o["price"] * 100.0 < merge_pct for o in out):
                continue
            out.append({"price": price, "source": src})
            if len(out) >= n:
                break
        for i, o in enumerate(out, start=1):
            o["label"] = f"{'R' if side_up else 'S'}{i}"
        return out

    return {"supports": pick(False), "resistances": pick(True)}


def risk_reward(side: str, ltp: float, zone: Optional[Dict], lv: Dict[str, List[Dict]]) -> Optional[Dict]:
    """Invalidation = the zone's far edge; target = nearest level beyond the price. None unless risk and reward are positive."""
    if not zone:
        return None
    if side == "bull":
        inval = zone["bottom"]
        risk = ltp - inval
        tgt = next((x for x in lv.get("resistances", [])), None)
        reward = (tgt["price"] - ltp) if tgt else None
    else:
        inval = zone["top"]
        risk = inval - ltp
        tgt = next((x for x in lv.get("supports", [])), None)
        reward = (ltp - tgt["price"]) if tgt else None
    if risk is None or risk <= 0 or reward is None or reward <= 0:
        return None
    return {"invalidation": inval, "target": tgt["price"], "target_label": tgt["label"], "risk": risk,
            "reward": reward, "rr": round(reward / risk, 2)}


def zone_score(zone: Optional[Dict], rr: Optional[float], chase: Optional[float], chase_limit: float = 1.5) -> Optional[int]:
    """group311: one 0-100 number for how clean a zone setup is (for the Real Trade board, which has no zone maths of its own).
    Heuristic, not a measured edge: zone quality 40 (bos / fvg / fresh), zone still valid 15, reward:risk 25 (capped at 3R), room left
    before the zone is chased 20 (unknown chase = half). None when there is no zone for the side."""
    if not zone:
        return None
    q = zone_quality(zone) or 0.4
    pts = 40.0 * max(0.0, min(1.0, (q - 0.4) / 0.6))
    pts += 15.0 if zone.get("valid") else 0.0
    if rr is not None and _finite(rr) and rr > 0:
        pts += 25.0 * min(float(rr), 3.0) / 3.0
    if chase is not None and _finite(chase) and chase_limit > 0:
        pts += 20.0 * (1.0 - max(0.0, min(1.0, float(chase) / chase_limit)))
    else:
        pts += 10.0
    return int(round(max(0.0, min(100.0, pts))))


def zone_risk(side: str, ltp: float, zone: Optional[Dict]) -> Optional[Tuple[float, float]]:
    """group311: (invalidation price, risk per share) from the zone's far edge, with or without a target level. None when there is no
    zone or price is already beyond the far edge. Used for the Real Trade 'over risk cap' check."""
    if not zone or not _finite(ltp) or ltp <= 0:
        return None
    inval = zone["bottom"] if side == "bull" else zone["top"]
    risk = (ltp - inval) if side == "bull" else (inval - ltp)
    if not _finite(inval) or not _finite(risk) or risk <= 0:
        return None
    return round(float(inval), 2), round(float(risk), 2)
