"""execution/shared_rate_window.py - E285 round 87: ONE account-wide one-second window of Dhan order calls, shared by position-stocks-service and real-trade-service.

Why: Dhan's order API allows about 10 requests a second for the whole account (secondary sources; verify on Dhan's page). execution/order_rate.py (E282) counts calls per
PROCESS, so it cannot see the other service's calls. Both services append every Dhan order call here; position-stocks-service stop / target modifies then wait while the
account-wide count is near the limit. Real Trade only records (its broker-stop calls are protective and never wait).

How: a small JSON file on a Docker volume both containers mount (default /data/shared/order_window.json, ORDER_RATE_SHARED_FILE), guarded by an flock on `<file>.lock`.
Each entry is [wall-clock seconds, service, kind]; entries older than one second are dropped on every touch. Wall-clock time is used because processes do not share a
monotonic origin.

Fail open, always: no file system, no flock (Windows), a lock held for more than ~50 ms, a corrupt file - every function returns None / False and the caller goes on with its
own per-process window only. Nothing here raises. IDENTICAL copy in both services (scripts/check_shared_copies.py).

  record(service, kind) -> bool                    append one call (True when written)
  total_last_s(now=None) -> (total, by_service)|None   calls in the last second, or None when the file cannot be used
  snapshot() -> dict                                what GET /live/state shows
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("shared-rate-window")

WINDOW_S = 1.0
LOCK_WAIT_S = 0.05
MAX_ENTRIES = 200                      # a runaway writer cannot grow the file without bound
DEFAULT_PATH = "/data/shared/order_window.json"

_clock = time.time                     # tests replace this
_last_error: Optional[str] = None


def path() -> str:
    return (os.getenv("ORDER_RATE_SHARED_FILE") or DEFAULT_PATH).strip() or DEFAULT_PATH


class _Locked:
    """flock on `<file>.lock` for at most LOCK_WAIT_S; `ok` is False when the lock could not be taken (then the caller fails open)."""

    def __init__(self) -> None:
        self.ok = False
        self._fh: Any = None

    def __enter__(self) -> "_Locked":
        try:
            import fcntl
            p = path()
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            self._fh = open(p + ".lock", "a+")
            end = time.monotonic() + LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    self.ok = True
                    break
                except OSError:
                    if time.monotonic() >= end:
                        break
                    time.sleep(0.002)
        except Exception as e:  # noqa: BLE001 - no fcntl, read-only volume, ...
            _set_error(f"{type(e).__name__}: {e}")
        return self

    def __exit__(self, *_a: Any) -> None:
        try:
            if self._fh is not None:
                if self.ok:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
                self._fh.close()
        except Exception:  # noqa: BLE001
            pass


def _set_error(msg: Optional[str]) -> None:
    global _last_error
    _last_error = msg


def _load(now: float) -> List[list]:
    """The entries still inside the window. A missing / empty / corrupt file is an empty window."""
    try:
        with open(path(), "r", encoding="utf-8") as fh:
            raw = fh.read()
        doc = json.loads(raw) if raw.strip() else []
        rows = doc if isinstance(doc, list) else []
    except FileNotFoundError:
        return []
    except Exception:  # noqa: BLE001 - corrupt file: start again
        return []
    out: List[list] = []
    for r in rows:
        try:
            ts = float(r[0])
            if now - WINDOW_S < ts <= now + 1.0:          # a timestamp from the future (clock step) is kept for at most a second
                out.append([ts, str(r[1])[:8], str(r[2])[:24]])
        except Exception:  # noqa: BLE001
            continue
    return out[-MAX_ENTRIES:]


def _save(rows: List[list]) -> None:
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, separators=(",", ":"))
    os.replace(tmp, path())


def record(service: str, kind: str) -> bool:
    """Append one Dhan order call to the account-wide window. Counts only, never waits, never raises."""
    try:
        with _Locked() as lk:
            if not lk.ok:
                return False
            now = _clock()
            rows = _load(now)
            rows.append([round(now, 4), str(service)[:8], str(kind)[:24]])
            _save(rows[-MAX_ENTRIES:])
            _set_error(None)
            return True
    except Exception as e:  # noqa: BLE001
        _set_error(f"{type(e).__name__}: {e}")
        return False


def total_last_s(now: Optional[float] = None) -> Optional[Tuple[int, Dict[str, int]]]:
    """(calls in the last second, per service) or None when the shared file cannot be used. Never raises."""
    try:
        with _Locked() as lk:
            if not lk.ok:
                return None
            t = _clock() if now is None else float(now)
            rows = _load(t)
            by: Dict[str, int] = {}
            for r in rows:
                by[r[1]] = by.get(r[1], 0) + 1
            _set_error(None)
            return len(rows), by
    except Exception as e:  # noqa: BLE001
        _set_error(f"{type(e).__name__}: {e}")
        return None


def snapshot() -> Dict[str, Any]:
    got = total_last_s()
    out: Dict[str, Any] = {"file": path(), "working": got is not None, "calls_last_s": got[0] if got else None,
                           "by_service_last_s": got[1] if got else {}}
    if _last_error:
        out["last_error"] = _last_error[:140]
    return out
