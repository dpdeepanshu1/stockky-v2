"""E578 (round 553, Tredye plan P4): GET /market/global-cues - the read-only global cues strip (S&P 500, Nasdaq, Brent crude, dollar index, USD/INR, gold).
DISPLAY ONLY: it feeds the Home card and the Market Weather page and nothing else. No login (not in admin_guard.PROTECTED, like the other market reads).
The prices come only from market-data-service GET /global-strip (Yahoo daily bars). Each row keeps its bar_date so the page can say how old the number is. A row that is
unusable (not a number, not above zero, a day move over 25 %) is dropped to "missing"; market-data unreachable gives an empty set with `error`, never a made-up value.
Switch: GLOBAL_STRIP_ENABLED=0 answers an empty set (market-data reads the same switch).
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter

logger = logging.getLogger("api-gateway")

global_cues_router = APIRouter()

SYMBOLS = ("^GSPC", "^IXIC", "BZ=F", "DX-Y.NYB", "INR=X", "GC=F")
MAX_ABS_MOVE_PCT = 25.0
_IST = timezone(timedelta(hours=5, minutes=30))


def _enabled() -> bool:
    return (os.getenv("GLOBAL_STRIP_ENABLED") or "").strip().lower() not in ("0", "false", "no", "off")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def clean_rows(body) -> dict:
    """{SYMBOL: {price, previous_close, day_change_pct, bar_date}} for the known symbols with a usable row. Never raises."""
    out: dict = {}
    rows = (body or {}).get("rows") if isinstance(body, dict) else None
    if not isinstance(rows, dict):
        return out
    for sym in SYMBOLS:
        r = rows.get(sym)
        if not isinstance(r, dict):
            continue
        price, prev, pct = _num(r.get("price")), _num(r.get("previous_close")), _num(r.get("day_change_pct"))
        bar = r.get("bar_date")
        if price is None or price <= 0 or pct is None or abs(pct) > MAX_ABS_MOVE_PCT:
            continue
        if not isinstance(bar, str) or len(bar) != 10:      # no date = no way to say how old it is = not shown
            continue
        out[sym] = {"price": price, "previous_close": prev if (prev is not None and prev > 0) else None, "day_change_pct": pct, "bar_date": bar}
    return out


def _fetch() -> dict:
    base = (os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/") or "http://market-data-service:8001"
    try:
        r = httpx.get(f"{base}/global-strip", timeout=25)
        if r.status_code != 200:
            return {"error": f"market-data answered {r.status_code}"}
        return {"body": r.json()}
    except Exception as e:  # noqa: BLE001
        logger.debug("global-cues strip: %s", e)
        return {"error": "market-data unreachable"}


@global_cues_router.get("/market/global-cues")
async def market_global_cues():
    """Rows for the strip, `missing` for the rest, `fetched_at` (IST) for the time stamp. Read-only."""
    now_ist = datetime.now(_IST)
    base = {"fetched_at": now_ist.isoformat(timespec="seconds"), "today_ist": now_ist.date().isoformat()}
    if not _enabled():
        return {**base, "rows": {}, "missing": list(SYMBOLS), "enabled": False}
    got = await asyncio.to_thread(_fetch)
    rows = clean_rows(got.get("body"))
    out = {**base, "rows": rows, "missing": [s for s in SYMBOLS if s not in rows], "enabled": True}
    if got.get("error"):
        out["error"] = got["error"]
    return out
