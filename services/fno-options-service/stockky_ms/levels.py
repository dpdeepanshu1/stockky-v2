"""stockky_ms.levels - structure-aware support / resistance zones.

Candidates (priority first): unfilled structural swings, active order-block / FVG edges, previous-bar H/L, equal-high/low pools,
52-week high / low (only with >= 200 bars) and the legacy 20-bar extremes (so plain `support` / `resistance` stay available).
Candidates within level_merge_pct are merged into one zone {price_low, price_high, strength, sources}. Pure; no clock, no I/O.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .ms_types import C, H, L, r2, r3
from .params import MSParams

_WEIGHT = {"swing": 0.5, "equal_high": 0.3, "equal_low": 0.3, "52w_high": 0.3, "52w_low": 0.3, "ob": 0.25, "fvg": 0.2,
           "prev_bar": 0.2, "range20": 0.15}


def _merge(cands: List[Dict[str, Any]], tol_pct: float) -> List[Dict[str, Any]]:
    zones: List[Dict[str, Any]] = []
    for c in sorted(cands, key=lambda x: (x["low"], x["high"], x["src"])):
        z = zones[-1] if zones else None
        if z is not None and c["low"] <= z["price_high"] * (1.0 + tol_pct / 100.0):
            z["price_high"] = max(z["price_high"], c["high"])
            if c["src"] not in z["sources"]:
                z["sources"].append(c["src"])
        else:
            zones.append({"price_low": c["low"], "price_high": c["high"], "sources": [c["src"]]})
    for z in zones:
        z["strength"] = min(1.0, sum(_WEIGHT.get(s, 0.1) for s in z["sources"]))
    return zones


def _fmt(z: Dict[str, Any]) -> Dict[str, Any]:
    return {"price_low": r2(z["price_low"]), "price_high": r2(z["price_high"]), "strength": r3(z["strength"]), "sources": list(z["sources"])}


def build_levels(bars: Sequence[Sequence[float]], struct: Sequence[Dict], pools: Sequence[Dict], obs: Sequence[Dict],
                 fvgs: Sequence[Dict], p: MSParams) -> Dict[str, List[Dict[str, Any]]]:
    n = len(bars)
    if n == 0:
        return {"supports": [], "resistances": []}
    price = bars[-1][C]
    sup: List[Dict[str, Any]] = []
    res: List[Dict[str, Any]] = []

    def add(kind_up: bool, lo: float, hi: float, src: str) -> None:
        if kind_up and lo > price:
            res.append({"low": lo, "high": hi, "src": src})
        elif not kind_up and hi < price:
            sup.append({"low": lo, "high": hi, "src": src})

    for s in struct:  # structural swings still intact (no close through them since confirmation)
        after = bars[s["confirmed_at"] + 1:]
        if s["kind"] == "high":
            if not any(b[H] > s["price"] for b in after):
                add(True, s["price"], s["price"], "swing")
        elif not any(b[C] < s["price"] for b in after):
            add(False, s["price"], s["price"], "swing")
    for z in obs:
        add(z["side"] == "bearish", z["bottom"], z["top"], "ob")
    for z in fvgs:
        if z["status"] in ("fresh", "partial", "tested"):
            add(z["side"] == "bearish", z["bottom"], z["top"], "fvg")
    last = bars[-1]
    add(True, last[H], last[H], "prev_bar")
    add(False, last[L], last[L], "prev_bar")
    for x in pools:
        if x["kind"] == "high" and x.get("equal") and not x["swept"]:
            add(True, x["price"], x["price"], "equal_high")
        elif x["kind"] == "low" and x.get("equal") and not x["swept"]:
            add(False, x["price"], x["price"], "equal_low")
    if n >= 200:
        yr = bars[-250:]
        add(True, max(b[H] for b in yr), max(b[H] for b in yr), "52w_high")
        add(False, min(b[L] for b in yr), min(b[L] for b in yr), "52w_low")
    w = bars[-20:]
    add(True, max(b[H] for b in w), max(b[H] for b in w), "range20")
    add(False, min(b[L] for b in w), min(b[L] for b in w), "range20")
    sz = sorted(_merge(sup, p.level_merge_pct), key=lambda z: -z["price_high"])[: p.level_n]
    rz = sorted(_merge(res, p.level_merge_pct), key=lambda z: z["price_low"])[: p.level_n]
    return {"supports": [_fmt(z) for z in sz], "resistances": [_fmt(z) for z in rz]}
