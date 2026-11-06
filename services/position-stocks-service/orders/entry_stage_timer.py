"""orders/entry_stage_timer.py - round 225: where does the time go inside one attempt_entry call? (log only, never changes an entry).

6 Nov: one entry took about 1.8 s between the last gate and the order and the cause was a guess (the Market Board log write was blamed but it is a
queue put; the DB insert runs on its own thread). attempt_entry() now marks the end of a few stages; when the whole call takes longer than
ENTRY_STAGE_SLOW_S (default 1.0 s, 0 = never log) ONE WARNING lists every stage that took at least ENTRY_STAGE_MIN_S (default 0.1 s), largest first.
ENTRY_STAGE_LOG=0 turns the timer off. Thread-local (attempt_entry is synchronous), cheap (a few perf_counter calls), never raises."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import config

logger = logging.getLogger("position-stocks-entry-stage")

_tl = threading.local()


def _enabled() -> bool:
    return bool(getattr(config, "ENTRY_STAGE_LOG", True))


def begin(symbol: str) -> None:
    try:
        if not _enabled():
            _tl.cur = None
            return
        now = time.perf_counter()
        _tl.cur = {"symbol": symbol, "t0": now, "last": now, "stages": []}
    except Exception:  # noqa: BLE001
        _tl.cur = None


def mark(name: str) -> None:
    """Close the stage that just finished: its duration is the time since the previous mark (or begin)."""
    try:
        cur = getattr(_tl, "cur", None)
        if cur is None:
            return
        now = time.perf_counter()
        cur["stages"].append((name, now - cur["last"]))
        cur["last"] = now
    except Exception:  # noqa: BLE001 - a timing note must never touch an entry
        logger.debug("entry stage timer: mark(%s) failed (ignored)", name, exc_info=True)


def format_line(symbol: str, total_s: float, stages: list, min_s: float, outcome: str) -> str:
    big = sorted((s for s in stages if s[1] >= min_s), key=lambda s: -s[1])
    parts = ", ".join(f"{n} {d:.2f}s" for n, d in big) or "no single stage reached the minimum"
    return f"position-stocks entry SLOW: {symbol} attempt_entry took {total_s:.2f}s ({outcome}); stages: {parts}"


def end(outcome: str = "ok") -> Optional[str]:
    """Close the call; logs one WARNING when it was slow. Returns the line (for tests) or None. Never raises."""
    try:
        cur = getattr(_tl, "cur", None)
        _tl.cur = None
        if cur is None:
            return None
        now = time.perf_counter()
        if now - cur["last"] > 0:
            cur["stages"].append(("after_last_mark", now - cur["last"]))
        total = now - cur["t0"]
        slow = float(getattr(config, "ENTRY_STAGE_SLOW_S", 1.0) or 0.0)
        if slow <= 0 or total < slow:
            return None
        line = format_line(cur["symbol"], total, cur["stages"], float(getattr(config, "ENTRY_STAGE_MIN_S", 0.1) or 0.0), outcome)
        logger.warning(line)
        return line
    except Exception:  # noqa: BLE001
        return None
