"""orders/structure_gate.py - daily market-structure gate on the automatic scalp entry (group 354, 2026-10-14).

QUESTION IT ANSWERS: "good scalp, bad daily location?" The candidate already passed the intraday screens; this looks at where the price sits
in the DAILY structure (technical service `market_structure` summary, flat `ms_*` keys, fetched from api-gateway
GET /stock/{symbol}/structure): a bearish daily trend, a fresh bearish break of structure, almost no room under the next daily resistance, an
extreme chase above the nearest demand/imbalance zone or an extreme stretch above the daily EMA20.

LIVE: config.STRUCTURE_GATE_MODE = enforce (default, always live) | off (kill switch). It can only ever REFUSE an entry (never create one).
NO DATA = NO OPINION: a missing / unavailable / insufficient / stale summary, an old last bar, or a live price far from the structure close
(split / bad data) never blocks and is never bullish - unless STRUCTURE_REQUIRE_DATA=true.
The scalper buys strength on purpose, so the default limits are looser than real-trade's swing entry gate; every limit is an env var and 0
switches that test off. Sync (attempt_entry runs in a worker thread), pure where it can be, never raises.
"""
from __future__ import annotations

import json
import logging
import math
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import config

logger = logging.getLogger("position-stocks-structure-gate")

PREFIX = "STRUCTURE_"
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


def _cfg(name: str, dflt: float) -> float:
    try:
        return float(getattr(config, name, dflt))
    except (TypeError, ValueError):
        return float(dflt)


def mode() -> str:
    m = str(getattr(config, "STRUCTURE_GATE_MODE", "enforce") or "enforce").lower()
    return "off" if m == "off" else "enforce"


