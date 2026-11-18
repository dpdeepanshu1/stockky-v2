"""shared_or_budget.py - round 613: ONE per-day count of live OR rescues (round 612) shared by real-trade-service and position-stocks-service, and kept across restarts.

Why: round 612 counted rescues in a per-process variable, so (a) each service had its own budget (up to 2x the cap in total) and (b) a restart or rebuild in the middle of the day
set the count back to 0 and the day's cap could be spent again. This keeps the count in a small JSON file on the Docker volume both containers already mount
(default /data/shared/or_live_budget.json, OR_LIVE_BUDGET_FILE), guarded by an flock on `<file>.lock`.

How: {"day": "2026-11-18", "n": 3, "by": {"rt": 2, "ps": 1}}. A new day (any other day string) starts again at 0. `take` reads, checks the cap, writes, all under the lock.

If the file cannot be used (no volume, no flock, a lock held for more than LOCK_WAIT_S, a read-only disk) `take` falls back to a per-process count, which is exactly the round 612 behaviour,
and says so (source "process"). A corrupt file is read as an empty day. Nothing here raises. IDENTICAL copy in both services (scripts/check_shared_copies.py).

  take(day, cap, service) -> (ok, used, source)   use one rescue if the day's budget is not spent; `used` is the count after the call, `source` is "file" or "process"
  give_back(day, service, source) -> bool           return one rescue (round 614: the rescued entry never became an order); False = nothing given back, the budget stays spent
  used(day) -> int | None                          today's count from the file (None when the file cannot be used)
  reset_process() / snapshot()                     tests / a status view
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger("shared-or-budget")

LOCK_WAIT_S = 0.25
DEFAULT_PATH = "/data/shared/or_live_budget.json"

_PROC_LOCK = threading.Lock()
_PROC: Dict[str, Any] = {"day": None, "n": 0}
_last_error: Optional[str] = None


def path() -> str:
    return (os.getenv("OR_LIVE_BUDGET_FILE") or DEFAULT_PATH).strip() or DEFAULT_PATH


def shared_enabled() -> bool:
    """OR_LIVE_SHARED_BUDGET=0 goes back to the per-process count of round 612."""
    return (os.getenv("OR_LIVE_SHARED_BUDGET") or "1").strip().lower() not in ("0", "false", "off", "no")


def _set_error(msg: Optional[str]) -> None:
    global _last_error
    _last_error = msg


class _Locked:
    """flock on `<file>.lock` for at most LOCK_WAIT_S; `ok` is False when the lock could not be taken (the caller then uses its per-process count)."""

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
                    time.sleep(0.005)
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


def _load(day: str) -> Dict[str, Any]:
    """The file's record for `day`; a missing / empty / corrupt file, or another day, is an empty record."""
    try:
        with open(path(), "r", encoding="utf-8") as fh:
            raw = fh.read()
        doc = json.loads(raw) if raw.strip() else {}
        if isinstance(doc, dict) and str(doc.get("day")) == day:
            by = doc.get("by") if isinstance(doc.get("by"), dict) else {}
            return {"day": day, "n": max(0, int(doc.get("n", 0))), "by": {str(k)[:8]: max(0, int(v)) for k, v in by.items()}}
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 - corrupt file: start the day again
        logger.debug("or_live budget file unreadable, counting from 0", exc_info=True)
    return {"day": day, "n": 0, "by": {}}


def _save(rec: Dict[str, Any]) -> None:
    tmp = path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, separators=(",", ":"))
    os.replace(tmp, path())


def _take_process(day: str, cap: int) -> Tuple[bool, int, str]:
    with _PROC_LOCK:
        if _PROC["day"] != day:
            _PROC["day"], _PROC["n"] = day, 0
        if cap <= 0 or _PROC["n"] >= cap:
            return False, int(_PROC["n"]), "process"
        _PROC["n"] += 1
        return True, int(_PROC["n"]), "process"


def take(day: Any, cap: int, service: str = "") -> Tuple[bool, int, str]:
    """Use one rescue of `day`'s budget. Returns (ok, used after the call, "file" | "process"). Never raises; any doubt = the per-process count."""
    d = str(day)
    try:
        cap = int(cap)
    except Exception:  # noqa: BLE001
        return False, 0, "process"
    if cap <= 0:
        return False, 0, "process"
    if not shared_enabled():
        return _take_process(d, cap)
    try:
        with _Locked() as lk:
            if not lk.ok:
                return _take_process(d, cap)
            rec = _load(d)
            if rec["n"] >= cap:
                _set_error(None)
                return False, int(rec["n"]), "file"
            rec["n"] += 1
            k = str(service)[:8] or "?"
            rec["by"][k] = int(rec["by"].get(k, 0)) + 1
            _save(rec)
            _set_error(None)
            return True, int(rec["n"]), "file"
    except Exception as e:  # noqa: BLE001
        _set_error(f"{type(e).__name__}: {e}")
        return _take_process(d, cap)


def give_back(day: Any, service: str = "", source: str = "file") -> bool:
    """Round 614: return one rescue to `day`'s budget (the rescued entry never reached an order). `source` is what `take` said: "file" gives back to the shared file, "process"
    to this process's count. Never goes below 0, never raises; False when nothing could be given back (the budget then stays spent, the safe side)."""
    d = str(day)
    try:
        if source == "process" or not shared_enabled():
            with _PROC_LOCK:
                if _PROC["day"] == d and _PROC["n"] > 0:
                    _PROC["n"] -= 1
                    return True
            return False
        with _Locked() as lk:
            if not lk.ok:
                return False
            rec = _load(d)
            if rec["n"] <= 0:
                return False
            rec["n"] -= 1
            k = str(service)[:8] or "?"
            if int(rec["by"].get(k, 0)) > 0:
                rec["by"][k] = int(rec["by"][k]) - 1
            _save(rec)
            _set_error(None)
            return True
    except Exception as e:  # noqa: BLE001
        _set_error(f"{type(e).__name__}: {e}")
        return False


def used(day: Any) -> Optional[int]:
    """Today's count from the shared file, or None when it cannot be used. Never raises."""
    if not shared_enabled():
        return None
    try:
        with _Locked() as lk:
            if not lk.ok:
                return None
            return int(_load(str(day))["n"])
    except Exception:  # noqa: BLE001
        return None


def reset_process() -> None:
    with _PROC_LOCK:
        _PROC["day"], _PROC["n"] = None, 0


def snapshot(day: Any = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"file": path(), "shared": shared_enabled(), "process_n": int(_PROC["n"]), "process_day": _PROC["day"]}
    if day is not None:
        out["file_n"] = used(day)
    if _last_error:
        out["last_error"] = _last_error[:140]
    return out
