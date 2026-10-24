"""entry_engine/structure_gate.py - daily market-structure gate on the entry path (group 351, 2026-10-13).

QUESTION IT ANSWERS: "good stock, bad entry?" The candidate already passed the scanners; this checks WHERE the price is relative to the
daily structure (technical service `market_structure` summary, flat `ms_*` keys): chasing above the nearest demand / imbalance zone, a
stretched price, no room to the next resistance, a poor structure R:R, a bearish daily structure.

LIVE: config.STRUCTURE_GATE_MODE / STRUCTURE_GATE_DEMO_MODE = enforce (default) | off (kill switch). enforce turns a rejection into a WAIT
(gate_tag="structure") and stores the verdict on decision.structure_json. There is no record-only (shadow) mode. Every threshold is a
hypothesis until the backtest / observation data calibrates it (scripts/structure_backtest.py).

NO DATA = NO OPINION: missing / unavailable / insufficient / stale quality, an old last bar, or a live price far from the structure close
(split / bad data) never blocks and is never bullish - unless STRUCTURE_REQUIRE_DATA=true (then it WAITs, tag structure_nodata).

LIVE ADJUSTMENT: the summary was computed at the structure close (ms_price). The live entry has moved since, so chase / stretch / room are
shifted by (live - ms_price) / ATR and the R:R is recomputed from ms_stop / ms_target at the live entry. Pure, never raises."""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config

logger = logging.getLogger("real-trade-structure-gate")

TAG = "structure"
TAG_NODATA = "structure_nodata"
_OK_QUALITIES = ("ok", "limited")


def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _pos(v) -> Optional[float]:
    x = _f(v)
    return x if x is not None and x > 0 else None


_OOS_CACHE: Dict[str, Any] = {}      # mode -> (monotonic ts, count)
_OOS_TTL_S = 600.0