def _age_days(asof) -> Optional[float]:
    try:
        if asof is None:
            return None
        if isinstance(asof, (int, float)):
            ts = float(asof)
            ts = ts / 1000.0 if ts > 1e11 else ts
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(asof).strip().replace("Z", "+00:00"))
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
    max_age = _cfg("STRUCTURE_MAX_AGE_DAYS", 7.0)
    if max_age > 0 and age is not None and age > max_age:
        return f"structure last bar is {age:.0f}d old"
    px, msp = _pos(live_price), _pos(summary.get("ms_price"))
    if px is None or msp is None:
        return "no price to compare"
    dev = _cfg("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0)
    if dev > 0 and abs(px - msp) / msp * 100.0 > dev:
        return f"live price {px:.2f} is {abs(px - msp) / msp * 100:.0f}% from the structure close {msp:.2f} (split / bad data)"
    if _pos(summary.get("ms_atr")) is None:
        return "no ATR in the structure summary"
    return None


def live_metrics(summary: Dict[str, Any], live_price) -> Dict[str, Optional[float]]:
    """chase / stretch / room (daily ATR) re-expressed at the live price (the summary was computed at the structure close)."""
    px, msp, atr = _pos(live_price), _pos(summary.get("ms_price")), _pos(summary.get("ms_atr"))
    out: Dict[str, Optional[float]] = {"chase_atr": None, "stretch_atr": None, "room_atr": None}
    if px is None or msp is None or atr is None:
        return out
    d = (px - msp) / atr
    c, s, r = _f(summary.get("ms_chase_atr")), _f(summary.get("ms_stretch_atr")), _f(summary.get("ms_room_atr"))
    out["chase_atr"] = round(max(0.0, c + d), 3) if c is not None else None
    out["stretch_atr"] = round(s + d, 3) if s is not None else None
    out["room_atr"] = round(r - d, 3) if r is not None else None
    return out


def evaluate(summary: Optional[Dict[str, Any]], live_price) -> Dict[str, Any]:
    """Pure verdict: opinion ('ok'|'no_opinion'), would_reject, reasons[], reason, metrics, no_data. Never raises."""
    base: Dict[str, Any] = {"opinion": "no_opinion", "would_reject": False, "reasons": [], "reason": None, "metrics": {}, "no_data": None}
    try:
        nd = no_data_reason(summary, live_price)
        if nd:
            base["no_data"] = nd
            return base
        m = live_metrics(summary, live_price)
        base["metrics"] = m
        base["opinion"] = "ok"
        reasons: List[str] = []
        lim = _cfg("STRUCTURE_MAX_CHASE_ATR", 4.0)
        if lim > 0 and m["chase_atr"] is not None and m["chase_atr"] > lim:
            reasons.append(f"chase {m['chase_atr']:.1f} daily ATR above the nearest demand zone (max {lim:g})")
        lim = _cfg("STRUCTURE_MAX_STRETCH_ATR", 5.0)
        if lim > 0 and m["stretch_atr"] is not None and m["stretch_atr"] > lim:
            reasons.append(f"stretched {m['stretch_atr']:.1f} daily ATR above EMA20 (max {lim:g})")
        lim = _cfg("STRUCTURE_MIN_ROOM_ATR", 0.3)
        if lim > 0 and m["room_atr"] is not None and m["room_atr"] < lim:
            _room_txt = f"only {max(0.0, m['room_atr']):.1f} daily ATR of room to the next resistance (min {lim:g})"
            if m["room_atr"] < 0:
                # group366: the clamp above hid that the live price is already BEYOND the cached resistance (a possible breakout, not "no room")
                _room_txt += f" [raw {m['room_atr']:.1f}: live price is {abs(m['room_atr']):.1f} ATR beyond the cached resistance - possible breakout]"
            reasons.append(_room_txt)
        if getattr(config, "STRUCTURE_BEARISH_BLOCK", True):
            if summary.get("ms_trend") == "bearish":
                reasons.append("daily structure is bearish")
            else:
                bars = _f(summary.get("ms_bos_bars"))
                if (summary.get("ms_bos_dir") == "bearish" and not summary.get("ms_bos_failed") and bars is not None
                        and bars <= int(_cfg("STRUCTURE_BEARISH_BOS_BARS", 5))):
                    reasons.append(f"fresh bearish break of structure {int(bars)} bar(s) ago")
        base["reasons"] = reasons
        if reasons:
            base["would_reject"] = True
            base["reason"] = "; ".join(reasons)
        return base
    except Exception as e:  # noqa: BLE001
        logger.debug("structure gate evaluate failed (no opinion): %s", e)
        base.update(no_data="evaluation error", opinion="no_opinion", would_reject=False)
        return base


def extract_summary(payload: Any) -> Optional[Dict[str, Any]]:
    """The flat ms_* summary from a gateway reply / candidate row: `structure_summary`, or ms_* keys on the dict itself."""
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


# ── group 355: observability (process-local counters + a ring of recent verdicts; read by GET /structure/status) ───────────────────
_REASON_KEYS = (("chase", "chase"), ("stretched", "stretch"), ("of room", "room"), ("bearish break", "bos"), ("is bearish", "bearish"))
_stats: Dict[str, Any] = {}
_recent: "deque[Dict[str, Any]]" = deque(maxlen=100)


def reset_stats() -> None:
    _stats.clear()
    _stats.update(evaluated=0, ok=0, no_opinion=0, would_reject=0, enforced=0, nodata_blocked=0, fetch_cache_hit=0, fetch_ok=0,
                  fetch_fail=0, errors=0, room_past_resistance=0, by_reason={}, by_nodata={}, since=time.time())
    _recent.clear()


reset_stats()


def reason_key(text: Any) -> str:
    """Short bucket for one gate reason sentence (chase / stretch / room / bos / bearish / other)."""
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


def record(symbol: str, verdict: Dict[str, Any], gate_mode: str, enforced: bool, price: Any = None) -> None:
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
        _recent.append({"ts": time.time(), "symbol": str(symbol or "").upper(), "gate_mode": gate_mode, "opinion": op,
                        "would_reject": bool(verdict.get("would_reject")), "enforced": bool(enforced),
                        "reasons": list(verdict.get("reasons") or []), "no_data": verdict.get("no_data"), "price": _pos(price)})
    except Exception:  # noqa: BLE001 - counters must never break an entry
        _stats["errors"] = _stats.get("errors", 0) + 1


def stats() -> Dict[str, Any]:
    """Counters + config + the last verdicts (newest first). Never raises."""
    try:
        ev = int(_stats.get("evaluated") or 0)
        cfg = {k: getattr(config, k, None) for k in ("STRUCTURE_GATE_MODE", "STRUCTURE_REQUIRE_DATA", "STRUCTURE_MAX_CHASE_ATR",
               "STRUCTURE_MAX_STRETCH_ATR", "STRUCTURE_MIN_ROOM_ATR", "STRUCTURE_BEARISH_BLOCK", "STRUCTURE_BEARISH_BOS_BARS",
               "STRUCTURE_MAX_AGE_DAYS", "STRUCTURE_MAX_PRICE_DEV_PCT")}
        return {**{k: v for k, v in _stats.items() if k != "since"}, "since_epoch": _stats.get("since"),
                "coverage_pct": round(100.0 * _stats.get("ok", 0) / ev, 1) if ev else None, "config": cfg,
                "recent": list(reversed(list(_recent)))[:30]}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)[:120]}


