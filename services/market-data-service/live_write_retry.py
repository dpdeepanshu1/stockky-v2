"""E285 round 159 (plan item 17): retry-once box for failed `live_quotes` writes.

Before this, a batch the AngelOne background writer failed to write was dropped (it had already been popped from the pending queue), so
a symbol that is only polled every FEED_COLD_INTERVAL_S (30 s) had no fresh row until its next poll, and a Dhan chunk that failed twice
was lost the same way. Both writers now put the rows of a failed write in a RetryBox and send them ONCE more, in smaller transactions
(LIVE_QUOTES_RETRY_CHUNK rows), on their next pass. Rules, each there for a reason:

  * ONCE: a retry that fails is dropped, not queued again. A saturated database must not be fed the same rows forever.
  * NEWER WINS: a row is skipped when its symbol was written successfully, or has a newer row waiting, after the failure.
  * NOT TOO OLD: the table stamps `updated_at` with the time of the WRITE, so a very old price retried late would look fresh. Rows older
    than LIVE_QUOTES_RETRY_MAX_AGE_S (default 12 s, inside the 20 s freshness window real-trade-service applies) are dropped. 0 = retry off.
  * BACK-OFF STILL RULES: while the shared Oracle back-off is open only held-position (priority) rows are retried; the rest wait and
    usually expire, exactly as for ordinary rows (group347 / group356).

Kept in its own module (no database or network imports) so it can be unit-tested on its own. Nothing here raises.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Callable, Iterable, List, Tuple

_CAP = 1500


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        raw = (os.getenv(name) or "").strip()
        v = float(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


def max_age_s() -> float:
    """LIVE_QUOTES_RETRY_MAX_AGE_S (default 12; blank / bad -> 12; 0 = no retry)."""
    return _env_float("LIVE_QUOTES_RETRY_MAX_AGE_S", 12.0, 0.0, 120.0)


def chunk_size() -> int:
    """LIVE_QUOTES_RETRY_CHUNK (default 10 rows per retry transaction; blank / bad -> 10; 1 to 200)."""
    return int(_env_float("LIVE_QUOTES_RETRY_CHUNK", 10.0, 1.0, 200.0))


def chunks(items: list, size: int):
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i:i + size]


class RetryBox:
    """Failed rows by key (symbol), each with the monotonic time it failed. Thread-safe."""

    def __init__(self, cap: int = _CAP) -> None:
        self._cap = cap
        self._lock = threading.Lock()
        self._rows: dict = {}
        self.stats = {"queued": 0, "written": 0, "failed": 0, "expired": 0, "superseded": 0, "overflow": 0}

    def __len__(self) -> int:
        return len(self._rows)

    def clear(self) -> None:
        with self._lock:
            self._rows.clear()
            for k in self.stats:
                self.stats[k] = 0

    def add(self, items: Iterable[Tuple[str, object]], now: float = None) -> int:
        """Remember failed rows [(key, payload), ...]. A newer failed row for the same key replaces the older one. Returns how many were kept."""
        if max_age_s() <= 0:
            return 0
        n = 0
        try:
            now = time.monotonic() if now is None else now
            with self._lock:
                for key, payload in items:
                    if not key:
                        continue
                    if key not in self._rows and len(self._rows) >= self._cap:
                        self.stats["overflow"] += 1
                        continue
                    self._rows[key] = (payload, now)
                    self.stats["queued"] += 1
                    n += 1
        except Exception:  # noqa: BLE001
            pass
        return n

    def take_due(self, superseded: Callable[[str, float], bool], hold: Callable[[str], bool], now: float = None) -> List[Tuple[str, object]]:
        """Remove and return the rows to retry now, [(key, payload), ...].

        Dropped (counted): rows older than max_age_s(), and rows `superseded(key, failed_at)` says a newer write or row has replaced.
        Kept for a later pass: rows `hold(key)` says must wait (the Oracle back-off is open and the symbol is not a held position)."""
        due: List[Tuple[str, object]] = []
        try:
            limit = max_age_s()
            now = time.monotonic() if now is None else now
            with self._lock:
                for key in list(self._rows.keys()):
                    payload, ts = self._rows[key]
                    if limit <= 0 or now - ts > limit:
                        del self._rows[key]
                        self.stats["expired"] += 1
                        continue
                    try:
                        if superseded(key, ts):
                            del self._rows[key]
                            self.stats["superseded"] += 1
                            continue
                        if hold(key):
                            continue
                    except Exception:  # noqa: BLE001 - a bad callback must not lose or block the row
                        pass
                    del self._rows[key]
                    due.append((key, payload))
        except Exception:  # noqa: BLE001
            return []
        return due

    def note(self, written: int = 0, failed: int = 0) -> None:
        with self._lock:
            self.stats["written"] += int(written)
            self.stats["failed"] += int(failed)

    def snapshot(self) -> dict:
        with self._lock:
            return {"waiting": len(self._rows), "max_age_s": max_age_s(), "chunk": chunk_size(), **self.stats}
