"""feed/universe_filter.py - round 660 (T4): subscribe only to names that can pass the liquidity floor.

20 Nov: position-stocks subscribed 2,667 NSE-EQ tokens over 3 websockets, the tick buffer ran over its 1.5M budget (idle names thinned to one tick per
5 s) and the service was CPU-throttled in 10-21% of periods, yet the entry liquidity gate (orders/liquidity_gate.py) already refuses any name whose normal
daily turnover is under ENTRY_MIN_AVG_TURNOVER_CR (3 crore) or whose day turnover is under ENTRY_MIN_DAY_TURNOVER_CR (1 crore). Thin names can never be entered.

How it works (no network, no new dependency):
  record  once the session is at or past 15:20 IST (cumulative day volume is then final to within minutes) the tick path calls maybe_record(): one
          turnover figure per symbol (cumulative volume x last price, in crore) is written for that IST date to UNIVERSE_FILE (default
          <BAR_RECORD_DIR>/_universe_turnover.json, the persistent bar-records volume), the last 12 sessions are kept. A session that never reached
          15:20 (a half day, a restart late in the day with no feed) writes nothing, so it can never look like a thin day.
  trim    trim(symbol_map) is applied where the ETF filter already is (ws_client WS loop and md_poller): a symbol is dropped only when it has at least
          UNIVERSE_MIN_DAYS recorded sessions and its BEST recorded session is under UNIVERSE_MIN_TURNOVER_CR (default 1.0). A symbol with no history, a
          symbol kept by the caller (read recently / open position) and everything when the file is missing, stale (newest session older than 10 days),
          unreadable or would leave fewer than UNIVERSE_MIN_KEEP names (default 400) stays subscribed.
First effect: the first session after the deploy records, the next session's subscription is trimmed. UNIVERSE_TRIM_ENABLED=0 = old behaviour.
Everything here fails open and never raises into the feed."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Optional, Tuple

logger = logging.getLogger("position-stocks-universe")

_IST = timezone(timedelta(hours=5, minutes=30))
KEEP_SESSIONS = 12
STALE_DAYS = 10
RECORD_FROM = (15, 20)
RECORD_EVERY_S = 300.0

_lock = threading.Lock()
_state = {"last_record": 0.0, "last_recorded_day": None, "kept": None, "dropped": None, "reason": "not applied yet", "file_days": 0}
_cache: dict = {"mtime": None, "path": None, "data": None}


def _cfg(name: str, default):
    try:
        import config
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_cfg("UNIVERSE_TRIM_ENABLED", True))


def path() -> str:
    p = str(_cfg("UNIVERSE_FILE", "") or "").strip()
    return p or os.path.join(str(_cfg("BAR_RECORD_DIR", "/data/bars")), "_universe_turnover.json")


def _load(p: str) -> Optional[dict]:
    """{"sessions": {date: {symbol: crore}}} or None. Cached by mtime. Never raises."""
    try:
        m = os.path.getmtime(p)
        if _cache["path"] == p and _cache["mtime"] == m:
            return _cache["data"]
        with open(p, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        sess = doc.get("sessions") if isinstance(doc, dict) else None
        data = {"sessions": sess} if isinstance(sess, dict) else None
        _cache.update(path=p, mtime=m, data=data)
        return data
    except Exception:  # noqa: BLE001
        return None


def _write(p: str, doc: dict) -> bool:
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        os.replace(tmp, p)
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("universe filter: could not write %s: %s", p, e)
        return False


def _ist(now: Optional[float]) -> datetime:
    return datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc).astimezone(_IST)


def maybe_record(volumes: Dict[str, float], last_price: Callable[[str], Optional[float]], now: Optional[float] = None) -> bool:
    """From 15:20 IST on a weekday, at most every RECORD_EVERY_S: write today's turnover (crore) per symbol. True when a file was written."""
    try:
        if not enabled():
            return False
        t = time.monotonic() if now is None else now
        dt = _ist(now)
        if dt.weekday() >= 5 or (dt.hour, dt.minute) < RECORD_FROM or dt.hour >= 16:
            return False
        with _lock:
            if _state["last_record"] and (t - _state["last_record"]) < RECORD_EVERY_S and now is None:
                return False
            _state["last_record"] = t
        out: Dict[str, float] = {}
        for sym, vol in list(volumes.items()):
            try:
                px = last_price(sym)
                if vol and px and float(vol) > 0 and float(px) > 0:
                    out[sym] = round(float(vol) * float(px) / 1e7, 3)
            except Exception:  # noqa: BLE001
                continue
        if len(out) < 100:                                   # a feed that barely ticked says nothing about the market
            return False
        p = path()
        cur = _load(p) or {"sessions": {}}
        sessions = dict(cur.get("sessions") or {})
        day = dt.date().isoformat()
        sessions[day] = out
        for old in sorted(sessions)[:-KEEP_SESSIONS]:
            sessions.pop(old, None)
        ok = _write(p, {"v": 1, "sessions": sessions})
        if ok:
            _cache.update(path=None, mtime=None, data=None)
            with _lock:
                _state["last_recorded_day"] = day
            logger.info("universe filter: recorded %d symbols' turnover for %s (%d sessions kept)", len(out), day, len(sessions))
        return ok
    except Exception:  # noqa: BLE001
        return False


