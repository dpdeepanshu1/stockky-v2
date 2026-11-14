"""
market_research.py - round 512: the "re-run market research" the stale-threshold alert asked for, as code.

WHY: adaptive_thresholds.py warned "Stale trading thresholds detected ... Re-run market research and update these", but nothing in the repo
could actually do that research, and the review dates were hard-coded (2026-10-04) in three files. So the warning could only ever grow older.
It also fired at EVERY restart for constants that regime_adaptive.py manages: the check ran at boot, 45 s before regime_adaptive's first
snapshot existed, so is_managed() was still False for all five.

WHAT THIS MODULE DOES (read-only about trading: it never changes a constant, a switch or an order):
  1. run_research(): reads the index history (the same 2-year daily series regime_adaptive uses), computes the multi-horizon market picture
     (tone, stress, 1d..1y returns), the recent closed-REAL-trade feedback, and the values the adaptive rules set for the five regime
     constants. Compares each with the owner's static value and the governance guard-rail band. Verdict OK / ATTENTION.
  2. On success it STAMPS a review date (IST day) in a small state file. check_threshold_staleness() / threshold_age_note() read
     last_reviewed(name, hard_coded_date) = the later of the two, so a research run is what resets the 30-day clock. A failed run stamps
     nothing: the warning stays honest.
  3. Sends ONE Telegram message with the result (manual and automatic runs alike).
  4. run_loop(): weekly automatic run (Saturday 09:00 IST, catch-up when the last good run is older than 8 days or none exists), so no
     one has to remember. The calibration runner's timetable can fire the same endpoint at the same hour; whichever runs first stamps, the
     other sees a fresh stamp and skips (scheduled triggers only; a button press always runs).

Nothing here ever raises into trading. REGIME_ADAPTIVE_MODE=off still works: the picture is computed directly and reported as "what the
adaptive rules WOULD set", with the verdict ATTENTION because the constants are then frozen at the static values.

Switches (read at call time): MARKET_RESEARCH_WEEKLY (default on), MARKET_RESEARCH_STATE_PATH, MARKET_RESEARCH_NOTIFY (default on).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

import config

logger = logging.getLogger("real-trade-market-research")

IST = timezone(timedelta(hours=5, minutes=30))

# The five regime constants research reviews and the owner's governance guard-rail band for each (kept equal to
# notification-scheduler-service/scheduler/governance_check.py REGIME_CONSTANTS; tests/test_round512_market_research.py checks the copy).
CONSTANTS = ("ENTRY_REGIME_MIN_SCORE", "ENTRY_MIN_REWARD_RISK", "CANDIDATE_MIN_CONVICTION",
             "CANDIDATE_DOWNTREND_6M_PCT", "CANDIDATE_MIN_BULLISH_TF")
GUARDRAILS: Dict[str, tuple] = {
    "ENTRY_REGIME_MIN_SCORE": (20, 60),
    "ENTRY_MIN_REWARD_RISK": (1.5, 3.0),
    "CANDIDATE_MIN_CONVICTION": (40, 75),
    "CANDIDATE_DOWNTREND_6M_PCT": (-20, -5),
    "CANDIDATE_MIN_BULLISH_TF": (2, 5),
}

WEEKLY_WEEKDAY = 5          # Saturday (Monday = 0)
WEEKLY_HOUR_IST = 9
CATCHUP_DAYS = 8            # last good run older than this -> run at the next loop pass, whatever the weekday
MIN_GAP_DAYS = 5            # a scheduled trigger skips when a good run is newer than this
FAIL_RETRY_H = 6            # after a failed run the loop waits this long before trying again
TONE_SHIFT = 0.6            # |tone| or stress at/above this = "the market regime has moved materially"

_lock = threading.Lock()
_run_lock = threading.Lock()
_cache: Dict[str, Any] = {"ts": 0.0, "doc": None}


# ── state file ─────────────────────────────────────────────────────────────────────────────────

def _state_path() -> str:
    p = (os.getenv("MARKET_RESEARCH_STATE_PATH") or "").strip()
    if p:
        return p
    return os.path.join((os.getenv("CONTROL_DIR") or "/data/control").strip() or "/data/control", "market_research.json")


def _read_state() -> Dict[str, Any]:
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001 - missing / unreadable file = no research on record
        return {}


def _write_state(doc: Dict[str, Any]) -> bool:
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("market research: state file not written (%s): %s - the review date will not survive a restart", path, e)
        return False


def _state_cached(max_age_s: float = 30.0) -> Dict[str, Any]:
    now = time.time()
    with _lock:
        if _cache["doc"] is not None and now - _cache["ts"] < max_age_s:
            return _cache["doc"]
    doc = _read_state()
    with _lock:
        _cache.update(ts=now, doc=doc)
    return doc


def _reset_cache() -> None:
    with _lock:
        _cache.update(ts=0.0, doc=None)


def latest() -> Dict[str, Any]:
    """The last research record (good or failed) plus the last good one's review date; {} fields when none exists."""
    doc = _read_state()
    return {"last": doc.get("last"), "last_ok": doc.get("last_ok"), "reviewed_date": doc.get("reviewed_date"),
            "weekly": _weekly_on(), "next_weekly": next_weekly_ist(doc)}


