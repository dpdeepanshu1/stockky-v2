"""entry_engine/top_picks.py - group338: the "Top picks" cards (best 3 to 5) for the Board tab, same card shape as position-stocks.

The group332/337 shortlist (entry_engine/shortlist.py) holds the last Gate-6 cycle's risk-approved picks with entry / stop / target / R:R,
but only after a cycle has run, and without the live price, change %, zone or sector. This module
  * takes those picks while they are fresh (TOP_PICKS_FRESH_S) and adds the live Market Board row (ltp, change %, zone, sector, spark),
  * tops the list up from the strongest bullish board rows when fewer than TOP_PICKS_MIN_SHOWN are fresh, flagged source="board"
    (a preview: no risk engine / entry gate has looked at it, and it may not even be a candidate).
Pure functions, no I/O, never raises. Display only: it places no order and nothing in the entry path reads it.
Card keys: pick, symbol, name, sector, side, source (engine|board), status, outcome, score, conviction, rank, ltp, change_pct, zone_score,
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


def _zone_score(row: dict) -> Optional[float]:
    z = row.get("zone")
    if isinstance(z, dict) and _num(z.get("score")) is not None:
        return _num(z.get("score"))
    return _num(row.get("zone_score"))


def levels_from_row(row: Optional[dict]) -> dict:
    """entry / stop / target / rr from a board row: entry = ltp, stop = the zone's far edge, target = entry + rr x risk (None when not derivable)."""
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
        "name": row.get("name") or row.get("symbol"), "sector": row.get("sector"), "side": row.get("side"),
        "conviction": _r(row.get("conviction"), 0), "rank": row.get("rank"),
        "ltp": _r(row.get("ltp")), "change_pct": _r(row.get("change_pct")), "zone_score": _r(_zone_score(row), 0),
        "extended": bool(row.get("extended")), "in_position": bool(row.get("in_position")),
        "spark": [x for x in spark if isinstance(x, (int, float))][-40:],
        **levels_from_row(row),
    }


def _reasons(row: Optional[dict]) -> List[str]:
    out: List[str] = []
    if not row:
        return out
    conv, rank, zs, rr = _num(row.get("conviction")), row.get("rank"), _zone_score(row), _num(row.get("rr"))
    if row.get("side") == "bull" and conv is not None:
        out.append(f"board conviction {conv:.0f}" + (f", rank #{rank}" if rank is not None else ""))
    if zs is not None:
        out.append(f"zone score {zs:.0f}")
    if rr is not None:
        out.append(f"zone R:R {rr:.1f}")
    if row.get("side") == "bear":
        out.append("board is BEARISH on it")
    if row.get("extended"):
        out.append("extended past its zone - chasing risk")
    return out


def preview_score(row: dict) -> float:
    """0-100: 65 % board conviction + 35 % zone score (conviction alone when there is no zone), minus half the board-nudge cap x 2 when extended."""
    conv = _num(row.get("conviction")) or 0.0
    zs = _zone_score(row)
    s = conv if zs is None else 0.65 * conv + 0.35 * zs
    if row.get("extended"):
        s -= 10.0
    return max(0.0, min(100.0, s))


def preview(rows: Dict[str, dict], n: int, exclude: Iterable[str] = ()) -> List[dict]:
    """Strongest bullish board rows, best first: conviction >= TOP_PICKS_MIN_CONVICTION, priced, not held, not in `exclude`."""
    try:
        skip = {str(s) for s in exclude}
        cand = []
        for sym, r in (rows or {}).items():
            if not isinstance(r, dict) or sym in skip or r.get("side") != "bull" or r.get("in_position"):
                continue
            if _num(r.get("ltp")) is None or (_num(r.get("conviction")) or 0.0) < float(config.TOP_PICKS_MIN_CONVICTION):
                continue
            cand.append(dict(r, symbol=r.get("symbol") or sym))
        cand.sort(key=lambda r: -preview_score(r))
        return [{"symbol": r["symbol"], "source": "board", "status": "PREVIEW", "outcome": None,
                 "score": round(preview_score(r), 1), "reasons": _reasons(r), **_base(r)} for r in cand[: max(0, int(n))]]
    except Exception:  # noqa: BLE001
        return []


def from_engine(engine_rows: List[dict], rows: Dict[str, dict]) -> List[dict]:
    """The entry engine's own shortlist picks (SELECTED / RESERVE), enriched with the live board row. Its entry / stop / target / R:R win."""
    out = []
    for r in engine_rows or []:
        sym = r.get("symbol")
        if not sym:
            continue
        row = (rows or {}).get(sym)
        card = {"symbol": sym, "source": "engine", "status": r.get("status") or "SHORTLISTED", "outcome": r.get("outcome"),
                "score": r.get("composite_score"), "reasons": _reasons(row), **_base(row)}
        for k_card, k_eng in (("entry", "entry"), ("stop", "stop"), ("target", "target")):
            v = _r(r.get(k_eng))
            if v is not None:
                card[k_card] = v
        if _num(r.get("rr")) is not None:
            card["rr"] = _r(r.get("rr"))
        if card["conviction"] is None and r.get("conviction") is not None:
            card["conviction"] = _r(r.get("conviction"), 0)
        if card["name"] in (None, ""):
            card["name"] = sym
        extras = []
        if r.get("is_upper_circuit"):
            extras.append("upper circuit")
        if r.get("board_bonus"):
            extras.append(f"board {r['board_bonus']:+.1f}")
        if r.get("news_bonus"):
            extras.append(f"news {r['news_bonus']:+.1f}")
        if r.get("sector_bonus"):
            extras.append(f"US sector {r['sector_bonus']:+.1f}")
        card["reasons"] = card["reasons"] + extras
        out.append(card)
    return out


def build(snapshot: dict, rows: Dict[str, dict], now: float) -> dict:
    """snapshot: entry_engine.shortlist.snapshot(mode). Returns the card list for GET /top-picks/{mode}."""
    try:
        n = max(1, min(10, int(config.TOP_PICKS_N)))
        at = snapshot.get("as_of")
        fresh = bool(at) and (now - float(at)) <= float(config.TOP_PICKS_FRESH_S)
        picks = from_engine(snapshot.get("picks") or [], rows)[:n] if fresh else []
        engine_n = len(picks)
        if len(picks) < min(int(config.TOP_PICKS_MIN_SHOWN), n):
            picks += preview(rows, n - len(picks), exclude=[p["symbol"] for p in picks])
        for i, p in enumerate(picks, 1):
            p["pick"] = i
        return {"picks": picks, "engine_picks": engine_n, "preview_picks": len(picks) - engine_n, "engine_fresh": fresh}
    except Exception:  # noqa: BLE001
        return {"picks": [], "engine_picks": 0, "preview_picks": 0, "engine_fresh": False}
