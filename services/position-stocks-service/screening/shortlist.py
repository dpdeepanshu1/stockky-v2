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


# ---------------------------------------------------------------------------------------------------------------------------------------------
# E107 (P2 item 17): the SHORT side. Ranks the bearish short candidates against each other so the capital and the SHORT_MAX_POSITIONS slots go to the
# strongest one first. It only ORDERS (and trims to SHORT_SHORTLIST_MAX); it never adds a candidate and never loosens a gate: every name still walks the
# fail-closed short gates in orders/short_gates.py. Any error falls back to the original scan order. SHORT_SHORTLIST_ENABLED=0 = scan order.
#   scan      composite_score share of the best candidate (the fall scan)     0.35
#   board     Market Board BEARISH conviction                                  0.35
#   zone      Market Board zone score (supply zone above, room, rr)            0.15
#   weakness  100 - technical_score (a weak chart is a better short)           0.15
# Each part is used only when its data exists; weights are renormalised over the parts present. Adjust: -SHORT_SHORTLIST_EXTENDED_PENALTY when the board
# marks it extended (already far below its zone: a late short chases the move). Score 0-100, ties keep scan order.
SHORT_WEIGHTS = {"scan": 0.35, "board": 0.35, "zone": 0.15, "weakness": 0.15}


def score_short(cand, quality, brow: Optional[dict], best_scan: float) -> dict:
    parts: Dict[str, float] = {}
    scan = _num(getattr(cand, "composite_score", None))
    if scan is not None and best_scan > 0:
        parts["scan"] = max(0.0, min(100.0, scan / best_scan * 100.0))
    if brow and brow.get("side") == "bear":
        conv = _num(brow.get("conviction"))
        if conv is not None:
            parts["board"] = max(0.0, min(100.0, conv))
        zs = _zone_score(brow)
        if zs is not None:
            parts["zone"] = max(0.0, min(100.0, zs))
    tech = _num(getattr(quality, "technical_score", None))
    if tech is not None:
        parts["weakness"] = max(0.0, min(100.0, 100.0 - tech))
    tw = sum(SHORT_WEIGHTS[k] for k in parts)
    base = sum(parts[k] * SHORT_WEIGHTS[k] for k in parts) / tw if tw > 0 else 0.0
    adjust, extended = 0.0, False
    if brow and (brow.get("extended") or "EXTENDED" in str(brow.get("label") or brow.get("rule") or "").upper()):
        extended = True
        adjust -= float(getattr(config, "SHORT_SHORTLIST_EXTENDED_PENALTY", 15.0))
    return {"score": round(max(0.0, min(100.0, base + adjust)), 1), "parts": dict(parts, adjust=adjust), "extended": extended}


def build_short(passers: Sequence[Tuple[object, object]], bear_index: Optional[Dict[str, dict]] = None) -> List[dict]:
    """passers: [(candidate, quality)] in scan order (quality may be None); bear_index: symbol -> Market Board bearish row. Returns rows best-first, trimmed to
    SHORT_SHORTLIST_MAX: {pick, symbol, score, scan_rank, board_rank, board_conviction, extended, parts, cand, quality}. With SHORT_SHORTLIST_ENABLED off, or on any
    error, the passers come back in the original order (trimmed the same way, score None). Never raises."""
    items = list(passers)
    cap = max(1, int(getattr(config, "SHORT_SHORTLIST_MAX", 5)))
    try:
        if not getattr(config, "SHORT_SHORTLIST_ENABLED", True):
            raise RuntimeError("short shortlist off")
        idx = bear_index or {}
        best_scan = max([_num(getattr(c, "composite_score", None)) or 0.0 for c, _q in items] or [0.0])
        rows = []
        for i, (c, q) in enumerate(items):
            brow = idx.get(getattr(c, "symbol", ""))
            s = score_short(c, q, brow, best_scan)
            rows.append({"symbol": c.symbol, "score": s["score"], "scan_rank": i + 1, "board_rank": brow.get("rank") if brow else None,
                         "board_conviction": _num(brow.get("conviction")) if brow else None, "extended": s["extended"], "parts": s["parts"],
                         "cand": c, "quality": q})
        rows.sort(key=lambda r: (-r["score"], r["scan_rank"]))
        kept = rows[:cap]
    except Exception as e:  # noqa: BLE001
        if getattr(config, "SHORT_SHORTLIST_ENABLED", True):
            logger.warning("short shortlist: scoring failed, keeping scan order (%s: %s)", type(e).__name__, e)
        kept = [{"symbol": c.symbol, "score": None, "scan_rank": n, "board_rank": None, "board_conviction": None, "extended": False, "parts": {},
                 "cand": c, "quality": q} for n, (c, q) in enumerate(items[:cap], 1)]
    for n, r in enumerate(kept, 1):
        r["pick"] = n
    return kept
