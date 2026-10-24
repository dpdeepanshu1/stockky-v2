"""Optional IST timetable (group 442). OFF until switched on from the page; stored in DATA_DIR/state/schedule.json.

It only submits packs to the same queue a button would (trigger "schedule"). A slot that was missed (container down at that minute) is NOT caught up:
a calibration started at the wrong time is worse than one skipped. The market-hours guard still applies, so a heavy pack that fires inside market hours is recorded as `blocked`.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from runner import IST, ist_now


def due(entries: List[Dict[str, Any]], last_fired: Dict[str, str], now: datetime, window_min: int = 10) -> List[Dict[str, Any]]:
    """Entries whose time was reached in the last `window_min` minutes today (IST), enabled, right weekday, not yet fired today."""
    now = now.astimezone(IST)
    today = now.strftime("%Y-%m-%d")
    out = []
    for e in entries:
        if not e.get("enabled") or now.weekday() not in e.get("days", []):
            continue
        if e.get("first_week_only") and now.day > 7:
            continue
        try:
            hh, mm = [int(x) for x in str(e["time"]).split(":")]
        except Exception:  # noqa: BLE001
            continue
        mins = now.hour * 60 + now.minute - (hh * 60 + mm)
        if 0 <= mins < window_min and last_fired.get(e["id"]) != today:
            out.append(e)
    return out


def validate_entries(entries: Any, pack_ids: List[str]) -> List[Dict[str, Any]]:
    if not isinstance(entries, list) or len(entries) > 30:
        raise ValueError("schedule must be a list of at most 30 slots")
    clean, seen = [], set()
    for e in entries:
        if not isinstance(e, dict):
            raise ValueError("each slot must be an object")
        sid = str(e.get("id", "")).strip()
        if not sid or len(sid) > 40 or not all(c.isalnum() or c in "_-" for c in sid) or sid in seen:
            raise ValueError(f"bad or duplicate slot id: {sid!r}")
        seen.add(sid)
        if e.get("pack") not in pack_ids:
            raise ValueError(f"{sid}: unknown pack {e.get('pack')!r}")
        days = e.get("days")
        if not isinstance(days, list) or not days or any((not isinstance(d, int)) or d < 0 or d > 6 for d in days):
            raise ValueError(f"{sid}: days must be a list of 0 (Mon) .. 6 (Sun)")
        t = str(e.get("time", ""))
        try:
            hh, mm = [int(x) for x in t.split(":")]
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:  # noqa: BLE001
            raise ValueError(f"{sid}: time must be HH:MM (IST)")
        clean.append({"id": sid, "pack": e["pack"], "days": sorted(set(days)), "time": f"{hh:02d}:{mm:02d}", "enabled": bool(e.get("enabled")),
                      "first_week_only": bool(e.get("first_week_only"))})
    return clean


# E116: default slots that existed before the "seen_defaults" record was kept. A schedule file written by an older runner is treated as having been offered exactly these,
# so a slot added by a later release (s_tm_open, E115) is appended once, while a slot the person deleted from an old file is never put back.
LEGACY_DEFAULT_IDS = ("s_monday_open", "s_daily_close", "s_friday_review", "s_weekly_cal", "s_monthly")


class Scheduler:
    def __init__(self, path: str, defaults: List[Dict[str, Any]], submit: Callable[..., Any], pack_ids: List[str], enabled_default: bool = False,
                 now_fn: Callable[[], datetime] = ist_now, on_skip: Optional[Callable[[Dict[str, Any], str], Any]] = None):
        self.path, self.submit, self.pack_ids, self.now_fn = path, submit, pack_ids, now_fn
        self.on_skip = on_skip      # group 447: (slot, reason) -> told when a slot fired but could not be submitted
        self.lock = threading.Lock()
        self.state: Dict[str, Any] = {"enabled": bool(enabled_default), "entries": [dict(e) for e in defaults], "last_fired": {}, "last_skipped": None,
                                      "seen_defaults": [e["id"] for e in defaults]}
        loaded = False
        try:
            d = json.load(open(path, encoding="utf-8"))
            self.state["enabled"] = bool(d.get("enabled", self.state["enabled"]))
            self.state["entries"] = validate_entries(d.get("entries", self.state["entries"]), pack_ids)
            self.state["last_fired"] = {str(k): str(v) for k, v in (d.get("last_fired") or {}).items()}
            ls = d.get("last_skipped")
            self.state["last_skipped"] = ls if isinstance(ls, dict) else None
            seen = d.get("seen_defaults")
            self.state["seen_defaults"] = [str(x) for x in seen] if isinstance(seen, list) else list(LEGACY_DEFAULT_IDS)
            loaded = True
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 - a damaged file falls back to the defaults, it never stops the runner
            pass
        if loaded:
            self._add_new_defaults(defaults)
        self.stop = threading.Event()
        self.thread: Optional[threading.Thread] = None

    def _add_new_defaults(self, defaults: List[Dict[str, Any]]) -> None:
        """E116: a default slot this file has never been offered is appended (as the release ships it, enabled or not). A slot already offered and since deleted stays deleted."""
        seen = set(self.state["seen_defaults"])
        before = set(seen)
        have = {e["id"] for e in self.state["entries"]}
        added = []
        for e in defaults:
            if e["id"] in seen:
                continue
            seen.add(e["id"])
            if e["id"] not in have and len(self.state["entries"]) < 30:
                try:
                    self.state["entries"] += validate_entries([dict(e)], self.pack_ids)
                    added.append(e["id"])
                except ValueError:
                    pass
        self.state["seen_defaults"] = sorted(seen)
        if added or seen != before:
            try:
                self._save()
            except Exception:  # noqa: BLE001 - a read-only data dir must not stop the runner
                pass

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.path)

    def get(self) -> Dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def put(self, enabled: Optional[bool], entries: Optional[Any]) -> Dict[str, Any]:
        with self.lock:
            if entries is not None:
                self.state["entries"] = validate_entries(entries, self.pack_ids)
            if enabled is not None:
                self.state["enabled"] = bool(enabled)
            self._save()
        return self.get()

    def tick(self) -> List[str]:
        """One pass; returns the slot ids fired. Public for tests."""
        fired: List[str] = []
        with self.lock:
            if not self.state["enabled"]:
                return fired
            now = self.now_fn()
            todo = due(self.state["entries"], self.state["last_fired"], now)
            for e in todo:
                self.state["last_fired"][e["id"]] = now.astimezone(IST).strftime("%Y-%m-%d")
                fired.append(e["id"])
            if todo:
                self._save()
        for e in todo:
            try:
                self.submit("pack", e["pack"], {}, "scheduler", "schedule")
            except Exception as ex:  # noqa: BLE001 - already queued / queue full: skipped, the day is still marked, but it is no longer silent
                self._skipped(e, str(ex) or type(ex).__name__)
        return fired

    def _skipped(self, e: Dict[str, Any], reason: str) -> None:
        with self.lock:
            self.state["last_skipped"] = {"slot": e["id"], "pack": e["pack"], "time": e["time"], "reason": reason[:300],
                                          "at": self.now_fn().astimezone(IST).isoformat(timespec="seconds"), "telegram": ""}
            try:
                self._save()
            except OSError:
                pass
        if self.on_skip:
            try:
                res = self.on_skip(e, reason)
            except Exception as ex:  # noqa: BLE001
                res = f"error: {type(ex).__name__}"
            if res:
                with self.lock:
                    self.state["last_skipped"]["telegram"] = str(res)
                    try:
                        self._save()
                    except OSError:
                        pass

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return

        def loop():
            while not self.stop.wait(30):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001
                    pass

        self.thread = threading.Thread(target=loop, name="scheduler", daemon=True)
        self.thread.start()
