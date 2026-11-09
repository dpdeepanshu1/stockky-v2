"""stockky_ms.structure - HH/HL/LH/LL labels, trend state machine, BOS and CHOCH in ONE left-to-right scan.

Each bar t only uses swings with confirmed_at < t for its break test and adds swings confirmed at t afterwards, so events found for a
prefix are exactly what a live system would have seen (truncation invariance). CHOCH is a transition signal, never a standalone BUY.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from .ms_types import BEARISH, BULLISH, C, H, L, NEUTRAL, O, TRANSITION, V, clamp
from .order_blocks import displacement
from .params import MSParams


def _label(struct: List[Dict], k: int, tol: float) -> Optional[str]:
    cur = struct[k]
    prev = next((struct[j] for j in range(k - 1, -1, -1) if struct[j]["kind"] == cur["kind"]), None)
    if prev is None:
        return None
    ref = prev["price"]
    if abs(cur["price"] - ref) <= ref * tol:
        return "EH" if cur["kind"] == "high" else "EL"
    if cur["kind"] == "high":
        return "HH" if cur["price"] > ref else "LH"
    return "HL" if cur["price"] > ref else "LL"


def _state(struct: List[Dict]) -> str:
    hs = [s for s in struct if s["kind"] == "high" and s.get("label")]
    ls = [s for s in struct if s["kind"] == "low" and s.get("label")]
    if not hs or not ls:
        return NEUTRAL
    h, l = hs[-1]["label"], ls[-1]["label"]
    if h == "HH" and l == "HL":
        return BULLISH
    if h == "LH" and l == "LL":
        return BEARISH
    return NEUTRAL


def _vol_ratio(bars, t: int, n: int) -> Optional[float]:
    if t < n:
        return None
    prev = [bars[j][V] for j in range(t - n, t)]
    if any(v <= 0 for v in prev) or bars[t][V] <= 0:
        return None
    return bars[t][V] / (sum(prev) / n)


def _strength(bars, t, level, direction, a, gap, p: MSParams, struct, disp) -> float:
    b = bars[t]
    dist = ((b[C] - level) if direction > 0 else (level - b[C])) / a
    rng = b[H] - b[L]
    body = abs(b[C] - b[O]) / rng if rng > 0 else 0.0
    vr = _vol_ratio(bars, t, p.vol_mean_bars)
    vol_pts = 0.1 if vr is None else (0.2 if vr >= 1.2 else 0.05 if vr >= 0.8 else 0.0)
    s = 0.4 * clamp(dist / 1.0) + 0.2 * body + (0.2 if disp else 0.0) + vol_pts
    beyond = [x["price"] for x in struct if x["kind"] == ("high" if direction > 0 else "low")
              and ((x["price"] > b[C]) if direction > 0 else (x["price"] < b[C]))]
    if beyond and min(abs(x - b[C]) for x in beyond) <= a:
        s -= 0.15
    if gap:
        s *= 0.6
    return round(clamp(s), 3)


def scan_structure(bars: Sequence[Sequence[float]], atr: Sequence[Optional[float]], raw: Sequence[Dict],
                   gap_event: Sequence[bool], circuit: Sequence[bool], p: MSParams) -> Dict[str, Any]:
    n = len(bars)
    tol = p.equal_tol_pct / 100.0
    struct: List[Dict] = []
    consumed: Set[Tuple[str, int]] = set()
    state, trans_dir, trans_idx = NEUTRAL, 0, -1
    bos: List[Dict] = []
    choch: List[Dict] = []
    ptr = 0
    for t in range(n):
        a = atr[t]
        if a and a > 0 and struct and not circuit[t]:
            nc = len(choch)
            _detect(bars, t, a, struct, consumed, state, trans_dir, bool(gap_event[t]), p, bos, choch)
            if len(choch) > nc:
                state, trans_dir, trans_idx = TRANSITION, choch[-1]["dir"], t
        added = False
        while ptr < len(raw) and raw[ptr]["confirmed_at"] <= t:
            _add(struct, raw[ptr], atr[t] if atr[t] else None, p, tol)
            ptr += 1
            added = True
        if added:
            ns = _state(struct)
            if state == TRANSITION:
                want = BULLISH if trans_dir > 0 else BEARISH
                fail_label = "LL" if trans_dir > 0 else "HH"
                if ns == want:
                    state, trans_dir = ns, 0
                elif any(s.get("label") == fail_label and s["idx"] > trans_idx for s in struct):
                    state, trans_dir = ns, 0
            else:
                state = ns
    for e in bos + choch:
        lvl, bi = e["level"], e["break_idx"]
        after = [bars[j][C] for j in range(bi + 1, min(n, bi + 1 + p.bos_fail_bars))]
        e["failed"] = any((c < lvl) if e["dir"] > 0 else (c > lvl) for c in after)
    labels = [s["label"] for s in struct if s.get("label")]
    return {"struct": struct, "state": state, "bos": bos, "choch": choch, "labels": labels}


def _add(struct: List[Dict], sw: Dict, a: Optional[float], p: MSParams, tol: float) -> None:
    new = {"idx": sw["idx"], "ts": sw["ts"], "kind": sw["kind"], "price": sw["price"], "confirmed_at": sw["confirmed_at"], "label": None}
    if struct and struct[-1]["kind"] == new["kind"]:
        last = struct[-1]
        more = new["price"] > last["price"] if new["kind"] == "high" else new["price"] < last["price"]
        if more:
            struct[-1] = new
            new["label"] = _label(struct, len(struct) - 1, tol)
        return
    if struct and a and abs(new["price"] - struct[-1]["price"]) < p.swing_min_amp_atr * a:
        return
    struct.append(new)
    new["label"] = _label(struct, len(struct) - 1, tol)


def _detect(bars, t, a, struct, consumed, state, trans_dir, gap, p: MSParams, bos, choch) -> None:
    b = bars[t]
    px_up = b[C] if p.bos_close_required else b[H]
    px_dn = b[C] if p.bos_close_required else b[L]
    disp = displacement(b, a, p)
    live = [s for s in struct if s["confirmed_at"] < t]
    if not live:
        return
    hs = [s for s in live if s["kind"] == "high" and ("high", s["idx"]) not in consumed]
    ls = [s for s in live if s["kind"] == "low" and ("low", s["idx"]) not in consumed]
    if hs:
        bear_side = state == BEARISH or (state == TRANSITION and trans_dir < 0)
        level_sw = hs[-1]
        if bear_side:
            lows = [s for s in live if s["kind"] == "low"]
            lastl = lows[-1] if lows else None
            prior_h = [s for s in hs if lastl and s["idx"] < lastl["idx"]]
            level_sw = prior_h[-1] if prior_h else None
        if level_sw is not None and px_up > level_sw["price"] + p.bos_min_atr * a:
            consumed.add(("high", level_sw["idx"]))
            ev = {"dir": 1, "direction": "bullish", "level": level_sw["price"], "level_idx": level_sw["idx"], "break_price": b[C],
                  "break_idx": t, "gap_event": gap, "disp": disp > 0, "vol_ratio": _vol_ratio(bars, t, p.vol_mean_bars),
                  "strength": _strength(bars, t, level_sw["price"], 1, a, gap, p, live, disp > 0)}
            (choch if bear_side else bos).append(ev)
            return
    if ls:
        bull_side = state == BULLISH or (state == TRANSITION and trans_dir > 0)
        level_sw = ls[-1]
        if bull_side:
            highs = [s for s in live if s["kind"] == "high"]
            lasth = highs[-1] if highs else None
            prior_l = [s for s in ls if lasth and s["idx"] < lasth["idx"]]
            level_sw = prior_l[-1] if prior_l else None
        if level_sw is not None and px_dn < level_sw["price"] - p.bos_min_atr * a:
            consumed.add(("low", level_sw["idx"]))
            ev = {"dir": -1, "direction": "bearish", "level": level_sw["price"], "level_idx": level_sw["idx"], "break_price": b[C],
                  "break_idx": t, "gap_event": gap, "disp": disp < 0, "vol_ratio": _vol_ratio(bars, t, p.vol_mean_bars),
                  "strength": _strength(bars, t, level_sw["price"], -1, a, gap, p, live, disp < 0)}
            (choch if bull_side else bos).append(ev)
