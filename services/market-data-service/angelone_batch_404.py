"""E285 round 163 (plan item 20, Angel One 404 batch): what the AngelOne poll does when a quote batch answers HTTP 404.

Before this, a batch that answered 404 went through `raise_for_status()` and the feed's generic handler, which logged one WARNING per
failed batch per poll cycle (`AngelOne quote batch (a-b) failed: Client error '404 Not Found' for url ...`) for as long as the answer
lasted, without the response body (the only thing that says WHY), and sent the same batch again on the next pass. 403 / 429 already have
their own rules (cooldown, throttled body log); a 404 had none.

Rules, each there for a reason:

  * LOG ONCE PER WINDOW: the first 404 batch failure writes one WARNING with the response body and the number of failures since the last
    line (`ANGELONE_BATCH_404_LOG_EVERY_S`, default 600). The others go to DEBUG. 0 = log every one (the old noise).
  * BODY SHOWN: the log line carries the first 300 characters of the response, which tells a bad token list from a changed endpoint.
  * BACKGROUND ONLY IS PAUSED: a BACKGROUND (cold, whole-universe) batch that answered 404 is not sent again for
    `ANGELONE_BATCH_404_BACKOFF_S` seconds (default 120; 0 = never pause). Held-position and candidate batches, and the old unlaned plan
    (which can contain held symbols), are NEVER paused: they are retried every cycle exactly as before.
  * FAIL OPEN: the pause is keyed on the exact token list of the batch. When the list changes (the hot set moved) or the pause ends, the
    batch is sent again; one success clears the memory of it. Nothing here changes a price, a score or an order.

Kept in its own module (no database or network imports) so it can be unit-tested on its own. Nothing here raises.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, Optional, Tuple

_CAP = 400


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        raw = (os.getenv(name) or "").strip()
        v = float(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


def backoff_s() -> float:
    """ANGELONE_BATCH_404_BACKOFF_S (default 120; blank / bad -> 120; 0 = never pause a batch)."""
    return _env_float("ANGELONE_BATCH_404_BACKOFF_S", 120.0, 0.0, 3600.0)


def log_every_s() -> float:
    """ANGELONE_BATCH_404_LOG_EVERY_S (default 600; blank / bad -> 600; 0 = log every failure)."""
    return _env_float("ANGELONE_BATCH_404_LOG_EVERY_S", 600.0, 0.0, 86400.0)


def is_not_found(exc: BaseException) -> bool:
    """True when `exc` is an HTTP error whose response status is 404 (httpx.HTTPStatusError and look-alikes). Never raises."""
    try:
        return getattr(getattr(exc, "response", None), "status_code", None) == 404
    except Exception:  # noqa: BLE001
        return False


def response_body(exc: BaseException, limit: int = 300) -> str:
    """The first `limit` characters of the error response body on one line ('' when there is none). Never raises."""
    try:
        text = getattr(getattr(exc, "response", None), "text", "") or ""
        return str(text)[:limit].replace("\n", " ").replace("\r", " ").strip()
    except Exception:  # noqa: BLE001
        return ""


def batch_key(tokens: Iterable) -> Tuple[int, int]:
    """A cheap identity for a token list: (length, hash of the tuple). Order matters, as the planner's order is stable."""
    try:
        t = tuple(str(x) for x in tokens)
        return (len(t), hash(t))
    except Exception:  # noqa: BLE001
        return (0, 0)


class BatchBackoff:
    """Pauses and throttled logging for batches that answered 404. Thread-safe; the clock is injectable for tests."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._until: dict = {}          # batch key -> monotonic time until which the batch is not sent
        self._last_log = 0.0            # monotonic time of the last WARNING (0 = never)
        self._since_log = 0             # 404 failures counted since that WARNING
        self.stats = {"not_found": 0, "paused_skips": 0, "logged": 0, "cleared": 0}

    def reset(self) -> None:
        with self._lock:
            self._until.clear()
            self._last_log = 0.0
            self._since_log = 0
            for k in self.stats:
                self.stats[k] = 0

    def should_skip(self, tokens: Iterable, background: bool) -> bool:
        """True when this BACKGROUND batch answered 404 recently and its pause has not ended. Other lanes: always False."""
        try:
            if not background or backoff_s() <= 0:
                return False
            key = batch_key(tokens)
            now = self._clock()
            with self._lock:
                until = self._until.get(key)
                if until is None:
                    return False
                if now >= until:
                    del self._until[key]
                    return False
                self.stats["paused_skips"] += 1
                return True
        except Exception:  # noqa: BLE001
            return False

    def note_not_found(self, tokens: Iterable, background: bool) -> Tuple[bool, int]:
        """Record one 404 for this batch. Returns (log_now, failures_since_last_line): log_now is True for the first failure and then at
        most once per `log_every_s()`; the count is what the line should report (this failure included)."""
        try:
            now = self._clock()
            with self._lock:
                self.stats["not_found"] += 1
                self._since_log += 1
                if background and backoff_s() > 0:
                    if len(self._until) >= _CAP:
                        for k in [k for k, v in self._until.items() if v <= now]:
                            del self._until[k]
                        if len(self._until) >= _CAP:
                            self._until.clear()
                    self._until[batch_key(tokens)] = now + backoff_s()
                every = log_every_s()
                if self._last_log <= 0.0 or every <= 0 or (now - self._last_log) >= every:
                    n = self._since_log
                    self._last_log = now or 1e-9
                    self._since_log = 0
                    self.stats["logged"] += 1
                    return True, n
                return False, self._since_log
        except Exception:  # noqa: BLE001
            return True, 1

    def note_ok(self, tokens: Iterable) -> None:
        """A successful answer for this batch clears any pause held for it."""
        try:
            key = batch_key(tokens)
            with self._lock:
                if self._until.pop(key, None) is not None:
                    self.stats["cleared"] += 1
        except Exception:  # noqa: BLE001
            pass

    def snapshot(self) -> dict:
        with self._lock:
            now = self._clock()
            return {**self.stats, "paused_batches": sum(1 for v in self._until.values() if v > now),
                    "backoff_s": backoff_s(), "log_every_s": log_every_s()}


BOX = BatchBackoff()
