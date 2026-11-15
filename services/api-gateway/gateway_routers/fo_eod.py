"""E579 (round 554, Tredye plan P5): GET /market/fo-eod - the end-of-day F&O pack (OI Flow and futures basis) from the F&O bhavcopy. DISPLAY ONLY: it feeds one F&O
page section and nothing else. No login (not in admin_guard.PROTECTED, like the other market reads). All numbers come from market-data-service GET /fo-eod; this
route only checks them. A number that is not finite is None (the page draws a dash, never 0); the build-up label is recomputed from the cleaned price and OI
changes so a label can never disagree with the numbers shown; rows go through only with a real trade date on the pack. market-data unreachable gives an empty set
with `error`, never a made-up value. Switch: FO_EOD_ENABLED=0 answers an empty set (market-data reads the same switch).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter

logger = logging.getLogger("api-gateway")

fo_eod_router = APIRouter()

_IST = timezone(timedelta(hours=5, minutes=30))
_SYM = re.compile(r"^[A-Z0-9&\-]{1,20}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
BUILDUPS = ("long_buildup", "short_covering", "short_buildup", "long_unwinding")
MAX_ROWS = 400
MAX_ABS_PRICE_PCT = 60.0      # a one-day futures move beyond this is a data fault, not a move
MAX_ABS_BASIS_PCT = 25.0      # a stock future 25 % away from its cash price is a data fault (wrong underlying, stale cash), not carry


def _enabled() -> bool:
    return (os.getenv("FO_EOD_ENABLED") or "").strip().lower() not in ("0", "false", "no", "off")


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _classify(px, oi):
    if px is None or oi is None or px == 0 or oi == 0:
        return None
    if px > 0:
        return "long_buildup" if oi > 0 else "short_covering"
    return "short_buildup" if oi > 0 else "long_unwinding"


def clean_row(r):
    """One checked row, or None when it has no usable symbol. Unusable numbers become None; nothing is repaired."""
    if not isinstance(r, dict):
        return None
    sym = str(r.get("symbol") or "").strip().upper()
    if not _SYM.match(sym):
        return None
    px = _num(r.get("price_chg_pct"))
    if px is not None and abs(px) > MAX_ABS_PRICE_PCT:
        px = None
    oi = _num(r.get("oi"))
    if oi is not None and oi <= 0:
        oi = None
    oi_chg_pct = _num(r.get("oi_chg_pct"))
    basis = _num(r.get("basis_pct"))
    spot = _num(r.get("spot"))
    ann = _num(r.get("annualised_pct"))
    if basis is not None and (abs(basis) > MAX_ABS_BASIS_PCT or spot is None or spot <= 0):
        basis = ann = None
    if basis is None:
        ann = None
    days = _num(r.get("days_to_expiry"))
    days = int(days) if days is not None and days >= 0 else None
    fut = _num(r.get("fut_close"))
    roll = _num(r.get("roll_pct"))
    exp = r.get("near_expiry")
    return {
        "symbol": sym, "near_expiry": exp if isinstance(exp, str) and _ISO.match(exp) else None, "days_to_expiry": days,
        "fut_close": fut if fut is not None and fut > 0 else None, "price_chg_pct": px, "oi": oi, "oi_chg": _num(r.get("oi_chg")),
        "oi_chg_pct": oi_chg_pct, "buildup": _classify(px, oi_chg_pct), "spot": spot if spot is not None and spot > 0 else None,
        "basis_pct": basis, "annualised_pct": ann, "roll_pct": roll if roll is not None and 0 <= roll <= 100 else None,
        "rolling": bool(r.get("rolling")) if days is not None else None,
    }


def clean_pack(body) -> dict:
    """{trade_date, expected_date, stale, rows, counts, fetched_at?}. Without a real trade date there are no rows (no number without a time). Never raises."""
    empty = {"trade_date": None, "expected_date": None, "stale": False, "rows": [], "counts": {}}
    if not isinstance(body, dict):
        return empty
    td, ex = body.get("trade_date"), body.get("expected_date")
    out = dict(empty)
    out["expected_date"] = ex if isinstance(ex, str) and _ISO.match(ex) else None
    if not (isinstance(td, str) and _ISO.match(td)):
        return out
    rows = [c for c in (clean_row(r) for r in (body.get("rows") if isinstance(body.get("rows"), list) else [])[:MAX_ROWS]) if c]
    counts = {b: sum(1 for r in rows if r["buildup"] == b) for b in BUILDUPS}
    counts["stocks"] = len(rows)
    counts["no_basis"] = sum(1 for r in rows if r["basis_pct"] is None)
    out.update(trade_date=td, rows=rows, counts=counts, stale=bool(out["expected_date"] and td < out["expected_date"]))
    fa = body.get("fetched_at")
    if isinstance(fa, str) and fa:
        out["fetched_at"] = fa[:40]
    return out


def _fetch() -> dict:
    base = (os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/") or "http://market-data-service:8001"
    try:
        r = httpx.get(f"{base}/fo-eod", timeout=60)
        if r.status_code != 200:
            return {"error": f"market-data answered {r.status_code}"}
        return {"body": r.json()}
    except Exception as e:  # noqa: BLE001
        logger.debug("fo-eod: %s", e)
        return {"error": "market-data unreachable"}


@fo_eod_router.get("/market/fo-eod")
async def market_fo_eod():
    """OI Flow and basis rows, each pack stamped with its file's trade date, `stale` when older than the session expected. Read-only."""
    now_ist = datetime.now(_IST)
    base = {"now_ist": now_ist.isoformat(timespec="seconds"), "today_ist": now_ist.date().isoformat()}
    if not _enabled():
        return {**base, "trade_date": None, "expected_date": None, "stale": False, "rows": [], "counts": {}, "enabled": False}
    got = await asyncio.to_thread(_fetch)
    body = got.get("body")
    out = {**base, **clean_pack(body), "enabled": not (isinstance(body, dict) and body.get("enabled") is False)}
    err = got.get("error") or (body.get("error") if isinstance(body, dict) and isinstance(body.get("error"), str) else None)
    if err and (not out["rows"] or out["stale"]):
        out["error"] = err[:200]
    return out
