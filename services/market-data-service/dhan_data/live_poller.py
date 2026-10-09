"""dhan_data/live_poller.py - keeps the quote batcher (and live_quotes) warm during market hours (group 270).

Every DHAN_LIVE_POLL_S seconds, while the NSE feed window is open, the poller queues the feed universe as BACKGROUND
symbols. The batcher merges them with on-demand callers into one request per second and the live writer upserts the
rows. A universe larger than DHAN_LIVE_MAX_SYMBOLS is covered in rotating slices.

It also runs the shadow comparison (DHAN_SHADOW_COMPARE=1): Dhan's price against AngelOne's in-memory live price for
the same symbol, logged when they differ by more than 0.5% and summarised in /internal/dhan-status. That is how you
validate Dhan during a real session BEFORE it becomes the first provider.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, List, Optional

from . import client, config, live_store, quotes

logger = logging.getLogger("dhan-data.poller")

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_offset = 0
_cmp = {"compared": 0, "over_half_pct": 0, "max_diff_pct": 0.0, "sum_diff_pct": 0.0}
_cmp_lock = threading.Lock()
_listener_added = False


def next_slice(universe: List[str], offset: int, size: int) -> tuple[List[str], int]:
    """Rotating window over `universe`: (symbols, new_offset). Pure function (tested)."""
    n = len(universe)
    if n == 0:
        return [], 0
    if n <= size:
        return list(universe), 0
    start = offset % n
    chunk = universe[start:start + size]
    if len(chunk) < size:
        chunk = chunk + universe[:size - len(chunk)]
    return chunk, (start + size) % n


def _compare(rows: List[dict]) -> None:
    """Shadow comparison against AngelOne's in-memory live price. Never raises."""
    try:
        import angelone_ws_feed
    except Exception:  # noqa: BLE001
        return
    for r in rows[:200]:
        try:
            ao = angelone_ws_feed.get_live_quote(r["symbol"])
            p_ao = float((ao or {}).get("price") or 0)
            p_dh = float(r.get("price") or 0)
            if p_ao <= 0 or p_dh <= 0:
                continue
            d = abs(p_dh - p_ao) / p_ao * 100.0
            with _cmp_lock:
                _cmp["compared"] += 1
                _cmp["sum_diff_pct"] += d
                _cmp["max_diff_pct"] = max(_cmp["max_diff_pct"], d)
                if d > 0.5:
                    _cmp["over_half_pct"] += 1
            if d > 0.5:
                logger.warning("dhan shadow: %s dhan=%.2f angelone=%.2f (%.2f%% apart)", r["symbol"], p_dh, p_ao, d)
        except Exception:  # noqa: BLE001
            continue


def _on_rows(rows: List[dict]) -> None:
    live_store.submit(rows)
    if config.shadow_compare():
        _compare(rows)


def _run(get_universe: Callable[[], List[str]]) -> None:
    global _offset
    while not _stop.is_set():
        try:
            if not (config.enabled() and config.live_poller_enabled()):
                _stop.wait(5.0)
                continue
            try:
                from market_hours import is_feed_window_ist
                in_window = is_feed_window_ist()
            except Exception:  # noqa: BLE001
                in_window = True
            if not in_window:
                _stop.wait(30.0)
                continue
            if not client.available():
                _stop.wait(5.0)
                continue
            universe = [s for s in (get_universe() or []) if s]
            chunk, _offset = next_slice(universe, _offset, config.live_max_symbols())
            if chunk:
                quotes.request(chunk, background=True)
        except Exception as e:  # noqa: BLE001 - the poller must never die
            logger.debug("dhan poller loop error: %s", type(e).__name__)
        _stop.wait(config.live_poll_interval_s())


def start(get_universe: Callable[[], List[str]]) -> None:
    """Idempotent. Registers the writer/compare listener once and starts the poll thread."""
    global _thread, _listener_added
    if not _listener_added:
        quotes.add_listener(_on_rows)
        _listener_added = True
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, args=(get_universe,), name="dhan-live-poller", daemon=True)
    _thread.start()
    logger.info("dhan live poller started (interval %.1fs, max %d symbols, live_quotes write: %s)",
                config.live_poll_interval_s(), config.live_max_symbols(), live_store.write_mode())


def stop() -> None:
    _stop.set()


def status() -> dict:
    with _cmp_lock:
        c = dict(_cmp)
    c["mean_diff_pct"] = round(c["sum_diff_pct"] / c["compared"], 4) if c["compared"] else None
    c.pop("sum_diff_pct", None)
    c["max_diff_pct"] = round(c["max_diff_pct"], 3)
    return {"running": bool(_thread and _thread.is_alive()), "shadow_compare": config.shadow_compare(),
            "shadow": c}
