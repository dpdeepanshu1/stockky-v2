"""Round 669 (E691, D2b): one live_quotes write transaction at a time per process, and a fixed row order inside it.

The 20 Nov database wait samples (3 h, ASH) were: 1,239 `log file sync` and 622 `enq: TX - row lock contention`, against 122 ON CPU.
Every sampled row-lock waiter was the same statement, `MERGE INTO live_quotes ... USING (SELECT :s ... FROM dual)`, run by this
service's python process. The blockers were sessions of the same process sitting INACTIVE (their transaction was open, waiting
for its commit to be written). `live_quotes` is written by TWO threads of market-data-service that touch the same symbols:

  * the AngelOne writer (`angelone_ws_feed._db_writer_drain`, `_ao_retry_pass`), up to BATCH_SIZE rows per transaction, and
  * the Dhan writer (`dhan_data/live_store._write_sync`, `retry_failed`), chunks of DHAN_LIVE_WRITE_CHUNK rows.

Each used its own pooled connection and its own row order, so writer B blocked on rows writer A had updated but not yet committed
(a commit costs a `log file sync` on this database), and two writers with opposite row orders can also deadlock (ORA-00060). A wait of
3 to 8 s then looks like a DPY-4024 call timeout to the client.

Two changes, both here so the two writers cannot drift apart:

  1. `write_slot()`: a process-wide lock held for the whole transaction (execute AND commit). Rows of the two writers no longer
     overlap in time, so neither can wait on the other's row locks. The wait for the lock is capped (LIVE_QUOTES_WRITE_LOCK_WAIT_S,
     default 6 s); when the cap passes the write is reported as failed, the rows go to the existing retry box, and no extra database
     work is started. LIVE_QUOTES_SINGLE_WRITER=0 turns the lock off (old behaviour).
  2. `ordered()`: rows are sorted by symbol before they are sent, so any other process that ever writes the same table in
     sorted order locks rows in the same order and cannot deadlock with this one.

The lock is per process. Other containers do not write `live_quotes` (checked: only market-data-service has the MERGE statement).
No database or network imports: unit-testable on its own. Nothing here raises except `WriteSlotBusy`, which callers already treat
like any other failed write.
"""
from __future__ import annotations

import contextlib
import os
import threading
import time
from typing import Callable, Iterable, List

_LOCK = threading.Lock()
_STATS = {"acquired": 0, "busy": 0, "wait_s_total": 0.0, "wait_s_max": 0.0, "held_s_max": 0.0}
_STATS_LOCK = threading.Lock()


class WriteSlotBusy(Exception):
    """The live_quotes write slot could not be taken in time. Deliberately has no 'timeout' / 'timed out' / 'DPY-4024' in its text:
    callers treat those words as a database timeout and open the shared back-off, and this is not one."""


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        raw = (os.getenv(name) or "").strip()
        v = float(raw) if raw else default
    except (TypeError, ValueError):
        return default
    return min(hi, max(lo, v))


def enabled() -> bool:
    """LIVE_QUOTES_SINGLE_WRITER (default on; 0 / false / off / no = no lock, as before round 669)."""
    return ((os.getenv("LIVE_QUOTES_SINGLE_WRITER") or "").strip() or "1").lower() not in ("0", "false", "off", "no")


def wait_s() -> float:
    """LIVE_QUOTES_WRITE_LOCK_WAIT_S (default 6; blank / bad -> 6; 0.5 to 30)."""
    return _env_float("LIVE_QUOTES_WRITE_LOCK_WAIT_S", 6.0, 0.5, 30.0)


@contextlib.contextmanager
def write_slot(wait: float | None = None):
    """Hold the live_quotes write slot for one transaction. Wrap the whole `with engine.begin()` block so the commit is inside.
    Raises WriteSlotBusy when the slot is not free within `wait` seconds (default wait_s())."""
    if not enabled():
        yield
        return
    limit = wait_s() if wait is None else max(0.0, float(wait))
    t0 = time.monotonic()
    got = _LOCK.acquire(timeout=limit)
    waited = time.monotonic() - t0
    with _STATS_LOCK:
        if got:
            _STATS["acquired"] += 1
            _STATS["wait_s_total"] += waited
            _STATS["wait_s_max"] = max(_STATS["wait_s_max"], waited)
        else:
            _STATS["busy"] += 1
    if not got:
        raise WriteSlotBusy(f"live_quotes write slot busy for {limit:.1f}s")
    t1 = time.monotonic()
    try:
        yield
    finally:
        held = time.monotonic() - t1
        _LOCK.release()
        with _STATS_LOCK:
            _STATS["held_s_max"] = max(_STATS["held_s_max"], held)


def ordered(rows: Iterable, key: Callable | None = None) -> List:
    """Rows sorted by symbol (a stable order every writer shares). `key` maps a row to its symbol; the default reads tuple[0] or dict['s'] / ['symbol']."""
    def _sym(r):
        if key is not None:
            return str(key(r) or "")
        if isinstance(r, dict):
            return str(r.get("s") or r.get("symbol") or "")
        try:
            return str(r[0] or "")
        except Exception:  # noqa: BLE001
            return ""
    return sorted(rows, key=_sym)


def status() -> dict:
    with _STATS_LOCK:
        out = dict(_STATS)
    out["enabled"] = enabled()
    out["wait_limit_s"] = wait_s()
    out["locked_now"] = _LOCK.locked()
    for k in ("wait_s_total", "wait_s_max", "held_s_max"):
        out[k] = round(out[k], 3)
    return out
