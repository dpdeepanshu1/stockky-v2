"""screening/scan_hits.py - E232: a record of EVERY symbol the scan listed, not just the few that reached the quality gate.

WHY: scan() ranks every subscribed symbol that clears a window, but only the top SHORTLIST_MAX names per cycle are quality-checked and written to the
candidate log. A name ranked 6th every cycle left no trace, so "was X ever found today?" had no answer (2026-10-27 review of 18 top movers).

WHAT: per symbol and IST day: first / last seen, scans listed in, best and last rank, best move and its window, how many cycles it was quality-checked.
Kept in memory, written to SCAN_HITS_DIR/scan_hits_<day>.json at most every SCAN_HITS_DUMP_S (atomic), reloaded after a restart.
RECORD ONLY: nothing in the entry path reads this. Every public function swallows its own errors (counted in stats()); an unwritable directory turns the
file write off by itself and stats() says why - the in-memory record keeps working."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional

import config

logger = logging.getLogger("position-stocks-scan-hits")

_IST = timezone(timedelta(hours=5, minutes=30))
_lock = threading.Lock()
_state: Dict[str, object] = {"day": None, "hits": {}, "last_dump": 0.0, "dump_off": None, "errors": 0, "last_error": None, "scans": 0, "loaded": False}


def _day(now: Optional[float] = None) -> str:
    return datetime.fromtimestamp(now if now is not None else time.time(), _IST).strftime("%Y-%m-%d")


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, _IST).strftime("%H:%M:%S")


def _err(where: str, e: Exception) -> None:
    _state["errors"] = int(_state["errors"]) + 1
    _state["last_error"] = f"{where}: {type(e).__name__}: {e}"
    logger.debug("scan_hits %s failed: %s", where, e)


def _enabled() -> bool:
    return bool(getattr(config, "SCAN_HITS_ENABLED", True))


def _path(day: str) -> str:
    return os.path.join(str(getattr(config, "SCAN_HITS_DIR", "/data/bars/_scan_hits")), f"scan_hits_{day}.json")


def _roll(now: float) -> None:
    """Start a new day's record (and reload today's file once after a restart). Caller holds the lock."""
    day = _day(now)
    if _state["day"] != day:
        _state["day"] = day
        _state["hits"] = {}
        _state["loaded"] = False
        _state["scans"] = 0
    if not _state["loaded"]:
        _state["loaded"] = True
        try:
            p = _path(day)
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f)
                if d.get("day") == day and isinstance(d.get("hits"), dict):
                    _state["hits"] = d["hits"]
                    _state["scans"] = int(d.get("scans") or 0)
        except Exception as e:  # noqa: BLE001
            _err("load", e)


def _dump(now: float, force: bool = False) -> None:
    """Atomic write, at most every SCAN_HITS_DUMP_S. Caller holds the lock."""
    if _state["dump_off"] and not force:
        return
    if not force and now - float(_state["last_dump"]) < float(getattr(config, "SCAN_HITS_DUMP_S", 30)):
        return
    _state["last_dump"] = now
    try:
        d = os.path.dirname(_path(str(_state["day"])))
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".scan_hits_", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"day": _state["day"], "scans": _state["scans"], "hits": _state["hits"]}, f, separators=(",", ":"))
        os.replace(tmp, _path(str(_state["day"])))
        _state["dump_off"] = None
        _prune(d, str(_state["day"]))
    except Exception as e:  # noqa: BLE001
        _state["dump_off"] = f"cannot write {getattr(config, 'SCAN_HITS_DIR', '?')}: {type(e).__name__}: {e}"
        _err("dump", e)


def _prune(root: str, today: str) -> None:
    try:
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=int(getattr(config, "SCAN_HITS_KEEP_DAYS", 30)))).strftime("%Y-%m-%d")
        for name in os.listdir(root):
            if name.startswith("scan_hits_") and name.endswith(".json") and name[10:20] < cutoff:
                try:
                    os.unlink(os.path.join(root, name))
                except OSError:
                    pass
    except Exception as e:  # noqa: BLE001
        _err("prune", e)


def record_scan(candidates: Iterable, now: Optional[float] = None, side: str = "long") -> int:
    """Record one scan's ranked candidate list (best first). Returns the number of distinct symbols recorded. Never raises."""
    if not _enabled():
        return 0
    try:
        now = time.time() if now is None else now
        order: List[str] = []
        best: Dict[str, object] = {}
        for c in candidates or []:
            sym = str(getattr(c, "symbol", "") or "")
            if not sym:
                continue
            if sym not in best:
                order.append(sym)
                best[sym] = c
        with _lock:
            _roll(now)
            _state["scans"] = int(_state["scans"]) + 1
            hits = _state["hits"]
            for rank, sym in enumerate(order, start=1):
                c = best[sym]
                key = sym if side == "long" else f"{sym}|short"
                h = hits.get(key)
                pct = float(getattr(c, "pct_change", 0.0) or 0.0)
                if h is None:
                    h = {"symbol": sym, "side": side, "first_seen": _iso(now), "scans": 0, "best_rank": rank, "best_pct": pct, "best_window": getattr(c, "window_label", ""),
                         "best_score": float(getattr(c, "composite_score", 0.0) or 0.0), "checked_cycles": 0, "windows": []}
                    hits[key] = h
                h["scans"] = int(h["scans"]) + 1
                h["last_seen"] = _iso(now)
                h["last_rank"] = rank
                h["last_pct"] = pct
                h["last_ltp"] = float(getattr(c, "current_ltp", 0.0) or 0.0)
                h["listed_of"] = len(order)
                if rank < int(h["best_rank"]):
                    h["best_rank"] = rank
                if abs(pct) > abs(float(h["best_pct"])):
                    h["best_pct"] = pct
                    h["best_window"] = getattr(c, "window_label", "")
                w = getattr(c, "window_label", "")
                if w and w not in h["windows"]:
                    h["windows"].append(w)
            _dump(now)
        return len(order)
    except Exception as e:  # noqa: BLE001
        _err("record_scan", e)
        return 0