def with_moves(recent: Iterable[Dict[str, Any]], prices: Dict[str, float]) -> List[Dict[str, Any]]:
    """Add now / since_pct (live price vs the price at the verdict) to ring entries. Pure; entries without both prices get None."""
    out: List[Dict[str, Any]] = []
    for r in recent:
        px0, px1 = _pos(r.get("price")), _pos((prices or {}).get(r.get("symbol")))
        out.append({**r, "now": px1, "since_pct": round((px1 - px0) / px0 * 100.0, 2) if px0 and px1 else None})
    return out


def tally_stored(log_rows: Iterable[Dict[str, Any]], position_rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """What the DB says. log_rows: scalp_candidate_log {symbol, reason, decision, created_at}; the gate refuses with reason
    'STRUCTURE_BAD_LOCATION:<reasons>' or 'STRUCTURE_NODATA:<why>'. position_rows: scalp_positions {symbol, structure_json} - the snapshot
    stored on every entry that got through (no-opinion entries show up here). Pure."""
    out: Dict[str, Any] = {"refused": 0, "refused_bad_location": 0, "refused_nodata": 0, "by_reason": {}, "by_nodata": {}, "refused_symbols": [],
                           "entered_with_snapshot": 0, "entered_ok": 0, "entered_no_opinion": 0, "entered_would_reject": 0,
                           "entered_by_reason": {}, "coverage_pct": None}
    seen: set = set()
    for r in log_rows:
        reason = str(r.get("reason") or "")
        if not reason.startswith(PREFIX):
            continue
        out["refused"] += 1
        body = reason.split(":", 1)[1] if ":" in reason else ""
        if reason.startswith(PREFIX + "NODATA"):
            out["refused_nodata"] += 1
            k = nodata_key(body)
            out["by_nodata"][k] = out["by_nodata"].get(k, 0) + 1
        else:
            out["refused_bad_location"] += 1
            for part in body.split("; "):
                k = reason_key(part)
                out["by_reason"][k] = out["by_reason"].get(k, 0) + 1
        sym = str(r.get("symbol") or "").upper()
        if sym and sym not in seen and len(out["refused_symbols"]) < 20:
            seen.add(sym)
            out["refused_symbols"].append({"symbol": sym, "at": r.get("created_at"), "reason": body})
    for r in position_rows:
        raw = r.get("structure_json")
        try:
            doc = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except Exception:  # noqa: BLE001
            doc = None
        if not isinstance(doc, dict):
            continue
        out["entered_with_snapshot"] += 1
        if doc.get("opinion") == "ok":
            out["entered_ok"] += 1
        else:
            out["entered_no_opinion"] += 1
        if doc.get("would_reject"):
            out["entered_would_reject"] += 1
            for reason in doc.get("reasons") or []:
                k = reason_key(reason)
                out["entered_by_reason"][k] = out["entered_by_reason"].get(k, 0) + 1
    if out["entered_with_snapshot"]:
        out["coverage_pct"] = round(100.0 * out["entered_ok"] / out["entered_with_snapshot"], 1)
    return out


_cache: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}


