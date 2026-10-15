"""screening/preopen.py - 09:10 pre-market plan (09:08 until group 399): indicative gap snapshot + cache warm-up for the scalper (group 330, 2026-10-12).

WHY: this service did nothing between boot and the 09:15 bell, then paid for cold caches (previous-day candles, Nifty / VIX reads) on the first
scan, and had no view of which names were gapping hard. At PREOPEN_SNAPSHOT_TIME_IST (default 09:10 since group 399, before the bell) one best-effort job:
  1. WARMS  the Nifty market-gate read, the VIX read and the previous-day candle cache of the F&O universe (capped PREOPEN_MAX_SYMBOLS);
  2. records the gap (price vs previous close) of those symbols from market-data POST /quotes/bulk when the row's price is fresh
     (age <= PREOPEN_MAX_PRICE_AGE_S; a row with no readable age is NOT used). gap_reason() then refuses to chase a gap-up >= PREOPEN_GAP_UP_BLOCK_PCT
     or catch a gap-down >= PREOPEN_GAP_DOWN_BLOCK_PCT until PREOPEN_GAP_GATE_UNTIL_IST (default 10:00).
HONEST LIMIT: whether market-data holds a fresh indicative price at 09:08 depends on the broker feed; a symbol without one has no gap recorded and is
unaffected. Everything fails open, is in-memory and runs once per IST day. Off with PREOPEN_SNAPSHOT_ENABLED=0.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import config
from tz_utils import ist_now, ist_time_at_or_after, ist_today_str, is_nse_holiday, parse_hhmm

logger = logging.getLogger("position-stocks-preopen")

_snap: dict = {"day": None, "gaps": {}, "captured_at": None, "warmed": 0}


def reset() -> None:
    _snap.update({"day": None, "gaps": {}, "captured_at": None, "warmed": 0})


def gap_pct(price, prev_close) -> Optional[float]:
    try:
        p, c = float(price), float(prev_close)
        if not (math.isfinite(p) and math.isfinite(c)) or p <= 0 or c <= 0:
            return None
        g = (p - c) / c * 100.0
        return g if abs(g) <= 60.0 else None
    except (TypeError, ValueError):
        return None


def due(now: Optional[datetime] = None) -> bool:
    """True from PREOPEN_SNAPSHOT_TIME_IST until the 09:15 bell on a trading day while today's snapshot has not been taken. Never raises."""
    try:
        if not config.PREOPEN_SNAPSHOT_ENABLED:
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n) or _snap["day"] == ist_today_str(now):
            return False
        return (ist_time_at_or_after(parse_hhmm(config.PREOPEN_SNAPSHOT_TIME_IST, 9, 10), now)
                and not ist_time_at_or_after(parse_hhmm("09:15", 9, 15), now))
    except Exception:  # noqa: BLE001
        return False


def _row_gap(row, max_age_s: float) -> Optional[float]:
    """Gap of one /quotes/bulk row when its price is provably fresh, else None. Never raises."""
    try:
        if not isinstance(row, dict):
            return None
        age = row.get("age_s")
        if isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age < 0 or age > max_age_s:
            return None
        return gap_pct(row.get("price"), row.get("previous_close") or row.get("prev_close"))
    except Exception:  # noqa: BLE001
        return None


def record(rows, now: Optional[datetime] = None) -> int:
    """Store the gap of every fresh row ({"symbol", "price", "previous_close", "age_s"}); returns how many. Never raises."""
    try:
        max_age = float(config.PREOPEN_MAX_PRICE_AGE_S)
        gaps = {}
        for row in rows or []:
            g = _row_gap(row, max_age)
            sym = str((row or {}).get("symbol") or "").upper().replace(".NS", "").strip() if isinstance(row, dict) else ""
            if g is not None and sym:
                gaps[sym] = g
        _snap.update({"day": ist_today_str(now), "gaps": gaps,
                      "captured_at": (now or datetime.now(timezone.utc)).isoformat()})
        return len(gaps)
    except Exception as e:  # noqa: BLE001
        logger.debug("preopen.record failed (ignored): %s", e)
        return 0