def last_reviewed(name: str, default_date: str) -> str:
    """The review date to show / age: the later of the hard-coded one and the last successful research run (YYYY-MM-DD)."""
    try:
        stamp = str(_state_cached().get("reviewed_date") or "")
        if stamp and name in CONSTANTS and stamp > default_date:
            datetime.strptime(stamp, "%Y-%m-%d")
            return stamp
    except Exception:  # noqa: BLE001
        pass
    return default_date


# ── the research itself ────────────────────────────────────────────────────────────────────────

def _band_state(name: str, value: Any) -> Optional[bool]:
    lo, hi = GUARDRAILS[name]
    try:
        return lo <= float(value) <= hi
    except (TypeError, ValueError):
        return None


def _fmt(v: Any) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:g}"
    except (TypeError, ValueError):
        return str(v)


def build_report(snap: Dict[str, Any], mode_live: bool, trigger: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Pure: snapshot {values, base, picture, feedback} -> the research record. `snap` is regime_adaptive's snapshot shape."""
    now = now or datetime.now(timezone.utc)
    vals = snap.get("values") or {}
    base = dict(snap.get("base") or {})
    base["ENTRY_REGIME_MIN_SCORE"] = float(config.ENTRY_REGIME_MIN_SCORE)
    pic = snap.get("picture") or {}
    fb = snap.get("feedback") or {}
    delta = int(vals.get("score_delta", 0) or 0)
    rows: List[Dict[str, Any]] = []
    for name in CONSTANTS:
        static = base.get(name)
        if name == "ENTRY_REGIME_MIN_SCORE":
            eff = max(20, min(60, int(static) + delta)) if static is not None else None      # same clamp as adaptive_regime_threshold()
            note = f"static {_fmt(static)} shifted {delta:+d} by the market tone; the live gate also follows the 90-day history percentile"
        else:
            eff = vals.get(name, static)
            note = ""
        lo, hi = GUARDRAILS[name]
        rows.append({"name": name, "static": static, "suggested": eff, "band": [lo, hi], "in_band": _band_state(name, eff),
                     "changed": (eff is not None and static is not None and float(eff) != float(static)), "note": note})
    tone, stress = float(pic.get("tone", 0.0) or 0.0), float(pic.get("stress", 0.0) or 0.0)
    out_of_band = [r["name"] for r in rows if r["in_band"] is False]
    reasons: List[str] = []
    if out_of_band:
        reasons.append("outside the governance band: " + ", ".join(out_of_band))
    if not mode_live:
        reasons.append("REGIME_ADAPTIVE_MODE=off: the constants stay frozen at the static values, update them by hand if the picture below says so")
    if abs(tone) >= TONE_SHIFT or stress >= TONE_SHIFT:
        reasons.append(f"market regime moved materially (tone {tone:+.2f}, stress {stress:.2f}): check the static values in config / the control page")
    ist = now.astimezone(IST)
    return {
        "ok": True, "trigger": trigger, "at": now.isoformat(timespec="seconds"), "at_ist": ist.strftime("%Y-%m-%d %H:%M"),
        "reviewed_date": ist.strftime("%Y-%m-%d"), "adaptive_live": bool(mode_live),
        "verdict": "ATTENTION" if reasons else "OK", "reasons": reasons,
        "picture": {"tone": tone, "stress": stress, "vol_ratio": pic.get("vol_ratio"), "returns": pic.get("returns") or {}, "sessions": pic.get("sessions")},
        "feedback": fb, "constants": rows,
    }


def _telegram_text(rep: Dict[str, Any]) -> str:
    if not rep.get("ok"):
        return (f"⚠️ Market research FAILED ({rep.get('trigger')})\n"
                f"{rep.get('error') or 'no data'}\n"
                "No review date was stamped, so the stale-threshold warning stays. Fix the index data feed and press the button again.")
    pic = rep.get("picture") or {}
    ret = pic.get("returns") or {}
    lines = [f"📊 Weekly market research report · {rep.get('at_ist')} IST ({rep.get('trigger')})",
             f"Verdict: {rep.get('verdict')} · review date stamped {rep.get('reviewed_date')}",
             f"Market: tone {pic.get('tone', 0):+.2f} · stress {pic.get('stress', 0):.2f} · 1m {_fmt(ret.get('1m'))}% · 3m {_fmt(ret.get('3m'))}% · 6m {_fmt(ret.get('6m'))}%"]
    fb = rep.get("feedback") or {}
    if fb.get("trades"):
        lines.append(f"Own results: {fb.get('trades')} closed REAL trades, win rate {_fmt(round((fb.get('win_rate') or 0) * 100))}%")
    for r in rep.get("constants") or []:
        arrow = "" if not r.get("changed") else f" -> {_fmt(r.get('suggested'))}"
        flag = " (outside band)" if r.get("in_band") is False else ""
        lines.append(f"  {r['name']}: {_fmt(r.get('static'))}{arrow}{flag}")
    for why in rep.get("reasons") or []:
        lines.append(f"Look: {why}")
    if rep.get("adaptive_live"):
        lines.append("Adaptive rules apply these shifts on their own every 15 minutes; this report changes no setting.")
    return "\n".join(lines)


def _notify(text: str) -> None:
    if (os.getenv("MARKET_RESEARCH_NOTIFY") or "true").strip().lower() in ("0", "false", "no", "off"):
        return
    try:
        from notifier import notify_sync
        notify_sync(text)
    except Exception as e:  # noqa: BLE001 - a Telegram problem never fails the research
        logger.warning("market research: Telegram message not sent: %s", e)


def _good_age_days(doc: Dict[str, Any], now: Optional[float] = None) -> Optional[float]:
    ok = doc.get("last_ok") or {}
    try:
        t = datetime.fromisoformat(str(ok.get("at"))).timestamp()
    except Exception:  # noqa: BLE001
        return None
    return ((time.time() if now is None else now) - t) / 86400.0


def run_research(session_factory: Optional[Callable[[], Any]] = None, trigger: str = "manual",
                 fetch: Optional[Callable[[], List[float]]] = None, pnls: Optional[Callable[[Any, int], List[float]]] = None) -> Dict[str, Any]:
    """One research pass. Never raises; returns the record (ok False + error when the data could not be read).
    trigger: manual (button) | weekly (this service's own loop) | runner (calibration-runner timetable). Non-manual triggers skip when a good run
    is younger than MIN_GAP_DAYS."""
    trigger = trigger if trigger in ("manual", "weekly", "runner") else "manual"
    if not _run_lock.acquire(blocking=False):
        return {"ok": False, "busy": True, "trigger": trigger, "error": "a research run is already in progress"}
    try:
        doc = _read_state()
        if trigger != "manual":
            age = _good_age_days(doc)
            if age is not None and age < MIN_GAP_DAYS:
                return {"ok": True, "skipped": True, "trigger": trigger, "reason": f"last good research {age:.1f} days ago", "last_ok": doc.get("last_ok")}
        import regime_adaptive as ra
        live = ra.mode() == "live"
        try:
            if live:
                snap = ra.refresh(session_factory, fetch=fetch, pnls=pnls)        # also freshens the snapshot the trading rules read
                if not isinstance(snap, dict) or "values" not in snap:
                    raise RuntimeError(str((snap or {}).get("error") or "adaptive snapshot not produced"))
            else:
                closes = (fetch or ra.fetch_index_closes)()
                pic = ra.market_picture(closes)
                if pic is None:
                    raise RuntimeError(f"not enough index history ({len(closes)} sessions, need {ra.MIN_SESSIONS})")
                fb, n_tr, wr = 0.0, 0, None
                if session_factory is not None:
                    db = session_factory()
                    try:
                        days, min_trades = ra.result_params()
                        fb, n_tr, wr = ra.result_feedback((pnls or ra.closed_real_pnls)(db, days), min_trades)
                    except Exception as e:  # noqa: BLE001 - results are optional evidence
                        logger.debug("market research: closed-trade read failed (market part only): %s", e)
                    finally:
                        try:
                            db.close()
                        except Exception:  # noqa: BLE001
                            pass
                base = ra._base_values()
                snap = {"values": ra.compute_values(base, pic["tone"], pic["stress"], fb, pic["returns"].get("6m")), "base": base, "picture": pic,
                        "feedback": {"trades": n_tr, "win_rate": None if wr is None else round(wr, 3)}}
            rep = build_report(snap, live, trigger)
        except Exception as e:  # noqa: BLE001
            rep = {"ok": False, "trigger": trigger, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "at_ist": datetime.now(IST).strftime("%Y-%m-%d %H:%M"), "error": str(e)[:300]}
            logger.warning("market research failed (no review date stamped): %s", e)
        doc["last"] = rep
        if rep.get("ok"):
            doc["last_ok"] = rep
            doc["reviewed_date"] = rep["reviewed_date"]
        rep["state_saved"] = _write_state(doc)
        _reset_cache()
        _notify(_telegram_text(rep))
        return rep
    except Exception as e:  # noqa: BLE001 - never into trading / the endpoint
        logger.warning("market research crashed: %s", e)
        return {"ok": False, "trigger": trigger, "error": str(e)[:300]}
    finally:
        _run_lock.release()


# ── weekly automatic run ───────────────────────────────────────────────────────────────────────

def _weekly_on() -> bool:
    return (os.getenv("MARKET_RESEARCH_WEEKLY") or "true").strip().lower() not in ("0", "false", "no", "off")


def next_weekly_ist(doc: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> str:
    """The next Saturday 09:00 IST after `now` (text, for the page)."""
    n = (now or datetime.now(timezone.utc)).astimezone(IST)
    days = (WEEKLY_WEEKDAY - n.weekday()) % 7
    t = (n + timedelta(days=days)).replace(hour=WEEKLY_HOUR_IST, minute=0, second=0, microsecond=0)
    if t <= n:
        t += timedelta(days=7)
    return t.strftime("%Y-%m-%d %H:%M")


def weekly_due(doc: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    """True when the loop should run research now: no good run yet, the last one is older than CATCHUP_DAYS, or it is Saturday after
    09:00 IST and the last good run is at least MIN_GAP_DAYS old."""
    n = (now or datetime.now(timezone.utc))
    last = doc.get("last") or {}
    if last and not last.get("ok"):
        try:      # a failed attempt is retried after FAIL_RETRY_H hours, not every hour (one failure message, not twenty-four)
            if n.timestamp() - datetime.fromisoformat(str(last.get("at"))).timestamp() < FAIL_RETRY_H * 3600.0:
                return False
        except Exception:  # noqa: BLE001
            pass
    age = _good_age_days(doc, n.timestamp())
    if age is None or age >= CATCHUP_DAYS:
        return True
    ist = n.astimezone(IST)
    return ist.weekday() == WEEKLY_WEEKDAY and ist.hour >= WEEKLY_HOUR_IST and age >= MIN_GAP_DAYS


async def run_loop(session_factory: Callable[[], Any]) -> None:
    """Background: first look 150 s after boot (after regime_adaptive's first snapshot at 45 s), then hourly. Runs research on a worker thread."""
    import asyncio
    await asyncio.sleep(150.0)
    while True:
        try:
            if _weekly_on() and weekly_due(_read_state()):
                await asyncio.to_thread(run_research, session_factory, "weekly")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("market research loop error (will retry): %s", e)
        await asyncio.sleep(3600.0)