def reset() -> None:
    _cache.clear()
    reset_stats()


def fetch_summary(symbol: str) -> Optional[Dict[str, Any]]:
    """Flat ms_* summary from api-gateway (cached STRUCTURE_CACHE_TTL_S; a failure is cached STRUCTURE_FAIL_TTL_S). None on any failure."""
    sym = str(symbol or "").strip().upper().replace(".NS", "").replace(".BO", "")
    if not sym:
        return None
    now = time.monotonic()
    hit = _cache.get(sym)
    if hit is not None and hit[0] > now:
        _stats["fetch_cache_hit"] += 1
        return hit[1]
    summary: Optional[Dict[str, Any]] = None
    ttl = _cfg("STRUCTURE_FAIL_TTL_S", 60.0)
    try:
        import httpx
        with httpx.Client(timeout=_cfg("STRUCTURE_FETCH_TIMEOUT_S", 2.0)) as client:
            r = client.get(f"{config._API_GATEWAY_URL}/stock/{sym}/structure")
        if r.status_code == 200:
            summary = extract_summary(r.json())
            if summary is not None:
                ttl = _cfg("STRUCTURE_CACHE_TTL_S", 600.0)
    except Exception as e:  # noqa: BLE001
        logger.debug("structure fetch %s failed (no opinion): %s", sym, e)
    _stats["fetch_ok" if summary is not None else "fetch_fail"] += 1
    _cache[sym] = (now + ttl, summary)
    if len(_cache) > 2000:
        for k in [k for k, v in _cache.items() if v[0] <= now][:1000]:
            _cache.pop(k, None)
    return summary


_KEEP = ("ms_quality", "ms_ver", "ms_asof", "ms_trend", "ms_bos_dir", "ms_bos_bars", "ms_bos_failed", "ms_choch_dir", "ms_fvg_side",
         "ms_fvg_status", "ms_ob_side", "ms_ob_status", "ms_in_golden", "ms_loc_score", "ms_rr", "ms_stop", "ms_target", "ms_chase_atr",
         "ms_stretch_atr", "ms_room_atr", "ms_atr", "ms_tqs", "ms_price")


def snapshot(summary: Optional[Dict[str, Any]], verdict: Dict[str, Any], gate_mode: str, enforced: bool) -> str:
    """Compact JSON stored on the position (scalp_positions.structure_json): what was seen and what the gate said."""
    s = summary if isinstance(summary, dict) else {}
    doc = {"v": 1, "gate_mode": gate_mode, "enforced": bool(enforced), "opinion": verdict.get("opinion"),
           "would_reject": bool(verdict.get("would_reject")), "reasons": verdict.get("reasons") or [], "no_data": verdict.get("no_data"),
           "live": verdict.get("metrics") or {}, "summary": {k: s.get(k) for k in _KEEP if s.get(k) is not None}}
    try:
        return json.dumps(doc, separators=(",", ":"), default=str)[:6000]
    except Exception:  # noqa: BLE001
        return "{}"


def check(symbol: str, ltp: float) -> Tuple[Optional[str], Optional[str]]:
    """(refuse_reason_or_None, snapshot_json_or_None). A reason is returned whenever the gate rejects (mode enforce). Never raises (fails open)."""
    m = mode()
    if m == "off":
        return None, None
    try:
        summary = fetch_summary(symbol)
        v = evaluate(summary, ltp)
        block = None
        if v["would_reject"]:
            block = f"{PREFIX}BAD_LOCATION:{v['reason']}"
        elif v["opinion"] == "no_opinion" and getattr(config, "STRUCTURE_REQUIRE_DATA", False):
            block = f"{PREFIX}NODATA:{v.get('no_data')}"
        enforced = bool(block)
        snap = snapshot(summary, v, m, enforced)
        record(symbol, v, m, enforced, ltp)   # group 355
        return block, snap
    except Exception as e:  # noqa: BLE001 - a gate bug must never stop an entry
        logger.warning("structure gate failed for %s (allowing): %s", symbol, e)
        return None, None
