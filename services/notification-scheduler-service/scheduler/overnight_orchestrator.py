"""
overnight_orchestrator.py — 2026-09-11 Oracle-side overnight automation.

User ask: since this stack now runs 24/7 on Oracle instead of sleeping
Render free-tier dynos, the Render-era GitHub Actions workflows that exist
purely to wake a sleeping dyno on a timer (data-feed-midnight,
hotpicks-premarket, surprise-premarket, ipo-premarket, hot-picks-midnight)
are no longer needed — the app is always running, so it can run its own
overnight schedule directly instead of needing an external GitHub Actions
runner to poke it awake.

Two phases, run strictly sequentially, with a rest period between EVERY
step (not just between phases) so an overnight batch never fires a burst
of calls at upstream data providers all at once:

  Phase "datafeed"  (default 00:30 IST) — full Data Feed run, then
                    repair-all for anything left incomplete.
  Phase "premarket" (default 07:00 IST) — premarket feed + repair for
                    Hot Picks, Surprise, and IPO Tracker.

The enabled/disabled toggle and the two trigger times are runtime-
configurable (see /overnight/config) and persisted to a small JSON file
under STATE_DIR so they survive a container restart. Job status is
in-memory only, same tradeoff weekend_hydrator.py/symbol_master_sync.py
already make in this service — a mid-run restart loses progress but the
next scheduled/manual run simply starts fresh.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger("overnight-orchestrator")

API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "http://api-gateway:8000").rstrip("/")
IST = ZoneInfo("Asia/Kolkata")

STATE_DIR = os.getenv("SCHEDULER_STATE_DIR", "/data")
CONFIG_PATH = os.path.join(STATE_DIR, "overnight_config.json")

_DEFAULT_CONFIG = {
    "enabled": os.getenv("OVERNIGHT_ORCH_ENABLED", "true").lower() == "true",
    "datafeed_time": os.getenv("OVERNIGHT_DATAFEED_TIME", "00:30"),
    "premarket_time": os.getenv("OVERNIGHT_PREMARKET_TIME", "07:00"),
    "rest_between_steps_sec": int(os.getenv("OVERNIGHT_REST_SEC", "45")),
}

_CONFIG_LOCK = threading.Lock()


def load_config() -> dict:
    with _CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            merged = dict(_DEFAULT_CONFIG)
            merged.update({k: v for k, v in cfg.items() if k in _DEFAULT_CONFIG})
            return merged
        except FileNotFoundError:
            return dict(_DEFAULT_CONFIG)
        except Exception as e:
            logger.warning("overnight_orchestrator: config load failed (%s), using defaults", e)
            return dict(_DEFAULT_CONFIG)


def save_config(patch: dict) -> dict:
    cfg = load_config()
    cfg.update({k: v for k, v in patch.items() if k in _DEFAULT_CONFIG})
    with _CONFIG_LOCK:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(cfg, f)
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            # Best-effort persistence only — a read-only/missing STATE_DIR
            # (e.g. no volume mounted) must not break the toggle for THIS
            # process's lifetime, it just won't survive a restart.
            logger.warning(
                "overnight_orchestrator: config save failed (%s) — change applies "
                "to this run only, will revert to env defaults on restart", e,
            )
    return cfg


# ── Job status (in-memory, mirrors weekend_hydrator.py's _HYDRATE_JOB) ──────
_JOB: dict[str, Any] = {
    "status": "idle",       # idle | running | done | error
    "phase": None,          # datafeed | premarket
    "step": None,
    "message": "Idle",
    "steps_log": [],
    "started_epoch": None,
    "updated_epoch": None,
}
_JOB_LOCK = threading.Lock()
_LAST_RUN_DATE: dict[str, str] = {"datafeed": "", "premarket": ""}  # IST date string, guards one run/day


def get_status() -> dict:
    with _JOB_LOCK:
        return dict(_JOB)


def _set_job(**kw) -> None:
    with _JOB_LOCK:
        _JOB.update(kw)
        _JOB["updated_epoch"] = time.time()


def _log_step(msg: str) -> None:
    logger.info("overnight_orchestrator: %s", msg)
    with _JOB_LOCK:
        log = _JOB.get("steps_log") or []
        log.append({"t": time.time(), "msg": msg})
        _JOB["steps_log"] = log[-40:]  # bounded, this is a live progress feed not a permanent audit log
        _JOB["message"] = msg
        _JOB["updated_epoch"] = time.time()


def _post(path: str, timeout: float = 60.0, params: Optional[dict] = None) -> dict:
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{API_GATEWAY_URL}{path}", params=params)
            try:
                return r.json()
            except Exception:
                return {"ok": r.status_code < 400, "http_status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _get(path: str, timeout: float = 30.0) -> dict:
    try:
        with httpx.Client(timeout=timeout) as client:
            r = client.get(f"{API_GATEWAY_URL}{path}")
            try:
                return r.json()
            except Exception:
                return {"ok": r.status_code < 400, "http_status": r.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def _poll_until_done(
    status_path: str,
    done_values=("done", "error", "stopped", "skipped_fresh", "idle"),
    max_polls: int = 160,
    interval_sec: float = 15.0,
    label: str = "",
) -> dict:
    """Poll a background-job status endpoint until it reports a terminal
    state, or give up after max_polls (this is a soft timeout, not a
    failure — the job may still be running server-side, it just means this
    orchestrator run stopped watching it so it can move on to the next
    step instead of hanging overnight on one stuck poll)."""
    last = {}
    for i in range(max_polls):
        time.sleep(interval_sec)
        last = _get(status_path)
        status = str(last.get("status", "")).lower()
        if status in done_values:
            return last
        if i % 8 == 0:
            _log_step(f"{label}: still running ({status or 'unknown'}), poll {i+1}/{max_polls}")
    _log_step(f"{label}: gave up watching after {max_polls} polls — continuing (server-side job may still finish)")
    return last


def _rest(seconds: float, why: str) -> None:
    _log_step(f"resting {seconds:.0f}s before next step ({why}) — avoids rate-limit bursts")
    time.sleep(seconds)


# ── Phase 1: Data Feed ───────────────────────────────────────────────────────
def _run_datafeed_phase(rest_sec: float) -> dict:
    _log_step("datafeed: starting full feed run")
    r1 = _post("/data-feed/run?force=true", timeout=90.0)
    if not r1.get("ok", True) and "error" in r1:
        _log_step(f"datafeed: run trigger failed ({r1.get('error')}) — continuing to repair-all anyway")
    _poll_until_done("/data-feed/status", label="datafeed run")

    _rest(rest_sec, "between feed run and repair-all")

    _log_step("datafeed: starting repair-all")
    r2 = _post("/data-feed/repair-all", timeout=60.0, params={"limit": 5000})
    if not r2.get("ok", True) and "error" in r2:
        _log_step(f"datafeed: repair-all trigger failed ({r2.get('error')})")
    final = _poll_until_done("/data-feed/repair-all/status", label="datafeed repair-all")

    _log_step("datafeed: phase complete")
    return {"run": r1, "repair_all_final": final}


# ── Phase 2: premarket feed + repair for hot_picks / surprise / ipo ─────────
def _repair_loop(post_path: str, label: str, rest_sec: float, max_iterations: int = 5) -> list:
    """Several of the repair endpoints are batch-limited (repair up to N
    rows per call), not run-to-completion — so this loops it a bounded
    number of times, resting between each call, until a call reports it
    repaired nothing (nothing left to do) or the iteration cap is hit."""
    results = []
    for i in range(max_iterations):
        res = _post(post_path, timeout=60.0)
        results.append(res)
        repaired = res.get("repaired") or res.get("repaired_count") or res.get("ok_count") or 0
        _log_step(f"{label}: repair pass {i+1}/{max_iterations} — repaired={repaired}")
        if not repaired:
            break
        if i < max_iterations - 1:
            _rest(rest_sec, f"between {label} repair passes")
    return results


def _run_premarket_phase(rest_sec: float) -> dict:
    out: dict = {}

    _log_step("premarket: hot_picks premarket feed")
    _post("/stockky-hot/premarket", timeout=60.0)
    out["hot_picks_premarket"] = _poll_until_done("/stockky-hot/premarket/status", label="hot_picks premarket")
    _rest(rest_sec, "before hot_picks repair")
    out["hot_picks_repair"] = _repair_loop("/stockky-hot/repair-batch", "hot_picks", rest_sec)
    _rest(rest_sec, "before surprise premarket")

    _log_step("premarket: surprise premarket baselines")
    out["surprise_premarket"] = _post("/surprise/premarket", timeout=600.0)
    _rest(rest_sec, "before surprise repair")
    out["surprise_repair"] = _repair_loop("/api/surprise/repair-batch", "surprise", rest_sec)
    _rest(rest_sec, "before ipo scan")

    _log_step("premarket: ipo premarket scan")
    _post("/surprise/ipo/scan?background=true&force=true", timeout=60.0)
    out["ipo_scan"] = _poll_until_done("/surprise/ipo/status", label="ipo premarket scan")
    _rest(rest_sec, "before ipo repair")
    out["ipo_repair"] = _repair_loop("/ipo/repair-batch", "ipo", rest_sec)

    _log_step("premarket: phase complete")
    return out


def _run_phase(phase: str) -> None:
    cfg = load_config()
    rest_sec = float(cfg.get("rest_between_steps_sec", 45))
    _set_job(status="running", phase=phase, step=None, started_epoch=time.time(), steps_log=[])
    try:
        if phase == "datafeed":
            result = _run_datafeed_phase(rest_sec)
        elif phase == "premarket":
            result = _run_premarket_phase(rest_sec)
        else:
            raise ValueError(f"unknown phase {phase}")
        _set_job(status="done", message=f"{phase} phase complete")
        _LAST_RUN_DATE[phase] = datetime.now(IST).strftime("%Y-%m-%d")
        logger.info("overnight_orchestrator: %s phase finished: %s", phase, result)
    except Exception as e:
        logger.exception("overnight_orchestrator: %s phase failed", phase)
        _set_job(status="error", message=str(e)[:300])


def start_phase_background(phase: str) -> dict:
    current = get_status()
    if current.get("status") == "running":
        return {"ok": True, "already_running": True, "job": current}
    thread = threading.Thread(target=_run_phase, args=(phase,), daemon=True)
    thread.start()
    return {"ok": True, "started": True, "phase": phase, "job": get_status()}


# ── Scheduling loop (started from scheduler/main.py's startup event, same
# pattern as the existing Neon keepalive loop) ──────────────────────────────
async def scheduling_loop() -> None:
    import asyncio
    await asyncio.sleep(30)
    while True:
        try:
            cfg = load_config()
            if cfg.get("enabled"):
                now = datetime.now(IST)
                today = now.strftime("%Y-%m-%d")
                hhmm = now.strftime("%H:%M")
                for phase, trigger in (("datafeed", cfg.get("datafeed_time")), ("premarket", cfg.get("premarket_time"))):
                    if not trigger:
                        continue
                    if _LAST_RUN_DATE.get(phase) == today:
                        continue
                    if hhmm >= trigger and get_status().get("status") != "running":
                        logger.info("overnight_orchestrator: scheduled trigger for %s at IST %s", phase, hhmm)
                        start_phase_background(phase)
                        # Only start one phase per loop tick — datafeed and
                        # premarket never run concurrently even if both
                        # trigger times were somehow reached in the same
                        # check (keeps to the "run one thing at a time,
                        # rest between" design all the way up).
                        break
        except Exception as e:
            logger.warning("overnight_orchestrator: scheduling loop error: %s", e)
        await asyncio.sleep(60)