def status() -> dict:
    return {"day": _snap["day"], "symbols_with_gap": len(_snap["gaps"]), "captured_at": _snap["captured_at"],
            "warmed": _snap["warmed"], "gaps": dict(sorted(_snap["gaps"].items(), key=lambda kv: -abs(kv[1]))[:15])}


def gap_reason(symbol: str, now: Optional[datetime] = None) -> Optional[str]:
    """A "PREOPEN_GAP_UP/DOWN:..." reason while the gate window is open, else None. Never raises."""
    try:
        if not config.PREOPEN_SNAPSHOT_ENABLED or _snap["day"] != ist_today_str(now):
            return None
        g = _snap["gaps"].get(str(symbol or "").upper().replace(".NS", "").strip())
        if g is None:
            return None
        if ist_time_at_or_after(parse_hhmm(config.PREOPEN_GAP_GATE_UNTIL_IST, 10, 0), now):
            return None
        up, dn = float(config.PREOPEN_GAP_UP_BLOCK_PCT or 0), float(config.PREOPEN_GAP_DOWN_BLOCK_PCT or 0)
        if up > 0 and g >= up:
            return f"PREOPEN_GAP_UP:indicative open {g:+.1f}% vs previous close >= +{up:.1f}% - not chasing a big gap-up"
        if dn > 0 and g <= -dn:
            return f"PREOPEN_GAP_DOWN:indicative open {g:+.1f}% vs previous close <= -{dn:.1f}% - not catching a gap-down"
        return None
    except Exception:  # noqa: BLE001
        return None


def _universe() -> list:
    try:
        from feed import fno_list
        return sorted(fno_list.get_fno_symbols())[: max(0, int(config.PREOPEN_MAX_SYMBOLS))]
    except Exception:  # noqa: BLE001
        return []


async def _fetch_rows(symbols: list) -> list:
    import httpx
    rows: list = []
    async with httpx.AsyncClient() as client:
        for i in range(0, len(symbols), 50):
            try:
                r = await client.post(f"{config.MARKET_DATA_URL}/quotes/bulk", json={"symbols": symbols[i:i + 50]},
                                      timeout=float(config.PREOPEN_FETCH_TIMEOUT_S))
                if r.status_code == 200:
                    body = r.json()
                    rows.extend(q for q in ((body or {}).get("quotes") or []) if isinstance(q, dict))
            except Exception as e:  # noqa: BLE001
                logger.info("preopen: bulk quote chunk failed (%s: %s) - skipped", type(e).__name__, e)
    return rows


async def run(symbols: Optional[list] = None) -> dict:
    """Warm the caches and take the gap snapshot. Never raises; marks the day even when nothing priced so it runs once."""
    try:
        syms = list(symbols) if symbols is not None else _universe()
        warmed = 0
        try:
            from screening import prev_day, trade_gates, vix_gate
            await vix_gate.refresh()
            await trade_gates.market_gate_reject()          # fills the Nifty cache
            for s in syms:
                prev_day.get(s)                             # background thread per symbol, never blocks
                warmed += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("preopen warm failed (ignored): %s", e)
        stored = record(await _fetch_rows(syms)) if syms else 0
        if _snap["day"] != ist_today_str():
            _snap.update({"day": ist_today_str(), "gaps": {}, "captured_at": datetime.now(timezone.utc).isoformat()})
        _snap["warmed"] = warmed
        logger.info("[preopen] snapshot: %d symbols, %d with a fresh gap, %d warmed", len(syms), stored, warmed)
        return status()
    except Exception as e:  # noqa: BLE001
        logger.warning("[preopen] snapshot failed (ignored): %s", e)
        _snap["day"] = ist_today_str()
        return status()