def mark_checked(symbols: Iterable[str], now: Optional[float] = None, side: str = "long") -> None:
    """The symbols that went to the quality gate this cycle (the top-N slice). Never raises."""
    if not _enabled():
        return
    try:
        now = time.time() if now is None else now
        with _lock:
            _roll(now)
            for sym in symbols or []:
                h = _state["hits"].get(str(sym) if side == "long" else f"{sym}|short")
                if h is not None:
                    h["checked_cycles"] = int(h["checked_cycles"]) + 1
                    h["last_checked"] = _iso(now)
    except Exception as e:  # noqa: BLE001
        _err("mark_checked", e)


def get(symbol: str, side: str = "long") -> Optional[dict]:
    try:
        with _lock:
            _roll(time.time())
            h = _state["hits"].get(symbol if side == "long" else f"{symbol}|short")
            return dict(h) if h else None
    except Exception as e:  # noqa: BLE001
        _err("get", e)
        return None


def snapshot(limit: int = 500, side: Optional[str] = None, never_checked_only: bool = False) -> dict:
    """Today's record, most-seen first. never_checked_only = listed by the scan but never quality-checked (ranked below the shortlist every time)."""
    try:
        with _lock:
            _roll(time.time())
            rows = list(_state["hits"].values())
            out = {"day": _state["day"], "scans": _state["scans"], "symbols": len(rows)}
        if side:
            rows = [r for r in rows if r.get("side") == side]
        if never_checked_only:
            rows = [r for r in rows if not r.get("checked_cycles")]
        rows.sort(key=lambda r: (-int(r.get("scans", 0)), r.get("symbol", "")))
        out["rows"] = rows[: max(1, min(int(limit), 5000))]
        out["shown"] = len(out["rows"])
        return out
    except Exception as e:  # noqa: BLE001
        _err("snapshot", e)
        return {"day": None, "scans": 0, "symbols": 0, "rows": [], "shown": 0}


def stats() -> dict:
    with _lock:
        return {"enabled": _enabled(), "day": _state["day"], "symbols": len(_state["hits"]), "scans": _state["scans"], "errors": _state["errors"],
                "last_error": _state["last_error"], "file_write_off": _state["dump_off"], "dir": getattr(config, "SCAN_HITS_DIR", None)}


def flush() -> None:
    """Write now (used at end of day / shutdown). Never raises."""
    if not _enabled():
        return
    try:
        with _lock:
            if _state["day"]:
                _dump(time.time(), force=True)
    except Exception as e:  # noqa: BLE001
        _err("flush", e)


def reset() -> None:
    with _lock:
        _state.update({"day": None, "hits": {}, "last_dump": 0.0, "dump_off": None, "errors": 0, "last_error": None, "scans": 0, "loaded": False})