def oos_count(db: Any, trade_mode: str) -> Optional[int]:
    """B15: structure observations for this mode whose outcome has been measured (ret_5d filled in). Cached 10 min. None = unreadable."""
    import time as _time
    key = str(trade_mode).upper()
    hit = _OOS_CACHE.get(key)
    if hit and _time.monotonic() - hit[0] < _OOS_TTL_S:
        return hit[1]
    try:
        import models as _models
        n = int(db.query(_models.StructureObservation).filter(
            _models.StructureObservation.mode == key, _models.StructureObservation.ret_5d.isnot(None)).count())
    except Exception:  # noqa: BLE001
        logger.warning("structure_gate: could not count out-of-sample observations", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return None
    _OOS_CACHE[key] = (_time.monotonic(), n)
    return n


def mode_for(trade_mode: str, db: Any = None) -> str:
    """enforce | log | off for REAL / DEMO. log = evaluate and record only (A9); anything else is enforce."""
    try:
        m = config.STRUCTURE_GATE_MODE if str(trade_mode).upper() == "REAL" else config.STRUCTURE_GATE_DEMO_MODE
        m = m if m in ("off", "log") else "enforce"
        # B15: enforce only with evidence. Callers that pass a db get the guard; without one the configured mode is returned unchanged.
        floor = int(getattr(config, "STRUCTURE_ENFORCE_MIN_OOS", 15) or 0)
        if m == "enforce" and db is not None and floor > 0:
            n = oos_count(db, trade_mode)
            if n is None or n < floor:
                logger.info("structure gate: enforce downgraded to log for %s (%s out-of-sample observations, need %d)", trade_mode,
                            "unreadable" if n is None else n, floor)
                return "log"
        return m
    except Exception:  # noqa: BLE001
        return "enforce"


def extract_summary(payload: Any) -> Optional[Dict[str, Any]]:
    """The flat ms_* summary carried on a candidate payload (dict or JSON text): `structure_summary`, or ms_* keys on the row itself."""
    try:
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        if not isinstance(payload, dict):
            return None
        s = payload.get("structure_summary")
        if isinstance(s, dict) and s.get("ms_quality"):
            return s
        flat = {k: v for k, v in payload.items() if isinstance(k, str) and k.startswith("ms_")}
        return flat if flat.get("ms_quality") else None
    except Exception:  # noqa: BLE001
        return None


def _age_days(asof) -> Optional[float]:
    """Days between the structure's last bar and now (None when unparseable)."""
    try:
        if asof is None:
            return None
        if isinstance(asof, (int, float)):
            ts = float(asof)
            ts = ts / 1000.0 if ts > 1e11 else ts
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            s = str(asof).strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001
        return None


def no_data_reason(summary: Optional[Dict[str, Any]], live_price) -> Optional[str]:
    """Why the summary cannot give an opinion, or None when it can."""
    if not isinstance(summary, dict):
        return "no structure summary"
    q = summary.get("ms_quality")
    if q not in _OK_QUALITIES:
        return f"structure quality '{q or 'unavailable'}'"
    age = _age_days(summary.get("ms_asof"))
    max_age = float(config.STRUCTURE_MAX_AGE_DAYS or 0.0)
    if max_age > 0 and age is not None and age > max_age:
        return f"structure last bar is {age:.0f}d old"
    px, msp = _pos(live_price), _pos(summary.get("ms_price"))
    if px is None or msp is None:
        return "no price to compare"
    dev = float(config.STRUCTURE_MAX_PRICE_DEV_PCT or 0.0)
    if dev > 0 and abs(px - msp) / msp * 100.0 > dev:
        return f"live price {px:.2f} is {abs(px - msp) / msp * 100:.0f}% from the structure close {msp:.2f} (split / bad data)"
    if _pos(summary.get("ms_atr")) is None:
        return "no ATR in the structure summary"
    return None


def live_metrics(summary: Dict[str, Any], live_price) -> Dict[str, Optional[float]]:
    """chase / stretch / room (ATR) and R:R re-expressed at the live price."""
    px, msp, atr = _pos(live_price), _pos(summary.get("ms_price")), _pos(summary.get("ms_atr"))
    out: Dict[str, Optional[float]] = {"chase_atr": None, "stretch_atr": None, "room_atr": None, "rr": None}
    if px is None or msp is None or atr is None:
        return out
    d = (px - msp) / atr
    c, s, r = _f(summary.get("ms_chase_atr")), _f(summary.get("ms_stretch_atr")), _f(summary.get("ms_room_atr"))
    out["chase_atr"] = round(max(0.0, c + d), 3) if c is not None else None
    out["stretch_atr"] = round(s + d, 3) if s is not None else None
    out["room_atr"] = round(r - d, 3) if r is not None else None
    stop, target = _pos(summary.get("ms_stop")), _pos(summary.get("ms_target"))
    if stop is not None and target is not None and stop < px < target:
        out["rr"] = round((target - px) / (px - stop), 3)
    return out


def breakout_volume_note(summary: Optional[Dict[str, Any]]) -> Optional[str]:
    """D-vol: text when the latest bullish break of structure is fresh, not failed and came on thin volume (ratio < STRUCTURE_BREAKOUT_MIN_VOL_RATIO);
    None when the test is off, there is no fresh bullish breakout, or the summary has no volume ratio (no opinion). Pure, never raises."""
    try:
        if str(getattr(config, "STRUCTURE_BREAKOUT_VOLUME_MODE", "log")) == "off" or not isinstance(summary, dict):
            return None
        lim = float(getattr(config, "STRUCTURE_BREAKOUT_MIN_VOL_RATIO", 0.0) or 0.0)
        ratio, bars = _f(summary.get("ms_bos_vol_ratio")), _f(summary.get("ms_bos_bars"))
        if lim <= 0 or ratio is None or bars is None or summary.get("ms_bos_dir") != "bullish" or summary.get("ms_bos_failed"):
            return None
        if bars > int(getattr(config, "STRUCTURE_BREAKOUT_BARS", 5)) or ratio >= lim:
            return None
        return f"breakout {int(bars)} bar(s) ago came on thin volume ({ratio:.2f}x the 20-day average, wants {lim:g}x)"
    except Exception:  # noqa: BLE001
        return None


def evaluate(summary: Optional[Dict[str, Any]], live_price) -> Dict[str, Any]:
    """Pure verdict. Keys: opinion ('ok'|'no_opinion'), would_reject, reasons[], tag, reason, metrics, no_data. Never raises."""
    base: Dict[str, Any] = {"opinion": "no_opinion", "would_reject": False, "reasons": [], "observations": [], "tag": None, "reason": None, "metrics": {},
                            "no_data": None, "quality": (summary or {}).get("ms_quality") if isinstance(summary, dict) else None,
                            "ver": (summary or {}).get("ms_ver") if isinstance(summary, dict) else None}
    try:
        nd = no_data_reason(summary, live_price)
        if nd:
            base["no_data"] = nd
            return base
        m = live_metrics(summary, live_price)
        base["metrics"] = m
        base["opinion"] = "ok"
        reasons: List[str] = []
        lim = float(config.STRUCTURE_MAX_CHASE_ATR or 0.0)
        if lim > 0 and m["chase_atr"] is not None and m["chase_atr"] > lim:
            reasons.append(f"chase {m['chase_atr']:.1f} ATR above the nearest demand/imbalance zone (max {lim:g})")
        lim = float(config.STRUCTURE_MAX_STRETCH_ATR or 0.0)
        if lim > 0 and m["stretch_atr"] is not None and m["stretch_atr"] > lim:
            reasons.append(f"price stretched {m['stretch_atr']:.1f} ATR above EMA20 (max {lim:g})")
        lim = float(config.STRUCTURE_MIN_ROOM_ATR or 0.0)
        if lim > 0 and m["room_atr"] is not None and m["room_atr"] < lim:
            _room_txt = f"only {max(0.0, m['room_atr']):.1f} ATR of room to the next resistance (min {lim:g})"
            if m["room_atr"] < 0:
                # group366: the clamp above hid that the live price is already BEYOND the cached resistance (a possible breakout, not "no room")
                _room_txt += f" [raw {m['room_atr']:.1f}: live price is {abs(m['room_atr']):.1f} ATR beyond the cached resistance - possible breakout]"
            reasons.append(_room_txt)
        lim = float(config.STRUCTURE_MIN_RR or 0.0)
        flags = summary.get("ms_rr_flags") or []
        if lim > 0 and m["rr"] is not None and m["rr"] < lim:
            reasons.append(f"structure R:R {m['rr']:.2f} below {lim:g}")
        if config.STRUCTURE_REJECT_STOP_WIDE and "stop_too_wide" in flags:
            reasons.append("structural stop is too wide")
        if config.STRUCTURE_BEARISH_BLOCK:
            if summary.get("ms_trend") == "bearish":
                reasons.append("daily structure is bearish")
            else:
                bars = _f(summary.get("ms_bos_bars"))
                if (summary.get("ms_bos_dir") == "bearish" and not summary.get("ms_bos_failed") and bars is not None
                        and bars <= int(config.STRUCTURE_BEARISH_BOS_BARS)):
                    reasons.append(f"fresh bearish break of structure {int(bars)} bar(s) ago")
        vol_txt = breakout_volume_note(summary)
        if vol_txt:
            if str(getattr(config, "STRUCTURE_BREAKOUT_VOLUME_MODE", "log")) == "enforce":
                reasons.append(vol_txt)
            else:
                base["observations"] = [vol_txt]          # log: recorded on the decision, never a reason, never a WAIT
        base["reasons"] = reasons
        if reasons:
            base["would_reject"] = True
            base["tag"] = TAG
            base["reason"] = "STRUCTURE: good stock, bad entry - " + "; ".join(reasons)
        return base
    except Exception as e:  # noqa: BLE001
        logger.debug("structure gate evaluate failed (no opinion): %s", e)
        base["no_data"] = "evaluation error"
        base["opinion"] = "no_opinion"
        base["would_reject"] = False
        return base


def structural_levels(summary: Optional[Dict[str, Any]], entry_price, atr_stop, atr_target) -> Dict[str, Any]:
    """The structural stop / target next to the ATR ones, with the sanity verdict. `use` is True only when STRUCTURE_STOP_MODE == 'on'
    AND the structural stop is below entry and its risk is within [MIN, MAX] x the ATR-stop risk. Never raises."""
    out: Dict[str, Any] = {"mode": config.STRUCTURE_STOP_MODE, "stop": None, "target": None, "sane": False, "use": False, "why": None}
    try:
        if config.STRUCTURE_STOP_MODE == "off" or not isinstance(summary, dict):
            return out
        e, a_stop = _pos(entry_price), _pos(atr_stop)
        s, t = _pos(summary.get("ms_stop")), _pos(summary.get("ms_target"))
        out["stop"], out["target"] = s, t
        if e is None or a_stop is None or s is None:
            out["why"] = "no structural stop"
            return out
        if "invalid" in (summary.get("ms_rr_flags") or []):
            out["why"] = "structure R:R flagged invalid"
            return out
        atr_risk, st_risk = e - a_stop, e - s
        if atr_risk <= 0 or st_risk <= 0:
            out["why"] = "stop not below entry"
            return out
        mult = st_risk / atr_risk
        out["risk_mult"] = round(mult, 3)
        if mult > float(config.STRUCTURE_STOP_MAX_RISK_MULT):
            out["why"] = f"structural stop {mult:.2f}x ATR risk (too wide)"
        elif mult < float(config.STRUCTURE_STOP_MIN_RISK_MULT):
            out["why"] = f"structural stop {mult:.2f}x ATR risk (hair-thin)"
        else:
            out["sane"] = True
            out["use"] = config.STRUCTURE_STOP_MODE == "on"
        return out
    except Exception as ex:  # noqa: BLE001
        logger.debug("structural_levels failed: %s", ex)
        return out


def snapshot(summary: Optional[Dict[str, Any]], verdict: Dict[str, Any], gate_mode: str, enforced: bool,
             levels: Optional[Dict[str, Any]] = None, source: Optional[str] = None) -> str:
    """Compact JSON stored on the decision (trade_decisions.structure_json): what was seen and what the gate said."""
    s = summary if isinstance(summary, dict) else {}
    keep = ("ms_quality", "ms_ver", "ms_hash", "ms_asof", "ms_trend", "ms_bos_dir", "ms_bos_bars", "ms_bos_failed", "ms_bos_vol_ratio", "ms_choch_dir",
            "ms_choch_bars", "ms_fvg_side", "ms_fvg_status", "ms_ob_side", "ms_ob_status", "ms_in_golden", "ms_loc_score", "ms_rr",
            "ms_stop", "ms_target", "ms_chase_atr", "ms_stretch_atr", "ms_room_atr", "ms_atr", "ms_tqs", "ms_groups", "ms_rr_flags",
            "ms_price")
    doc = {"v": 1, "gate_mode": gate_mode, "enforced": bool(enforced), "source": source,
           "opinion": verdict.get("opinion"), "would_reject": bool(verdict.get("would_reject")), "reasons": verdict.get("reasons") or [], "observations": verdict.get("observations") or [],
           "no_data": verdict.get("no_data"), "live": verdict.get("metrics") or {}, "levels": levels or None,
           "summary": {k: s.get(k) for k in keep if s.get(k) is not None}}
    try:
        return json.dumps(doc, separators=(",", ":"), default=str)[:6000]
    except Exception:  # noqa: BLE001
        return "{}"


# ── group 355: observability (process-local counters + a short ring of recent verdicts; read by GET /structure/status/{mode}) ───────────
_REASON_KEYS = (("chase", "chase"), ("stretched", "stretch"), ("of room", "room"), ("r:r", "rr"), ("stop is too wide", "stop_wide"),
                ("bearish break", "bos"), ("is bearish", "bearish"), ("thin volume", "breakout_volume"))
_stats: Dict[str, Any] = {}
_recent: "deque[Dict[str, Any]]" = deque(maxlen=100)


def reset_stats() -> None:
    _stats.clear()
    _stats.update(evaluated=0, ok=0, no_opinion=0, would_reject=0, enforced=0, nodata_blocked=0, fetch_cache_hit=0, fetch_ok=0,
                  fetch_fail=0, fetch_disabled=0, errors=0, room_past_resistance=0, thin_volume_breakouts=0, by_reason={}, by_nodata={}, since=time.time())
    _recent.clear()


reset_stats()


def reason_key(text: Any) -> str:
    """Short bucket for one gate reason sentence (chase / stretch / room / rr / stop_wide / bos / bearish / other)."""
    t = str(text or "").lower()
    for needle, key in _REASON_KEYS:
        if needle in t:
            return key
    return "other"


def nodata_key(text: Any) -> str:
    """Short bucket for a no-data explanation (no_summary / quality / stale / price_dev / no_price / no_atr / error / other)."""
    t = str(text or "").lower()
    if not t or t == "none":
        return "other"
    for needle, key in (("no structure summary", "no_summary"), ("quality", "quality"), ("d old", "stale"), ("split", "price_dev"),
                        ("no price", "no_price"), ("atr", "no_atr"), ("error", "error")):
        if needle in t:
            return key
    return "other"


def record(symbol: str, trade_mode: str, verdict: Dict[str, Any], gate_mode: str, enforced: bool, price: Any = None,
           source: Optional[str] = None) -> None:
    """Count one evaluated candidate and keep it in the recent ring. Never raises."""
    try:
        _stats["evaluated"] += 1
        op = verdict.get("opinion")
        if op == "ok":
            _stats["ok"] += 1
        else:
            _stats["no_opinion"] += 1
            k = nodata_key(verdict.get("no_data"))
            _stats["by_nodata"][k] = _stats["by_nodata"].get(k, 0) + 1
        if verdict.get("observations") or any("thin volume" in str(r) for r in (verdict.get("reasons") or [])):
            _stats["thin_volume_breakouts"] = _stats.get("thin_volume_breakouts", 0) + 1       # D-vol: counted whether log or enforce
        if verdict.get("would_reject"):
            _stats["would_reject"] += 1
            for r in verdict.get("reasons") or []:
                k = reason_key(r)
                _stats["by_reason"][k] = _stats["by_reason"].get(k, 0) + 1
                if k == "room" and "[raw -" in str(r):
                    _stats["room_past_resistance"] = _stats.get("room_past_resistance", 0) + 1     # group366: breakout-like room rejections
        if enforced:
            _stats["enforced"] += 1
            if not verdict.get("would_reject"):
                _stats["nodata_blocked"] += 1
        _recent.append({"ts": time.time(), "symbol": str(symbol or "").upper(), "mode": str(trade_mode or "").upper(), "gate_mode": gate_mode,
                        "opinion": op, "would_reject": bool(verdict.get("would_reject")), "enforced": bool(enforced),
                        "reasons": list(verdict.get("reasons") or []), "no_data": verdict.get("no_data"), "price": _pos(price),
                        "source": source})
    except Exception:  # noqa: BLE001 - counters must never break an entry
        _stats["errors"] = _stats.get("errors", 0) + 1


def stats() -> Dict[str, Any]:
    """Counters + config + the last verdicts (newest first). Never raises."""
    try:
        ev = int(_stats.get("evaluated") or 0)
        cfg = {k: getattr(config, k, None) for k in ("STRUCTURE_GATE_MODE", "STRUCTURE_GATE_DEMO_MODE", "STRUCTURE_STOP_MODE", "STRUCTURE_REQUIRE_DATA",
               "STRUCTURE_MAX_CHASE_ATR", "STRUCTURE_MAX_STRETCH_ATR", "STRUCTURE_MIN_ROOM_ATR", "STRUCTURE_MIN_RR", "STRUCTURE_REJECT_STOP_WIDE",
               "STRUCTURE_BEARISH_BLOCK", "STRUCTURE_BEARISH_BOS_BARS", "STRUCTURE_BREAKOUT_VOLUME_MODE", "STRUCTURE_BREAKOUT_MIN_VOL_RATIO", "STRUCTURE_MAX_AGE_DAYS", "STRUCTURE_MAX_PRICE_DEV_PCT",
               "STRUCTURE_FETCH_ENABLED")}
        return {**{k: v for k, v in _stats.items() if k != "since"}, "since_epoch": _stats.get("since"),
                "coverage_pct": round(100.0 * _stats.get("ok", 0) / ev, 1) if ev else None, "config": cfg,
                "recent": list(reversed(list(_recent)))[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def tally_decisions(rows: Iterable[Dict[str, Any]], prices: Optional[Dict[str, float]] = None, max_rejects: int = 15) -> Dict[str, Any]:
    """Aggregate stored entry decisions. Each row: {symbol, created_at (iso str), price (proposed entry), structure_json (str|dict)}, newest first.
    Pure. `since_pct` = live price vs the price at the decision: positive on a rejected symbol = it went up after the gate said no."""
    prices = prices or {}
    out: Dict[str, Any] = {"decisions": 0, "with_snapshot": 0, "ok": 0, "no_opinion": 0, "would_reject": 0, "enforced": 0,
                           "by_reason": {}, "by_nodata": {}, "rejects": [], "avg_since_pct": None, "rejects_up": 0, "rejects_down": 0,
                           "coverage_pct": None}
    seen: set = set()
    moves: List[float] = []
    for r in rows:
        out["decisions"] += 1
        raw = r.get("structure_json")
        try:
            doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except Exception:  # noqa: BLE001
            doc = None
        if not isinstance(doc, dict):
            continue
        out["with_snapshot"] += 1
        if doc.get("opinion") == "ok":
            out["ok"] += 1
        else:
            out["no_opinion"] += 1
            k = nodata_key(doc.get("no_data"))
            out["by_nodata"][k] = out["by_nodata"].get(k, 0) + 1
        if doc.get("would_reject"):
            out["would_reject"] += 1
            if doc.get("enforced"):
                out["enforced"] += 1
            for reason in doc.get("reasons") or []:
                k = reason_key(reason)
                out["by_reason"][k] = out["by_reason"].get(k, 0) + 1
            sym = str(r.get("symbol") or "").upper()
            if sym and sym not in seen:  # newest first: the first hit per symbol is its latest verdict
                seen.add(sym)
                px0, px1 = _pos(r.get("price")), _pos(prices.get(sym))
                since = round((px1 - px0) / px0 * 100.0, 2) if px0 and px1 else None
                if since is not None:
                    moves.append(since)
                    if since > 0:
                        out["rejects_up"] += 1
                    elif since < 0:
                        out["rejects_down"] += 1
                if len(out["rejects"]) < max_rejects:
                    out["rejects"].append({"symbol": sym, "at": r.get("created_at"), "price": px0, "now": px1, "since_pct": since,
                                           "enforced": bool(doc.get("enforced")), "reasons": list(doc.get("reasons") or [])})
    if out["with_snapshot"]:
        out["coverage_pct"] = round(100.0 * out["ok"] / out["with_snapshot"], 1)
    if moves:
        out["avg_since_pct"] = round(sum(moves) / len(moves), 2)
    return out


# ── live summary fetch (api-gateway /stock/{symbol}/structure), cached, fail-open ─────────────────────────────────────────────────
_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_inflight: Dict[str, Any] = {}      # symbol -> the running background fetch task (group365 warm)


def reset() -> None:
    _cache.clear()
    _inflight.clear()
    reset_stats()


def peek_summary(symbol: str) -> Optional[Dict[str, Any]]:
    """group365: the CACHED summary only (fresh entry, not a cached failure). Never does I/O - safe inside the exit loop."""
    try:
        hit = _cache.get(str(symbol or "").strip().upper())
        if hit is not None and hit[0] > time.monotonic() and isinstance(hit[1], dict):
            return hit[1]
    except Exception:  # noqa: BLE001
        pass
    return None


def warm(symbol: str) -> bool:
    """group365: start ONE background fetch for `symbol` so the next exit pass finds it cached. True only when a fetch was started;
    False when fetching is off, one is already running, or there is no running event loop. Never raises, never waits."""
    try:
        if not config.STRUCTURE_FETCH_ENABLED:
            return False
        sym = str(symbol or "").strip().upper()
        if not sym or sym in _inflight:
            return False
        import asyncio
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False

        async def _run():
            try:
                await fetch_summary(sym)
            except Exception:  # noqa: BLE001
                pass
            finally:
                _inflight.pop(sym, None)

        _inflight[sym] = loop.create_task(_run())
        return True
    except Exception:  # noqa: BLE001
        return False


async def fetch_summary(symbol: str) -> Optional[Dict[str, Any]]:
    """flat ms_* summary from the gateway; None on any failure (cached STRUCTURE_FAIL_TTL_S so a dead service is not hammered)."""
    if not config.STRUCTURE_FETCH_ENABLED:
        _stats["fetch_disabled"] += 1
        return None
    sym = str(symbol or "").strip().upper()
    if not sym:
        return None
    now = time.monotonic()
    hit = _cache.get(sym)
    if hit is not None and hit[0] > now:
        _stats["fetch_cache_hit"] += 1
        return hit[1]
    summary: Optional[Dict[str, Any]] = None
    ttl = float(config.STRUCTURE_FAIL_TTL_S)
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{config.API_GATEWAY_URL}/stock/{sym}/structure", timeout=float(config.STRUCTURE_FETCH_TIMEOUT_S))
        if r.status_code == 200:
            body = r.json()
            s = body.get("structure_summary") if isinstance(body, dict) else None
            if isinstance(s, dict) and s.get("ms_quality"):
                summary, ttl = s, float(config.STRUCTURE_CACHE_TTL_S)
    except Exception as e:  # noqa: BLE001
        logger.debug("structure fetch %s failed (no opinion): %s", sym, e)
    _stats["fetch_ok" if summary is not None else "fetch_fail"] += 1
    _cache[sym] = (now + ttl, summary)
    if len(_cache) > 2000:
        for k in [k for k, v in _cache.items() if v[0] <= now][:1000]:
            _cache.pop(k, None)
    return summary


async def get_summary(symbol: str, raw_payload: Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """(summary, source): the candidate payload's own summary first, else the gateway. Never raises."""
    try:
        s = extract_summary(raw_payload)
        if s is not None:
            return s, "payload"
        s = await fetch_summary(symbol)
        return (s, "gateway") if s is not None else (None, None)
    except Exception:  # noqa: BLE001
        return None, None
