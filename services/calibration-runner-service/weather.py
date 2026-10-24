"""weather.py - Market Weather jobs in SHADOW (Part 1 step 4).

Four timed stages, India time, Monday to Friday:
  morning 08:00  daily index + VIX history up to yesterday -> state, Telegram brief
  refine  08:50  same, re-read, with the live VIX quote
  lock    09:14  adds the opening gap (pre-open price against yesterday's close)
  tick    every 5 minutes 09:20-15:25: the session so far (move, fall from high, range); the state may only go UP during the day

SHADOW means: every stage computes, logs (<data>/weather/log.jsonl) and may send a message, and that is all. This module has NO way to send a dial
to a trading service: the only call it can make to /control/overlay is a stamp-only body (entries {}), and only when WEATHER_STAMP is on, so that
trades carry the state they ran under. Applying dials is step 6 and does not exist here (see _stamp_body and test_weather).

Data: market-data /history/^NSEI and /history/^INDIAVIX (daily) and /quote/^NSEI, /quote/INDIAVIX. A quote counts as TODAY's only if its previous
close equals the last completed daily close (a holiday or a stale feed fails that test), otherwise the stage records "no live quote" and does not
classify from it. A stage that cannot get its data changes nothing and says so; the last good state stays.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from regime import STATES, classifier, data, features, profiles

IST = timezone(timedelta(hours=5, minutes=30))
STAGES = ("morning", "refine", "lock", "tick")
TIMES = {"morning": (8, 0), "refine": (8, 50), "lock": (9, 14)}
TICK_FIRST, TICK_LAST = (9, 20), (15, 25)      # ticks run at minutes divisible by 5 inside this window
WINDOWS = {"morning": 10, "refine": 10, "lock": 5}   # minutes: a slot missed by more than this is skipped, never caught up (lock must end before the 09:20 tick)
LOG_MAX_LINES, LOG_KEEP_LINES = 2000, 1000
SHADOW_NOTE = "SHADOW: nothing was changed."


def ist(now: datetime) -> datetime:
    return now.astimezone(IST)


# ------------------------------------------------------------------------------------------------------ timing
def due_stages(now: datetime, last_fired: Dict[str, str]) -> List[str]:
    """Stages whose time has come. last_fired = {"morning": "2026-10-23", ..., "tick": "2026-10-23 09:20"}. Pure."""
    n = ist(now)
    if n.weekday() > 4:
        return []
    day, hm = n.strftime("%Y-%m-%d"), n.hour * 60 + n.minute
    out = []
    for st, (h, m) in TIMES.items():
        if 0 <= hm - (h * 60 + m) < WINDOWS[st] and last_fired.get(st) != day:
            out.append(st)
    if TICK_FIRST[0] * 60 + TICK_FIRST[1] <= hm <= TICK_LAST[0] * 60 + TICK_LAST[1] and n.minute % 5 == 0:
        slot = n.strftime("%Y-%m-%d %H:%M")
        if last_fired.get("tick") != slot:
            out.append("tick")
    return out


# ------------------------------------------------------------------------------------------------------ plumbing
def http_get_json(url: str, timeout: float = 20.0, tries: int = 2) -> Any:
    last: Optional[Exception] = None
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "stockky-weather/1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed internal URL from the environment
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (k + 1))
    raise last if last else RuntimeError("fetch failed")


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".w")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _num(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x > 0 else None


def quote_is_today(quote: Any, last_close: Optional[float], tol: float = 0.0005) -> bool:
    """True when this index quote belongs to the CURRENT session: its previous close equals the last completed daily close.
    On a holiday or with a frozen feed the quote's previous close is one session older and this is False."""
    if not isinstance(quote, dict) or not last_close:
        return False
    pc, px = _num(quote.get("previous_close")), _num(quote.get("price") or quote.get("cmp"))
    return bool(pc and px and abs(pc / last_close - 1.0) <= tol)


def _stamp_body(state: str, until: str) -> Dict[str, Any]:
    """The ONLY overlay body this module can build: stamp-only. entries is always empty; there is no code path that fills it."""
    return {"entries": {}, "until": until, "state": state, "version": profiles.VERSION, "note": "market weather stamp (shadow)"}


