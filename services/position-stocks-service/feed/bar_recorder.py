"""feed/bar_recorder.py - group441 (WS-15): persist today's 1-minute bars to disk. RECORD ONLY.

WHY: feed/bar_store.py keeps today's minute bars in memory and clears them on the next IST day. Intraday (15m / 5m) structure, the opening gate, the VWAP
gate and depth numbers can only be backtested on history nobody kept, so this keeps it. Nothing in the entry path reads these files.

LAYOUT  <BAR_RECORD_DIR>/<YYYY-MM-DD>/<quote(SYMBOL)>.json
  {"v": 1, "symbol", "date", "tf": 1, "first_ts", "last_ts", "n", "backfilled": bool, "bars": [[t, o, h, l, c, v], ...]}   t = epoch seconds of the minute.
HOW: run_once(now, market_open) is called by screening/board_runner (worker thread). Market open: writes at most every BAR_RECORD_EVERY_S; after 15:30 one
final flush per day (the store clears itself on the next day's first tick). A file is rewritten only when its bar count / last bar changed. Writes are atomic.
After a restart the first write of a symbol MERGES with the file on disk (a minute in memory wins), so a restart never shrinks a recorded day.
RETENTION: day folders older than BAR_RECORD_KEEP_DAYS are deleted, once per day.
SAFE: every public function swallows its own errors (counted in stats()); an unwritable directory switches the recorder off with the reason in stats().
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote

logger = logging.getLogger("position-stocks-bar-recorder")

IST_OFFSET_S = 330 * 60
_DAY_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_lock = threading.Lock()
_written: Dict[str, Tuple[str, int, float, float, float]] = {}
_state: Dict[str, Any] = {"last_run": 0.0, "last_write": 0.0, "final_day": None, "pruned_day": None, "files_written": 0, "bytes_written": 0,
                          "symbols_last": 0, "skipped_unchanged": 0, "merged_existing": 0, "errors": 0, "last_error": None, "disabled_reason": None}


def _cfg(name: str, default: Any) -> Any:
    try:
        import config
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def reset() -> None:
    with _lock:
        _written.clear()
        _state.update(last_run=0.0, last_write=0.0, final_day=None, pruned_day=None, files_written=0, bytes_written=0, symbols_last=0,
                      skipped_unchanged=0, merged_existing=0, errors=0, last_error=None, disabled_reason=None)


def ist_date(ts: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=float(ts) + IST_OFFSET_S)).strftime("%Y-%m-%d")


def day_dir(root: str, date: str) -> str:
    return os.path.join(root, date)


def file_for(root: str, date: str, symbol: str) -> str:
    return os.path.join(day_dir(root, date), quote(symbol, safe="") + ".json")


def symbol_from_file(name: str) -> str:
    return unquote(name[:-5] if name.endswith(".json") else name)


def _err(where: str, e: Exception) -> None:
    _state["errors"] += 1
    _state["last_error"] = f"{where}: {type(e).__name__}: {str(e)[:120]}"
    logger.warning("bar recorder %s failed (%s: %s)", where, type(e).__name__, e)


def _clean(bars: Any) -> List[List[float]]:
    out: Dict[float, List[float]] = {}
    for b in bars or []:
        try:
            t, o, h, l, c, v = (float(x) for x in list(b)[:6])
        except (TypeError, ValueError):
            continue
        if not (t > 0 and o > 0 and h > 0 and l > 0 and c > 0 and h >= l) or v != v or v < 0:
            continue
        out[t] = [t, round(o, 4), round(h, 4), round(l, 4), round(c, 4), round(v, 2)]
    return [out[k] for k in sorted(out)]


def _read(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _atomic_write(path: str, doc: Dict[str, Any]) -> int:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return os.path.getsize(path)


def write_symbol(root: str, date: str, symbol: str, bars: Any, backfilled: bool = False) -> Optional[int]:
    """Write one symbol's day. Merges with the file on disk the first time this process writes the symbol; returns bytes written, or None when nothing
    changed / nothing worth writing. Raises only OSError-like problems (the caller counts them)."""
    mem = _clean(bars)
    if len(mem) < int(_cfg("BAR_RECORD_MIN_BARS", 20)):
        return None
    path = file_for(root, date, symbol)
    prev = _written.get(symbol)
    merged_from_disk = False
    if prev is None or prev[0] != date:
        old = _read(path)
        if old and isinstance(old.get("bars"), list):
            disk = _clean(old["bars"])
            by_t = {b[0]: b for b in disk}
            by_t.update({b[0]: b for b in mem})            # a minute in memory wins
            mem = [by_t[k] for k in sorted(by_t)]
            backfilled = backfilled or bool(old.get("backfilled"))
            merged_from_disk = bool(disk)
    sig = (date, len(mem), mem[-1][0], mem[-1][4], mem[-1][5])
    if prev == sig:
        _state["skipped_unchanged"] += 1
        return None
    doc = {"v": 1, "symbol": symbol, "date": date, "tf": 1, "first_ts": mem[0][0], "last_ts": mem[-1][0], "n": len(mem),
           "backfilled": bool(backfilled), "bars": mem}
    size = _atomic_write(path, doc)
    _written[symbol] = sig
    if merged_from_disk:
        _state["merged_existing"] += 1
    return size


def prune_days(root: str, keep_days: int, today: str) -> int:
    """Delete YYYY-MM-DD folders older than keep_days (relative to `today`). Anything not shaped like a date folder is left alone."""
    removed = 0
    try:
        cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=int(keep_days))).strftime("%Y-%m-%d")
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if _DAY_DIR.match(name) and name < cutoff and os.path.isdir(p):
                for fn in os.listdir(p):
                    try:
                        os.unlink(os.path.join(p, fn))
                    except OSError:
                        pass
                try:
                    os.rmdir(p)
                    removed += 1
                except OSError:
                    pass
    except (OSError, ValueError):
        pass
    return removed


def _after_close(now: float) -> bool:
    ist = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=5, minutes=30)))
    m = ist.hour * 60 + ist.minute
    return ist.weekday() < 5 and 15 * 60 + 30 <= m <= 16 * 60 + 30


def _is_backfilled(symbol: str) -> bool:
    try:
        from feed import bar_store
        return bool(bar_store.is_backfilled(symbol))
    except Exception:  # noqa: BLE001
        return False


def run_once(now: float, market_open: bool, bars_fn: Any = None, symbols_fn: Any = None, day_fn: Any = None, backfilled_fn: Any = None) -> Dict[str, Any]:
    """One recorder pass. The *_fn arguments exist for tests; production reads feed.bar_store."""
    did: Dict[str, Any] = {"wrote": 0, "unchanged": 0, "skipped": None, "final": False, "pruned": 0}
    try:
        if not _cfg("BAR_RECORD_ENABLED", True):
            did["skipped"] = "disabled"
            return did
        if _state["disabled_reason"]:
            did["skipped"] = "disabled_reason"
            return did
        final = (not market_open) and _after_close(now)
        if not market_open and not final:
            did["skipped"] = "closed"
            return did
        with _lock:
            if final:
                if _state["final_day"] == ist_date(now):
                    did["skipped"] = "final_done"
                    return did
            elif now - _state["last_run"] < float(_cfg("BAR_RECORD_EVERY_S", 300)):
                did["skipped"] = "interval"
                return did
            _state["last_run"] = now
            root = str(_cfg("BAR_RECORD_DIR", "/data/bars"))
            try:
                os.makedirs(root, exist_ok=True)
                if not os.access(root, os.W_OK):
                    raise PermissionError(f"{root} is not writable")
            except OSError as e:
                _state["disabled_reason"] = f"{root}: {type(e).__name__}: {str(e)[:100]}"
                logger.warning("bar recorder switched off: %s", _state["disabled_reason"])
                did["skipped"] = "unwritable"
                return did
            if bars_fn is None or symbols_fn is None or day_fn is None:
                from feed import bar_store
                bars_fn, symbols_fn, day_fn, turn = bar_store.get_bars, bar_store.symbols, bar_store.day_number, bar_store.turnover
            else:
                turn = lambda s: 0.0  # noqa: E731
            dn = day_fn()
            if dn is None:
                did["skipped"] = "no_day"
                return did
            date = ist_date(int(dn) * 86400 - IST_OFFSET_S + 43200)
            syms = sorted(symbols_fn() or [], key=lambda s: turn(s), reverse=True)[: int(_cfg("BAR_RECORD_MAX_SYMBOLS", 250))]
            _state["symbols_last"] = len(syms)
            for sym in syms:
                try:
                    size = write_symbol(root, date, sym, bars_fn(sym), bool(backfilled_fn(sym)) if backfilled_fn else _is_backfilled(sym))
                except Exception as e:  # noqa: BLE001
                    _err(f"write {sym}", e)
                    continue
                if size is None:
                    did["unchanged"] += 1
                else:
                    did["wrote"] += 1
                    _state["files_written"] += 1
                    _state["bytes_written"] += size
            if did["wrote"]:
                _state["last_write"] = now
            if final:
                _state["final_day"] = ist_date(now)
                did["final"] = True
            today = ist_date(now)
            if _state["pruned_day"] != today:
                _state["pruned_day"] = today
                did["pruned"] = prune_days(root, int(_cfg("BAR_RECORD_KEEP_DAYS", 45)), today)
    except Exception as e:  # noqa: BLE001
        _err("run_once", e)
    return did


def history_minutes(symbol: str, days: int = 6, root: Optional[str] = None, before_date: Optional[str] = None) -> List[List[float]]:
    """Recorded 1-minute bars of `symbol` over the last `days` day folders (ascending). `before_date` leaves out that day and later, so the caller can add
    today's LIVE bars from the store. Missing days / files are skipped."""
    root = root or str(_cfg("BAR_RECORD_DIR", "/data/bars"))
    try:
        names = sorted((n for n in os.listdir(root) if _DAY_DIR.match(n) and (before_date is None or n < before_date)), reverse=True)
    except OSError:
        return []
    out: List[List[float]] = []
    for n in names[: max(0, int(days))]:
        doc = _read(file_for(root, n, symbol))
        if doc and isinstance(doc.get("bars"), list):
            out = _clean(doc["bars"]) + out
    return out


def stats() -> Dict[str, Any]:
    """For /board/status. Reads the directory listing only."""
    root = str(_cfg("BAR_RECORD_DIR", "/data/bars"))
    days: List[str] = []
    try:
        days = sorted(n for n in os.listdir(root) if _DAY_DIR.match(n))
    except OSError:
        pass
    last_files = 0
    if days:
        try:
            last_files = sum(1 for n in os.listdir(os.path.join(root, days[-1])) if n.endswith(".json"))
        except OSError:
            pass
    return {"enabled": bool(_cfg("BAR_RECORD_ENABLED", True)), "dir": root, **dict(_state),
            "days_on_disk": len(days), "first_day": days[0] if days else None, "last_day": days[-1] if days else None, "last_day_files": last_files}
