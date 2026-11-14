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


def management_open(now: datetime, holidays: Sequence[str] = (), until: dtime = dtime(16, 0)) -> bool:
    """Round 298: the short stretch after the close (15:31-16:00) in which the runner still MANAGES what is open (an exit in doubt, a blocked exit, an unknown entry). It never analyses or enters then."""
    return now.weekday() < 5 and now.date().isoformat() not in set(holidays) and dtime(15, 31) < now.time() <= until


class Runner:
    def __init__(self, engine: Engine, cfg: Any, data_dir: str):
        self.engine, self.cfg, self.data_dir = engine, cfg, data_dir
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_snap: Dict[str, float] = {}
        self.snapshots_today = 0
        self._rec_day: Optional[str] = None                  # round 507: the IST day the counters below belong to
        self.first_snapshot_ts: Optional[float] = None       # first snapshot written today (any underlying)
        self.chain_misses: Dict[str, int] = {}               # today: cycles that wanted a snapshot and got no chain, per underlying
        self._miss_logged: Dict[str, float] = {}
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

    def _roll_day(self, now: datetime) -> None:
        """Round 507 (E527): snapshots_today counted since the PROCESS started, so a container that stays up over midnight showed yesterday's snapshots as today's."""
        day = day_of(now.timestamp())
        if day != self._rec_day:
            self._rec_day, self.snapshots_today, self.first_snapshot_ts, self.chain_misses, self._miss_logged = day, 0, None, {}, {}

    def _miss(self, und: str, now: datetime, why: str) -> None:
        self.chain_misses[und] = self.chain_misses.get(und, 0) + 1
        if now.timestamp() - self._miss_logged.get(und, 0.0) >= 300.0:       # one line per underlying per 5 minutes, not one per cycle
            self._miss_logged[und] = now.timestamp()
            log.warning("no snapshot of %s at %s: %s (%d missed today)", und, now.strftime("%H:%M:%S"), why, self.chain_misses[und])

    def recording_status(self, now: datetime, holidays: Sequence[str] = ()) -> Dict[str, Any]:
        """Round 507: when today's recording really began and whether the replay can trust the opening range (the replay's own rule: first snapshot after 09:20 = late)."""
        self._roll_day(now)
        first = datetime.fromtimestamp(self.first_snapshot_ts, now.tzinfo) if self.first_snapshot_ts else None
        started = datetime.fromtimestamp(self.started_at, now.tzinfo) if self.started_at else None
        is_open_day = now.weekday() < 5 and now.date().isoformat() not in set(holidays)
        late = bool(first and first.time() > dtime(9, 20)) or bool(first is None and is_open_day and now.time() > dtime(9, 20) and now.time() <= dtime(15, 31))
        why = ""
        if late and first is not None:
            why = f"first snapshot at {first:%H:%M}" + (f" (the service started at {started:%H:%M})" if started and started.date() == now.date() and started.time() > dtime(9, 20) else "")
        elif late:
            why = "no snapshot yet today" + (f" (the service started at {started:%H:%M})" if started and started.date() == now.date() else "")
        return {"snapshots_today": self.snapshots_today, "first_snapshot": first.strftime("%H:%M") if first else None, "started_at": started.strftime("%d %b %H:%M") if started else None,
                "chain_misses": dict(self.chain_misses), "late": late, "detail": why}

    def snapshot(self, now: datetime) -> None:
        """Record one chain per underlying per SNAPSHOT_INTERVAL_S (the expiry the engine would trade)."""
        self._roll_day(now)
        if not getattr(self.cfg, "FNO_RECORD_SNAPSHOTS", True):
            return
        p = Params.from_config(self.cfg)
        try:
            vix = self.engine.provider.vix()
        except Exception:  # noqa: BLE001 - a chain is still worth saving without the VIX
            vix = None
        for und in self.engine.underlyings():
            try:
                self._snapshot_one(und, now, p, vix)
            except Exception:  # noqa: BLE001 - round 302: one underlying that raises must not stop the other from being recorded
                log.exception("snapshot of %s failed", und)

    def _snapshot_one(self, und: str, now: datetime, p: Params, vix: Optional[float]) -> None:
        if now.timestamp() - self._last_snap.get(und, 0.0) < float(getattr(self.cfg, "SNAPSHOT_INTERVAL_S", 60.0)):
            return
        exp = self.engine.pick_expiry(und, now.date(), p)
        ch = self.engine.provider.chain(und, exp) if exp else None
        if ch is None:
            self._miss(und, now, "no expiry to record" if not exp else f"the chain request for {exp} returned nothing")
            return
        self._last_snap[und] = now.timestamp()
        if recorder.record(self.data_dir, ch, now.timestamp(), {"vix": vix}):
            self.engine.store.add_snapshot(now.timestamp(), und, ch.expiry, recorder.snapshot_path(self.data_dir, day_of(now.timestamp()), und))
            self.snapshots_today += 1
            if self.first_snapshot_ts is None:
                self.first_snapshot_ts = now.timestamp()
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
                    try:
                        if getattr(self.cfg, "FNO_ENGINE_ENABLED", False) or self.engine.store.open_positions():
                            self.engine.tick(now)
                    finally:
                        self.snapshot(now)                 # round 302: recording does not depend on the engine cycle succeeding (the replay needs every minute it can get)
                elif management_open(now, getattr(self.engine, "holidays", ())) and self.engine.store.open_positions():
                    self.engine.tick(now, manage_only=True)        # round 298: unresolved exits / entries were left until the next 09:15; look at them for a bit after the close (a SELL then needs Dhan's net position)
                self.last_error = None
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"
                log.exception("runner cycle failed")
            self._stop.wait(max(0.5, float(getattr(self.cfg, "POLL_INTERVAL_S", 5.0)) - (time.time() - t0)))