# ------------------------------------------------------------------------------------------------------ engine
class Weather:
    def __init__(self, directory: str, md_url: str, fetch: Callable[[str], Any] = http_get_json, notify: Optional[Callable[[str, str, str], Any]] = None,
                 stamp: Optional[Callable[[str, Dict[str, Any]], Any]] = None, enabled: bool = False, stamp_on: bool = False, notify_on: bool = True,
                 apply: Optional[Callable[[str, datetime], Dict[str, Any]]] = None, apply_on: bool = False,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(IST), period: str = "1y"):
        self.dir, self.md, self.fetch, self.notify, self.stamp = directory, md_url.rstrip("/"), fetch, notify, stamp
        self.now_fn, self.period = now_fn, period
        self.apply = apply          # injected (weather_apply.Applier): this module can call it but cannot build a dial itself
        self.lock = threading.RLock()
        self.path = os.path.join(directory, "state.json")
        self.log_path = os.path.join(directory, "log.jsonl")
        self.state: Dict[str, Any] = {"enabled": bool(enabled), "stamp": bool(stamp_on), "notify": bool(notify_on), "apply": bool(apply_on), "last_apply": None, "last_fired": {}, "day": None, "current": None}
        self._daily: Dict[str, Any] = {}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            for k in ("enabled", "stamp", "notify", "apply"):
                if k in d:
                    self.state[k] = bool(d[k])
            self.state["last_apply"] = d.get("last_apply")
            self.state["last_fired"] = {str(k): str(v) for k, v in (d.get("last_fired") or {}).items()}
            self.state["day"], self.state["current"] = d.get("day"), d.get("current")
        except (FileNotFoundError, ValueError):
            pass
        self.thread: Optional[threading.Thread] = None
        self.stop = threading.Event()

    # -- settings / status
    def _save(self) -> None:
        _atomic_write(self.path, json.dumps(self.state, indent=1))

    def configure(self, enabled: Optional[bool] = None, stamp: Optional[bool] = None, notify: Optional[bool] = None, apply: Optional[bool] = None) -> Dict[str, Any]:
        with self.lock:
            for k, v in (("enabled", enabled), ("stamp", stamp), ("notify", notify), ("apply", apply)):
                if v is not None:
                    self.state[k] = bool(v)
            self._save()
        return self.status()

    def status(self, log_lines: int = 20) -> Dict[str, Any]:
        with self.lock:
            return {"mode": "live" if (self.state["apply"] and self.apply is not None) else "shadow", "enabled": self.state["enabled"], "stamp": self.state["stamp"], "notify": self.state["notify"],
                    "apply": self.state["apply"] and self.apply is not None, "last_apply": self.state.get("last_apply"), "day": self.state["day"],
                    "current": self.state["current"], "version": profiles.VERSION, "last_fired": dict(self.state["last_fired"]),
                    "profile": profiles.table(), "recent": self.read_log(log_lines), "gate": self._gate_status()}

    def _gate_status(self) -> Optional[Dict[str, Any]]:
        g = getattr(self.apply, "gate_status", None)
        try:
            return g() if g else None
        except Exception:  # noqa: BLE001
            return None

    def read_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            with open(self.log_path, encoding="utf-8") as f:
                lines = f.read().splitlines()[-max(1, limit):]
        except FileNotFoundError:
            return []
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    def _log(self, row: Dict[str, Any]) -> None:
        os.makedirs(self.dir, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        try:
            with open(self.log_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            if len(lines) > LOG_MAX_LINES:
                _atomic_write(self.log_path, "\n".join(lines[-LOG_KEEP_LINES:]) + "\n")
        except OSError:
            pass

    # -- data
    def _url(self, path: str) -> str:
        return self.md + path

    def _history(self, symbol: str, refresh: bool) -> List[Dict[str, Any]]:
        key = symbol + "|" + (self.state["day"] or "")
        if not refresh and key in self._daily:
            return self._daily[key]
        doc = self.fetch(self._url(f"/history/{urllib.parse.quote(symbol, safe='^')}?period={self.period}&interval=1d"))
        bars = data.clean_daily(doc)
        self._daily = {k: v for k, v in self._daily.items() if k.endswith("|" + (self.state["day"] or ""))}
        self._daily[key] = bars
        return bars

    def _quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        try:
            q = self.fetch(self._url(f"/quote/{urllib.parse.quote(symbol, safe='^')}"))
        except Exception:  # noqa: BLE001
            return None
        return q if isinstance(q, dict) else None

    def _daily_features(self, today: str, refresh: bool, live_vix: bool) -> Dict[str, Any]:
        nifty = data.upto(self._history(data.NIFTY, refresh), today)
        try:
            vix = data.upto(self._history(data.VIX, refresh), today)
        except Exception:  # noqa: BLE001 - VIX history is optional: the classifier re-weights what exists
            vix = []
        f = features.daily_features(nifty, vix or None)
        if live_vix:
            q = self._quote("INDIAVIX")
            lvl = _num((q or {}).get("price") or (q or {}).get("cmp"))
            if lvl and 3 <= lvl <= 120:
                f["vix"] = lvl
                pc = _num((q or {}).get("previous_close"))
                if pc and 3 <= pc <= 120:
                    f["vix_chg_1d_pct"] = (lvl / pc - 1.0) * 100.0
                hist = [b["c"] for b in vix[-250:]]
                if len(hist) >= 60:
                    f["vix_pctile"] = features.percentile_rank(hist, lvl)
        f["_last_close"] = nifty[-1]["c"] if nifty else None
        return f

    # -- one stage
    def run_stage(self, stage: str, now: Optional[datetime] = None, force: bool = False) -> Dict[str, Any]:
        """Run one stage and return its log row. Never raises: a failure is a row with ok=False and the last good state stays."""
        if stage not in STAGES:
            raise ValueError(f"unknown stage {stage!r}")
        with self.lock:
            now = ist(now or self.now_fn())
            today = now.strftime("%Y-%m-%d")
            if self.state["day"] != today:
                self.state["day"], self.state["current"] = today, None
            prev = self.state["current"]
            row: Dict[str, Any] = {"at": now.isoformat(timespec="seconds"), "day": today, "stage": stage, "ok": False, "shadow": not (self.apply is not None and self.state["apply"]),
                                   "classifier": classifier.CLASSIFIER_VERSION, "profile": profiles.VERSION}
            try:
                if stage in ("morning", "refine") or not (prev and prev.get("features")):
                    f = self._daily_features(today, refresh=stage in ("morning", "refine"), live_vix=stage != "morning")
                else:
                    f = dict(prev["features"])
                    f["_last_close"] = prev.get("last_close")
                last_close = f.pop("_last_close", None)
                if f.get("atr14_pct") is None and f.get("vix") is None:
                    raise RuntimeError("no index or VIX history")
                gap = (prev or {}).get("gap_pct") if stage in ("lock", "tick") else None
                note = ""
                if stage in ("lock", "tick"):
                    q = self._quote(data.NIFTY)
                    if not quote_is_today(q, last_close):
                        note = "no live quote for today (holiday or stale feed): state not updated from it"
                    else:
                        px, pc = _num(q.get("price") or q.get("cmp")), _num(q.get("previous_close"))
                        if stage == "lock":
                            gap = features.opening_features(pc, px, f.get("atr14_pct"))["gap_pct"]
                        if gap is not None:
                            f.update(features.opening_features(pc, pc * (1 + gap / 100.0), f.get("atr14_pct")))
                        if stage == "tick":
                            hi, lo = _num(q.get("day_high")) or px, _num(q.get("day_low")) or px
                            f.update(features.intraday_features(pc, [{"h": max(hi, px), "l": min(lo, px), "c": px}], f.get("atr14_pct")))
                if note and stage == "tick" and prev:
                    res = {"state": prev["state"], "score": prev.get("score"), "data_ok": prev.get("data_ok"), "reasons": prev.get("reasons", []),
                           "floor_reasons": prev.get("floor_reasons", []), "held": True}
                else:
                    res = classifier.classify(f, prev_state=(prev or {}).get("state"), ratchet=(stage == "tick"))
                before = (prev or {}).get("state")
                cur = {"state": res["state"], "score": res["score"], "data_ok": res["data_ok"], "reasons": res["reasons"], "floor_reasons": res["floor_reasons"],
                       "gap_pct": gap, "features": {k: v for k, v in f.items() if v is not None}, "last_close": last_close, "stage": stage, "at": row["at"]}
                self.state["current"] = cur
                row.update(ok=True, state=res["state"], score=res["score"], data_ok=res["data_ok"], reasons=res["reasons"], floor_reasons=res["floor_reasons"],
                           gap_pct=gap, previous_state=before, changed=(before is not None and before != res["state"]), note=note)
                row["live_would"] = {k: v for k, v in profiles.PROFILE[res["state"]].items()}
            except Exception as e:  # noqa: BLE001 - never let a data problem stop the loop; the last good state stays
                row["error"] = f"{type(e).__name__}: {e}"[:300]
            self.state["last_fired"]["tick" if stage == "tick" else stage] = now.strftime("%Y-%m-%d %H:%M") if stage == "tick" else today
            self._log(row)
            self._save()
        self._after(row, force)
        return row

    # -- messages and stamps (outside the lock: they do network)
    def _after(self, row: Dict[str, Any], force: bool = False) -> None:
        stage = row["stage"]
        if self.notify and self.state["notify"] and (force or self._worth_telling(row)):
            try:
                title, text, urg = self.message(row)
                if self.apply is not None and self.state["apply"]:
                    text = text.replace(SHADOW_NOTE, "LIVE apply is on: dials are sent only if the evidence gate and the service allow it (separate message).")
                self.notify(title, text, urg)
            except Exception:  # noqa: BLE001
                pass
        applied_svcs: set = set()
        if self.apply is not None and self.state["apply"] and row.get("ok") and stage in ("lock", "tick") and (stage == "lock" or row.get("changed")):
            try:
                res = self.apply(row["state"], datetime.fromisoformat(row["at"]))
            except Exception as e:  # noqa: BLE001 - an apply failure is never allowed to stop the loop; no dial was sent
                res = {"applied": False, "state": row["state"], "why": f"not applied: {type(e).__name__}", "services": {}}
            applied_svcs = {s for s, r in (res.get("services") or {}).items() if r.get("ok")}
            with self.lock:
                self.state["last_apply"] = {"at": row["at"], "stage": stage, **res}
                self._save()
            if self.notify and self.state["notify"]:
                try:
                    lines = [f"{row['at'][11:16]} IST {stage}: {row['state'].upper()} - " + (res.get("why") or "")]
                    for svc, r in (res.get("services") or {}).items():
                        lines.append(f"{svc}: " + (", ".join(f"{k}={v}" for k, v in (r.get("entries") or {}).items()) or "no dial tighter than saved") if r.get("ok") else f"{svc}: {r.get('why', 'not sent')}")
                    self.notify("Market Weather: dials " + ("applied" if res.get("applied") else "NOT applied"), "\n".join(lines) + "\nTighten-only, ends 15:30 IST.", "high" if res.get("applied") else "normal")
                except Exception:  # noqa: BLE001
                    pass
        if self.stamp and self.state["stamp"] and row.get("ok") and stage in ("lock", "tick") and (stage == "lock" or row.get("changed")):
            until = profiles.expiry(datetime.fromisoformat(row["at"]))
            if until is not None:
                for svc in profiles.SERVICES:
                    if svc in applied_svcs:
                        continue                      # the dial overlay already carries the stamp
                    try:
                        self.stamp(svc, _stamp_body(row["state"], until.isoformat(timespec="seconds")))
                    except Exception:  # noqa: BLE001 - a service that is down or has the mode off only misses its stamp
                        pass

    @staticmethod
    def _worth_telling(row: Dict[str, Any]) -> bool:
        if not row.get("ok"):
            return row["stage"] in ("morning", "lock")          # a failed morning / lock is worth knowing; a failed tick is not
        return row["stage"] == "morning" or bool(row.get("changed"))

    @staticmethod
    def message(row: Dict[str, Any]) -> tuple:
        stage, hhmm = row["stage"], row["at"][11:16]
        if not row.get("ok"):
            return f"Market Weather: {stage} failed", f"{hhmm} IST {stage}: no state ({row.get('error', '?')}). The last good state stays. {SHADOW_NOTE}", "normal"
        lines = [f"{hhmm} IST {stage}: {row['state'].upper()}" + (f" (was {row['previous_state']})" if row.get("changed") else "") + (f", score {row['score']}" if row.get("score") is not None else "")]
        if row.get("reasons"):
            lines.append("Because: " + ", ".join(row["reasons"]))
        if row.get("floor_reasons"):
            lines.append("Floor: " + "; ".join(row["floor_reasons"]))
        if row.get("gap_pct") is not None:
            lines.append(f"Gap at the open: {row['gap_pct']:+.2f}%")
        if not row.get("data_ok"):
            lines.append("Too little data for a full score: treated as normal.")
        if row.get("note"):
            lines.append(row["note"])
        w = row.get("live_would") or {}
        lines.append("Live would: " + (", ".join(f"{k}={v}" for k, v in w.items()) if w else "change nothing."))
        lines.append(SHADOW_NOTE)
        urgent = row["state"] in ("stressed", "panic") and row.get("changed")
        return f"Market Weather: {row['state']}", "\n".join(lines), "high" if urgent else "normal"

    # -- loop
    def tick_once(self, now: Optional[datetime] = None) -> List[str]:
        """One scheduler pass; returns the stages run. Public for tests."""
        with self.lock:
            if not self.state["enabled"]:
                return []
            now = now or self.now_fn()
            todo = due_stages(now, self.state["last_fired"])
        for st in todo:
            self.run_stage(st, now)
        return todo

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return

        def loop() -> None:
            while not self.stop.wait(20):
                try:
                    self.tick_once()
                except Exception:  # noqa: BLE001
                    pass

        self.thread = threading.Thread(target=loop, name="weather", daemon=True)
        self.thread.start()
