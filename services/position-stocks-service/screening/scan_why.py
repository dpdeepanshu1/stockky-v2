"""screening/scan_why.py - E232: "why was SYMBOL not picked?" for one symbol, from what the scan itself reads right now plus today's scan-hit record.
Read only: no network, no DB, no orders. Every check mirrors the matching line in screening/engine.scan(); the first one that fails is the reason."""
from __future__ import annotations

from typing import Optional

import config
from feed import ws_client
from screening import engine, scan_hits


def _find(symbol: str) -> Optional[str]:
    """The spelling the feed uses for this symbol (RELIANCE / RELIANCE.NS), or None when the feed does not carry it."""
    want = engine._bare_symbol(symbol)
    subscribed = list(ws_client._token_to_symbol.values()) or list(ws_client._tick_buffers.keys())
    for s in subscribed:
        if engine._bare_symbol(s) == want:
            return s
    return None


def explain(symbol: str) -> dict:
    out: dict = {"symbol": engine._bare_symbol(symbol), "blocking": [], "notes": []}
    try:
        from feed import instrument_filter
        if instrument_filter.is_etf_or_fund(symbol):
            out["blocking"].append("ETF / fund: dropped from the universe (feed/instrument_filter)")
    except Exception:  # noqa: BLE001
        pass
    feed_sym = _find(symbol)
    out["subscribed"] = feed_sym is not None
    if feed_sym is None:
        out["blocking"].append("not subscribed on the live feed (not in the stock list / no Dhan token): the scan cannot see it")
    else:
        buf = ws_client.get_tick_buffer(feed_sym)
        out["ticks_in_buffer"] = len(buf)
        ltp = buf[-1][1] if buf else None
        out["ltp"] = ltp
        if len(buf) < 2 or not ltp or ltp <= 0:
            out["blocking"].append("fewer than 2 ticks in the buffer: no price history yet")
        else:
            vol = ws_client.get_last_volume(feed_sym)
            out["day_volume"] = vol
            out["volume_floor"] = config.MIN_AVG_VOLUME
            if config.MIN_AVG_VOLUME > 0 and vol < config.MIN_AVG_VOLUME:
                out["blocking"].append(f"day volume {vol} is below the floor {config.MIN_AVG_VOLUME}")
            sp = engine._spread_pct(feed_sym, ltp)
            out["spread_pct"] = None if sp is None else round(sp, 3)
            if config.MAX_SPREAD_PCT > 0 and sp is not None and sp > config.MAX_SPREAD_PCT:
                out["blocking"].append(f"bid-ask spread {sp:.2f}% is above the cap {config.MAX_SPREAD_PCT}%")
            floor = max(float(config.MIN_STOCK_PRICE or 0.0), float(getattr(config, "ENTRY_MIN_PRICE", 0.0) or 0.0))
            if floor > 0 and ltp < floor:
                out["blocking"].append(f"price {ltp} is below the price floor {floor}")
            windows = {}
            any_cleared = False
            for w, thr in engine._WINDOW_THRESHOLDS.items():
                off = w in getattr(config, "DISABLED_SCAN_WINDOWS", ())
                pct = engine._rolling_pct_change(feed_sym, w)
                cleared = (not off) and pct is not None and abs(pct) >= thr
                any_cleared = any_cleared or cleared
                windows[f"{w}m"] = {"move_pct": None if pct is None else round(pct, 3), "needs": thr, "disabled": off, "cleared": cleared}
            out["windows"] = windows
            dm = engine._day_change_pct(feed_sym, ltp)
            out["day_move_pct"] = None if dm is None else round(dm, 3)
            out["day_mover"] = {"enabled": bool(config.DAY_MOVER_ENABLED), "min_pct": config.DAY_MOVER_MIN_PCT, "max_pct": config.DAY_MOVER_MAX_PCT}
            if dm is None:
                out["notes"].append("previous close unknown on the feed: the day-mover path cannot judge it")
            if not any_cleared:
                if dm is not None and config.DAY_MOVER_ENABLED and config.DAY_MOVER_MIN_PCT <= abs(dm) <= config.DAY_MOVER_MAX_PCT:
                    out["notes"].append(f"no rolling window cleared, but the day move {dm:+.2f}% qualifies it as a day mover")
                elif dm is not None and abs(dm) > config.DAY_MOVER_MAX_PCT:
                    out["blocking"].append(f"day move {dm:+.2f}% is beyond DAY_MOVER_MAX_PCT {config.DAY_MOVER_MAX_PCT} (circuit-chase guard)")
                else:
                    out["blocking"].append("no rolling window cleared its threshold and the day move is too small for the day-mover path")
    for side in ("long", "short"):
        h = scan_hits.get(out["symbol"], side)
        if h:
            out[f"hits_{side}"] = h
            if not h.get("checked_cycles"):
                out["notes"].append(f"{side}: listed by the scan {h['scans']} time(s), best rank {h['best_rank']} of {h.get('listed_of')}, never reached the quality gate (shortlist size {config.SHORTLIST_MAX})")
    out["picked_by_scan_today"] = bool(out.get("hits_long") or out.get("hits_short"))
    out["verdict"] = ("; ".join(out["blocking"]) if out["blocking"]
                      else ("listed by the scan today (see hits)" if out["picked_by_scan_today"] else "nothing blocks it right now; it simply has not cleared a window today"))
    return out
