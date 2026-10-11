"""screening/board_runner.py - group309: background loop behind the Market Board. Every LOOP_S: market open and BOARD_LOG_EVERY_S since
the last -> build board, queue a shadow snapshot, prune the bar store; market open or 15:30-16:30 IST on a weekday -> fill forward returns.
Display + shadow logging only; each step is wrapped; blocking work runs in a worker thread (never on the event loop)."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

import config

logger = logging.getLogger("position-stocks-board-runner")

LOOP_S = 30.0
BACKFILL_LOOP_S = 6.0
FILL_EVERY_S = 120.0
_state = {"last_snapshot": 0.0, "last_fill": 0.0, "snapshots": 0, "fills": 0, "errors": 0, "last_error": None, "backfilled": 0}


def state() -> dict:
    return dict(_state)


def reset() -> None:
    _state.update(last_snapshot=0.0, last_fill=0.0, snapshots=0, fills=0, errors=0, last_error=None, backfilled=0)


def in_fill_window(now: float, market_open: bool) -> bool:
    if market_open:
        return True
    from datetime import datetime, timedelta, timezone
    ist = datetime.fromtimestamp(now, tz=timezone(timedelta(hours=5, minutes=30)))
    m = ist.hour * 60 + ist.minute
    return ist.weekday() < 5 and 15 * 60 + 30 <= m <= 16 * 60 + 30


def run_once(now: float, market_open: bool, open_symbols_fn: Optional[Callable[[], list]] = None,
             session_factory: Optional[Callable] = None, today_fn: Optional[Callable[[], str]] = None,
             backfill_fn: Optional[Callable[[], dict]] = None) -> dict:
    from feed import bar_backfill, bar_store
    from screening import board, board_log
    did = {"snapshot": False, "fill": 0, "pruned": 0, "backfill": 0}

    def _failed(step: str, e: Exception) -> None:
        _state["errors"] += 1
        _state["last_error"] = f"{step}: {type(e).__name__}: {e}"
        logger.warning("board runner %s step failed (%s: %s)", step, type(e).__name__, e)

    # group313: each step has its own guard, so a failing backfill can no longer stop snapshots and forward-return fills
    # (before, all three shared one try block and an exception in the first skipped the other two on every pass).
    try:
        if config.BOARD_BACKFILL_ENABLED and bar_backfill.pending_count():       # group311: fill the morning after a mid-session restart
            r = (backfill_fn or bar_backfill.run_pending)() or {}
            did["backfill"] = int(r.get("merged_symbols", 0))
            _state["backfilled"] = _state.get("backfilled", 0) + did["backfill"]
    except Exception as e:  # noqa: BLE001
        _failed("backfill", e)
    try:
        if market_open and now - _state["last_snapshot"] >= config.BOARD_LOG_EVERY_S:
            held = []
            try:
                held = (open_symbols_fn or _open_symbols)() or []
            except Exception as e:  # noqa: BLE001
                logger.debug("board runner: open symbols unavailable (%s)", e)
            payload = board.build(open_symbols=held, now=now, use_cache=False)
            if payload["bullish"] or payload["bearish"]:
                board_log.record_snapshot(payload, now)
                did["snapshot"] = True
                _state["snapshots"] += 1
            _state["last_snapshot"] = now
            did["pruned"] = bar_store.prune()
    except Exception as e:  # noqa: BLE001
        _failed("snapshot", e)
    try:
        if in_fill_window(now, market_open) and now - _state["last_fill"] >= FILL_EVERY_S:
            _state["last_fill"] = now
            factory = session_factory or _session_factory()
            day = (today_fn or _today)()
            if factory is not None:
                with factory() as s:
                    did["fill"] = board_log.fill_forward(s, day, now)
                _state["fills"] += did["fill"]
    except Exception as e:  # noqa: BLE001
        _failed("fill", e)
    return did


def _open_symbols() -> list:
    from feed import md_poller
    return md_poller.open_position_symbols()


def _session_factory():
    try:
        import db as _db
        return _db.get_session_factory()
    except Exception:  # noqa: BLE001
        return None


def _today() -> str:
    from tz_utils import ist_today_str
    return ist_today_str()


async def loop() -> None:
    from tz_utils import is_market_open_ist
    await asyncio.sleep(20.0)
    while True:
        try:
            await asyncio.to_thread(run_once, time.time(), bool(is_market_open_ist()))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.debug("board runner loop: %s", e)
        try:
            from feed import bar_backfill
            quick = bool(config.BOARD_BACKFILL_ENABLED and bar_backfill.pending_count())
        except Exception:  # noqa: BLE001
            quick = False
        await asyncio.sleep(BACKFILL_LOOP_S if quick else LOOP_S)     # a queue of backfills is drained every few seconds