def trim(symbol_map: dict, keep: Optional[Iterable[str]] = None, today: Optional[str] = None) -> Tuple[dict, int]:
    """(map without names that cannot pass the liquidity floor, number dropped). The input is not modified. Fails open (returns the map, 0)."""
    try:
        if not enabled() or not symbol_map:
            return symbol_map, 0
        data = _load(path())
        if not data:
            _note(len(symbol_map), 0, "no turnover file yet - nothing trimmed")
            return symbol_map, 0
        sessions = data["sessions"]
        days = sorted(sessions)
        if not days:
            _note(len(symbol_map), 0, "turnover file is empty - nothing trimmed")
            return symbol_map, 0
        now_d = datetime.fromisoformat(today) if today else _ist(None).replace(tzinfo=None)
        newest = datetime.fromisoformat(days[-1])
        if (now_d - newest) > timedelta(days=STALE_DAYS):
            _note(len(symbol_map), 0, f"turnover file is stale (newest session {days[-1]}) - nothing trimmed")
            return symbol_map, 0
        floor = float(_cfg("UNIVERSE_MIN_TURNOVER_CR", 1.0) or 0.0)
        min_days = max(1, int(_cfg("UNIVERSE_MIN_DAYS", 1) or 1))
        min_keep = int(_cfg("UNIVERSE_MIN_KEEP", 400) or 0)
        if floor <= 0 or len(days) < min_days:
            _note(len(symbol_map), 0, "floor off or too few recorded sessions - nothing trimmed")
            return symbol_map, 0
        protect = {str(s).strip().upper() for s in (keep or [])}
        kept, dropped = {}, 0
        for sym, tok in symbol_map.items():
            vals = [sessions[d][sym] for d in days if isinstance(sessions[d], dict) and sym in sessions[d]]
            if sym in protect or sym.upper() in protect or len(vals) < min_days or max(vals) >= floor:
                kept[sym] = tok
            else:
                dropped += 1
        if len(kept) < min_keep:
            _note(len(symbol_map), 0, f"trim would leave only {len(kept)} names (< {min_keep}) - nothing trimmed")
            return symbol_map, 0
        _note(len(kept), dropped, f"dropped {dropped} names whose best recorded session ({len(days)} kept) was under {floor} crore")
        return kept, dropped
    except Exception:  # noqa: BLE001
        return symbol_map, 0


def _note(kept: int, dropped: int, reason: str) -> None:
    with _lock:
        changed = (_state["kept"], _state["dropped"], _state["reason"]) != (kept, dropped, reason)
        _state.update(kept=kept, dropped=dropped, reason=reason)
    if changed:
        logger.info("universe filter: %d names subscribed, %d trimmed (%s)", kept, dropped, reason)


def status() -> dict:
    with _lock:
        s = dict(_state)
    s.update(enabled=enabled(), file=path(), min_turnover_cr=_cfg("UNIVERSE_MIN_TURNOVER_CR", 1.0))
    return s


def reset_for_tests() -> None:
    with _lock:
        _state.update(last_record=0.0, last_recorded_day=None, kept=None, dropped=None, reason="not applied yet", file_days=0)
    _cache.update(path=None, mtime=None, data=None)
