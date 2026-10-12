"""screening/shortlist.py - group332: the "best 3 to 5" step between the quality gate and the entry attempt.

Before: the scalper walked the quality-gate passers in raw scan order and entered the first one that cleared capital; the Market
Board's own verdict (rank, conviction, zone, chase) was only read AFTER the pick, by the chase guard. Now the passers are ranked
against each other and the best SHORTLIST_MAX (default 5; all of them when fewer passed, never padded) are tried best-first.

Score 0-100, each part used only when its data exists, weights renormalised over the parts present:
  scan    screener composite_score as a share of the best passer's      SHORTLIST_W_SCAN    (0.40)
  board   Market Board bullish conviction                               SHORTLIST_W_BOARD   (0.30)
  quality mean of fundamental / technical score                         SHORTLIST_W_QUALITY (0.20)
  zone    Market Board zone score                                       SHORTLIST_W_ZONE    (0.10)
Adjust: board ranks it BEARISH -SHORTLIST_BEAR_PENALTY (scalper is long-only); EXTENDED past its zone -SHORTLIST_EXTENDED_PENALTY.
It never adds a candidate and never lowers a gate; any error falls back to the original order. SHORTLIST_ENABLED=0 = old behaviour."""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Sequence, Tuple

import config

logger = logging.getLogger("position-stocks-shortlist")


def _board_index(payload: Optional[dict]) -> Dict[str, dict]:
    """symbol -> board row; a bullish row wins when the symbol is in both lists."""
    idx: Dict[str, dict] = {}
    if not isinstance(payload, dict):
        return idx
    for key, side in (("bearish", "bear"), ("bullish", "bull")):
        for r in payload.get(key) or []:
            if isinstance(r, dict) and r.get("symbol"):
                idx[str(r["symbol"])] = dict(r, side=r.get("side") or side)
    return idx


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _zone_score(row: dict) -> Optional[float]:
    z = row.get("zone")
    if isinstance(z, dict):
        return _num(z.get("score"))
    return _num(row.get("zone_score"))


def score_one(cand, quality, brow: Optional[dict], best_scan: float) -> dict:
    parts: Dict[str, float] = {}
    scan = _num(getattr(cand, "composite_score", None))
    if scan is not None and best_scan > 0:
        parts["scan"] = max(0.0, min(100.0, scan / best_scan * 100.0))
    if brow and brow.get("side") == "bull":
        conv = _num(brow.get("conviction"))
        if conv is not None:
            parts["board"] = max(0.0, min(100.0, conv))
        zs = _zone_score(brow)
        if zs is not None:
            parts["zone"] = max(0.0, min(100.0, zs))
    qs = [v for v in (_num(getattr(quality, "fundamental_score", None)), _num(getattr(quality, "technical_score", None))) if v is not None]
    if qs:
        parts["quality"] = max(0.0, min(100.0, sum(qs) / len(qs)))
    w = {"scan": config.SHORTLIST_W_SCAN, "board": config.SHORTLIST_W_BOARD,
         "quality": config.SHORTLIST_W_QUALITY, "zone": config.SHORTLIST_W_ZONE}
    tw = sum(w[k] for k in parts)
    base = sum(parts[k] * w[k] for k in parts) / tw if tw > 0 else 0.0
    adjust = 0.0
    extended = False
    if brow:
        if brow.get("side") == "bear":
            adjust -= float(config.SHORTLIST_BEAR_PENALTY)
        if "EXTENDED" in str(brow.get("label") or brow.get("rule") or "").upper() or brow.get("extended"):
            extended = True
            adjust -= float(config.SHORTLIST_EXTENDED_PENALTY)
    return {"score": round(max(0.0, min(100.0, base + adjust)), 1), "parts": dict(parts, adjust=adjust), "extended": extended}


def build(passers: Sequence[Tuple[object, object]], board_payload: Optional[dict] = None) -> List[dict]:
    """passers: [(candidate, quality)] in scan-rank order. Returns shortlist rows best-first, each
    {pick, symbol, score, scan_rank, board_rank, board_conviction, board_side, extended, parts, cand, quality}.
    Fail-open: any error returns the passers in the original order."""
    items = list(passers)
    try:
        idx = _board_index(board_payload)
        best_scan = max([_num(getattr(c, "composite_score", None)) or 0.0 for c, _q in items] or [0.0])
        rows = []
        for i, (c, q) in enumerate(items):
            brow = idx.get(getattr(c, "symbol", ""))
            s = score_one(c, q, brow, best_scan)
            rows.append({"symbol": c.symbol, "score": s["score"], "scan_rank": i + 1,
                         "board_rank": brow.get("rank") if brow else None,
                         "board_conviction": _num(brow.get("conviction")) if brow else None,
                         "board_side": brow.get("side") if brow else None,
                         "extended": s["extended"], "parts": s["parts"], "cand": c, "quality": q})
        floor = float(config.SHORTLIST_MIN_SCORE or 0.0)
        rows.sort(key=lambda r: (-r["score"], r["scan_rank"]))      # stable: equal scores keep scan order
        kept = [r for r in rows if r["score"] >= floor] or rows[:1]
        if getattr(config, "SHORTLIST_DROP_BEARISH", True):
            # group338: long-only scalper - never trade against the board when a non-bearish passer is available.
            _longs = [r for r in kept if r["board_side"] != "bear"]
            if _longs:
                kept = _longs
        kept = kept[: max(1, int(config.SHORTLIST_MAX))]
        for n, r in enumerate(kept, 1):
            r["pick"] = n
        return kept
    except Exception as e:  # noqa: BLE001
        logger.warning("shortlist: scoring failed, keeping scan order (%s: %s)", type(e).__name__, e)
        out = []
        for n, (c, q) in enumerate(items[: max(1, int(config.SHORTLIST_MAX))], 1):
            out.append({"pick": n, "symbol": c.symbol, "score": None, "scan_rank": n, "board_rank": None, "board_conviction": None,
                        "board_side": None, "extended": False, "parts": {}, "cand": c, "quality": q})
        return out


def public(rows: Sequence[dict]) -> List[dict]:
    """JSON-safe copy (no candidate / quality objects) for GET /shortlist and the cycle stage."""
    return [{k: v for k, v in r.items() if k not in ("cand", "quality")} for r in rows]
