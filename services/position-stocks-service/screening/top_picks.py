"""screening/top_picks.py - group338: the "Top picks" cards (best 3 to 5) for the Board tab.

The group332 shortlist only exists once an entry cycle has run, and its rows carry no price, zone or stop/target, so the Board tab could
not show a useful pick card before the market opened, when the engine was not armed, or between cycles. This module builds ONE card list:
  * ENGINE picks - the last entry cycle's shortlist (what the scalper really tried, with its outcome), while it is fresh
                   (TOP_PICKS_FRESH_S), enriched with the live Market Board row (ltp, change %, zone, stop, target, R:R);
  * BOARD picks  - when the engine has fewer than TOP_PICKS_MIN_SHOWN fresh picks, the strongest bullish board rows fill the list, flagged
                   source="board" (a preview: the entry gates have not looked at them).
Pure functions, no I/O, never raises. Display only: it places no order and no gate reads it.

Card shape (same keys in position-stocks and real-trade so the frontend has one normaliser):
  pick, symbol, name, sector, side, source (engine|board), status, outcome, score, conviction, rank, ltp, change_pct, zone_score,
  entry, stop, target, rr, extended, in_position, spark, reasons[]"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

import config


def _num(v) -> Optional[float]:
    try:
        f = float(v)
        return f if f == f and abs(f) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _r(v, d: int = 2) -> Optional[float]:
    f = _num(v)
    return None if f is None else round(f, d)


def board_index(payload: Optional[dict]) -> Dict[str, dict]:
    """symbol -> board row; a bullish row wins when a symbol is in both lists."""
    idx: Dict[str, dict] = {}
    if not isinstance(payload, dict):
        return idx
    for key, side in (("bearish", "bear"), ("bullish", "bull")):
        for r in payload.get(key) or []:
            if isinstance(r, dict) and r.get("symbol"):
                idx[str(r["symbol"])] = dict(r, side=r.get("side") or side)
    return idx


def levels_from_row(row: Optional[dict]) -> dict:
    """entry / stop / target / rr from a board row: entry = ltp, stop = the zone's far edge (invalidation), target = entry + rr x risk.
    Any part that cannot be derived is None (the card then shows '-')."""
    if not row:
        return {"entry": None, "stop": None, "target": None, "rr": None}
    ltp, inv, rr = _num(row.get("ltp")), _num(row.get("invalidation")), _num(row.get("rr"))
    stop = inv if (ltp is not None and inv is not None and 0 < inv < ltp) else None
    target = None
    if ltp is not None and stop is not None and rr is not None and rr > 0:
        target = ltp + rr * (ltp - stop)
    return {"entry": _r(ltp), "stop": _r(stop), "target": _r(target), "rr": _r(rr)}


def _base(row: Optional[dict]) -> dict:
    row = row or {}
    spark = row.get("spark") or []
    return {
        "name": row.get("name") or row.get("symbol"), "sector": row.get("sector"),
        "side": row.get("side"), "conviction": _r(row.get("conviction"), 0), "rank": row.get("rank"),
        "ltp": _r(row.get("ltp")), "change_pct": _r(row.get("change_pct")), "zone_score": _r(_zone_score(row), 0),
        "extended": bool(row.get("extended")), "in_position": bool(row.get("in_position")),
        "spark": [x for x in spark if isinstance(x, (int, float))][-40:],
        **levels_from_row(row),
    }


def _zone_score(row: dict) -> Optional[float]:
    z = row.get("zone")
    if isinstance(z, dict) and _num(z.get("score")) is not None:
        return _num(z.get("score"))
    return _num(row.get("zone_score"))


def _reasons(row: Optional[dict]) -> List[str]:
    out: List[str] = []
    if not row:
        return out
    conv, rank, zs = _num(row.get("conviction")), row.get("rank"), _zone_score(row)
    if row.get("side") == "bull" and conv is not None:
        out.append(f"board conviction {conv:.0f}" + (f", rank #{rank}" if rank is not None else ""))
    if zs is not None:
        out.append(f"zone score {zs:.0f}")
    rr = _num(row.get("rr"))
    if rr is not None:
        out.append(f"R:R {rr:.1f}")
    if row.get("side") == "bear":
        out.append("board is BEARISH on it")
    if row.get("extended"):
        out.append("extended past its zone - chasing risk")
    return out


def preview_score(row: dict) -> float:
    """0-100: 65 % board conviction + 35 % zone score (conviction alone when there is no zone), minus the chase penalty."""
    conv = _num(row.get("conviction")) or 0.0
    zs = _zone_score(row)
    s = conv if zs is None else 0.65 * conv + 0.35 * zs
    if row.get("extended"):
        s -= float(config.SHORTLIST_EXTENDED_PENALTY)
    return max(0.0, min(100.0, s))


def preview(board_payload: Optional[dict], n: int, exclude: Iterable[str] = ()) -> List[dict]:
    """The strongest bullish board rows, best first: conviction >= TOP_PICKS_MIN_CONVICTION, has a price, not already held, not in `exclude`."""
    try:
        skip = {str(s) for s in exclude}
        rows = []
        for r in (board_payload or {}).get("bullish") or []:
            if not isinstance(r, dict) or not r.get("symbol") or r["symbol"] in skip or r.get("in_position"):
                continue
            if _num(r.get("ltp")) is None or (_num(r.get("conviction")) or 0.0) < float(config.TOP_PICKS_MIN_CONVICTION):
                continue
            rows.append(dict(r, side=r.get("side") or "bull"))
        rows.sort(key=lambda r: -preview_score(r))
        out = []
        for r in rows[: max(0, int(n))]:
            out.append({"symbol": r["symbol"], "source": "board", "status": "PREVIEW", "outcome": None,
                        "score": round(preview_score(r), 1), "reasons": _reasons(r), **_base(r)})
        return out
    except Exception:  # noqa: BLE001 - visibility must never break a request
        return []


def from_engine(engine_rows: List[dict], idx: Dict[str, dict]) -> List[dict]:
    """The scalper's own shortlist rows (group332/337), each enriched with its live board row."""
    out = []
    for r in engine_rows or []:
        sym = r.get("symbol")
        if not sym:
            continue
        row = idx.get(sym)
        card = {"symbol": sym, "source": "engine", "status": "SHORTLISTED", "outcome": r.get("outcome"),
                "score": r.get("score"), "reasons": _reasons(row), **_base(row)}
        # the engine's own view wins where it has one (it ranked this row when it chose the pick)
        if r.get("board_conviction") is not None and card["conviction"] is None:
            card["conviction"] = _r(r.get("board_conviction"), 0)
        if r.get("board_rank") is not None and card["rank"] is None:
            card["rank"] = r.get("board_rank")
        if r.get("extended"):
            card["extended"] = True
        out.append(card)
    return out


def build(engine_rows: List[dict], engine_at: Optional[float], board_payload: Optional[dict], now: float) -> dict:
    """The card list for GET /top-picks: fresh engine picks first, board preview picks fill up to TOP_PICKS_N."""
    try:
        n = max(1, min(10, int(config.TOP_PICKS_N)))
        fresh = bool(engine_at) and (now - float(engine_at)) <= float(config.TOP_PICKS_FRESH_S)
        idx = board_index(board_payload)
        picks = from_engine(engine_rows, idx)[:n] if fresh else []
        engine_n = len(picks)
        if len(picks) < min(int(config.TOP_PICKS_MIN_SHOWN), n):
            picks += preview(board_payload, n - len(picks), exclude=[p["symbol"] for p in picks])
        for i, p in enumerate(picks, 1):
            p["pick"] = i
        return {"picks": picks, "engine_picks": engine_n, "preview_picks": len(picks) - engine_n, "engine_fresh": fresh}
    except Exception:  # noqa: BLE001
        return {"picks": [], "engine_picks": 0, "preview_picks": 0, "engine_fresh": False}
