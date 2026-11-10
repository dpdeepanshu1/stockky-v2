"""fno/runtime.py - the background loop: every few seconds during market hours it runs one engine cycle and, when recording is on, saves a chain snapshot once a minute.
A broken cycle is logged and the loop continues; the thread is a daemon and stops with the process."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, time as dtime
from typing import Any, Dict, Optional, Sequence

from . import capital, readiness, recorder
from .engine import Engine, now_ist
from .params import Params
from .store import day_of

log = logging.getLogger("fno.runtime")


def market_open(now: datetime, holidays: Sequence[str] = ()) -> bool:
    """Round 273: a weekday exchange holiday is not open (the runner used to tick, and record chain snapshots, on holidays)."""
    return now.weekday() < 5 and now.date().isoformat() not in set(holidays) and dtime(9, 15) <= now.time() <= dtime(15, 31)


class Runner:
    def __init__(self, engine: Engine, cfg: Any, data_dir: str):
        self.engine, self.cfg, self.data_dir = engine, cfg, data_dir
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_snap: Dict[str, float] = {}
        self.snapshots_today = 0
        self.last_error: Optional[str] = None
        self.started_at: Optional[float] = None

    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.alive():
            return
        self._stop.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._loop, name="fno-runner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, now: datetime) -> None:
        """Record one chain per underlying per SNAPSHOT_INTERVAL_S (the expiry the engine would trade)."""
        if not getattr(self.cfg, "FNO_RECORD_SNAPSHOTS", True):
            return
        p = Params.from_config(self.cfg)
        vix = self.engine.provider.vix()
        for und in self.engine.underlyings():
            if now.timestamp() - self._last_snap.get(und, 0.0) < float(getattr(self.cfg, "SNAPSHOT_INTERVAL_S", 60.0)):
                continue
            exp = self.engine.pick_expiry(und, now.date(), p)
            ch = self.engine.provider.chain(und, exp) if exp else None
            if ch is None:
                continue
            self._last_snap[und] = now.timestamp()
            if recorder.record(self.data_dir, ch, now.timestamp(), {"vix": vix}):
                self.engine.store.add_snapshot(now.timestamp(), und, ch.expiry, recorder.snapshot_path(self.data_dir, day_of(now.timestamp()), und))
                self.snapshots_today += 1
            # Round 238: on a day the weekly contract expires, also record THAT contract (the engine trades next week's, so it was never saved). The expiry-day strategies can only be replayed
            # on recorded premiums if this exists, and it cannot be recorded afterwards. Same file; the replay picks a chain by its expiry.
            today = now.date().isoformat()
            if exp != today and today in (self.engine.provider.expiries(und) or []):
                ch0 = self.engine.provider.chain(und, today)
                if ch0 is not None and recorder.record(self.data_dir, ch0, now.timestamp(), {"vix": vix}):
                    self.engine.store.add_snapshot(now.timestamp(), und, ch0.expiry, recorder.snapshot_path(self.data_dir, day_of(now.timestamp()), und))
                    self.snapshots_today += 1

    def _loop(self) -> None:
        log.info("fno runner started")
        while not self._stop.is_set():
            t0 = time.time()
            try:
                now = now_ist()
                capital.publish(self.engine.store, self.cfg, self.engine.armed_day())      # round 242: F&O's figures for the capital board (throttled, never raises)
                readiness.publish(self.cfg, self.engine.store, self.data_dir, self.engine.last_reconcile, self.engine.armed_day())   # round 262: the REAL lock waits for this list (throttled, never raises)
                if market_open(now, getattr(self.engine, "holidays", ())):
                    if getattr(self.cfg, "FNO_ENGINE_ENABLED", False) or self.engine.store.open_positions():
                        self.engine.tick(now)
                    self.snapshot(now)
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                log.exception("runner cycle failed")
            self._stop.wait(max(0.5, float(getattr(self.cfg, "POLL_INTERVAL_S", 5.0)) - (time.time() - t0)))
