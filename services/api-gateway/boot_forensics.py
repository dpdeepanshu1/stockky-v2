"""
boot_forensics.py — session 72 (open-issue #2: "why did api-gateway / real-trade-service
restart twice during the session-68 test window with no crash or SIGTERM in the logs?").

A cgroup OOM-kill is a SIGKILL: Python never gets to log anything, which is exactly
"restart with no crash/SIGTERM evidence". This module makes the cause readable from the
NEXT boot's log, without needing `docker inspect`:

  * every boot writes /tmp/stockky_boot_state.json (survives a container RESTART,
    is absent after a container RE-CREATE);
  * a daemon heartbeat refreshes it every 15s with the cgroup memory usage/limit and
    logs a MEMORY PRESSURE warning when usage crosses 85% of the limit;
  * SIGTERM/SIGINT are recorded (then chained to the previous handler, so uvicorn's
    graceful shutdown is unchanged); the shutdown hook marks the exit clean;
  * the next boot logs ONE line classifying the previous exit:
      FRESH_CONTAINER | RESTART_AFTER_CLEAN_SHUTDOWN |
      SIGTERM_BUT_NOT_CLEAN (docker stop grace period expired -> SIGKILL) |
      DIED_WITHOUT_CLEAN_SHUTDOWN (SIGKILL/OOM-kill/crash/host reboot) — with the last
      heartbeat age and last-seen memory so an OOM is visible as "used ~= limit".

Identical copy lives in api-gateway/, real-trade-service/ and position-stocks-service/
(each service builds from its own directory, so it cannot import a shared module).
Never raises into the caller.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
import uuid

logger = logging.getLogger("boot-forensics")

_STATE_PATH = os.getenv("BOOT_FORENSICS_PATH", "/tmp/stockky_boot_state.json")
_HEARTBEAT_S = float(os.getenv("BOOT_FORENSICS_HEARTBEAT_S", "15"))
_MEM_WARN_PCT = float(os.getenv("BOOT_FORENSICS_MEM_WARN_PCT", "85"))

_state: dict = {}
_lock = threading.Lock()
_started = False


def _read_int(path: str):
    try:
        with open(path) as f:
            v = f.read().strip()
        return None if v == "max" else int(v)
    except Exception:
        return None


def memory_snapshot() -> dict:
    used = _read_int("/sys/fs/cgroup/memory.current")
    limit = _read_int("/sys/fs/cgroup/memory.max")
    if used is None:  # cgroup v1
        used = _read_int("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit = _read_int("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if limit is not None and limit > (1 << 60):
            limit = None
    rss = None
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
    except Exception:
        pass
    return {"used": used, "limit": limit, "rss": rss}


def _mb(x):
    return None if x is None else round(x / 1048576, 1)


def _write() -> None:
    try:
        tmp = _STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(_state, f)
        os.replace(tmp, _STATE_PATH)
    except Exception:
        pass


def _heartbeat_loop(service: str) -> None:
    last_warn = 0.0
    while True:
        time.sleep(_HEARTBEAT_S)
        try:
            mem = memory_snapshot()
            with _lock:
                _state["last_heartbeat"] = time.time()
                _state["mem"] = mem
                _write()
            if mem["used"] and mem["limit"]:
                pct = mem["used"] / mem["limit"] * 100.0
                if pct >= _MEM_WARN_PCT and time.time() - last_warn > 60:
                    last_warn = time.time()
                    logger.warning("MEMORY PRESSURE [%s]: cgroup %.0f/%.0f MB (%.0f%% of limit) — an OOM-kill "
                                   "(silent SIGKILL) is imminent above 100%%", service, _mb(mem["used"]), _mb(mem["limit"]), pct)
        except Exception:
            pass


def _classify(prev, now: float) -> tuple[str, str]:
    if prev is None:
        return "FRESH_CONTAINER", ("first start, or the container was re-created (e.g. `docker compose up` after an "
                                   "image/config change, or a redeploy)")
    if prev.get("corrupt"):
        return "UNKNOWN", f"previous boot state unreadable: {prev['corrupt']}"
    hb_age = now - float(prev.get("last_heartbeat") or prev.get("started_at") or now)
    up = float(prev.get("last_heartbeat") or now) - float(prev.get("started_at") or now)
    mem = prev.get("mem") or {}
    memtxt = (f"last memory {_mb(mem.get('used'))}/{_mb(mem.get('limit'))} MB" if mem.get("used") else "no memory sample")
    if prev.get("clean_shutdown"):
        return "RESTART_AFTER_CLEAN_SHUTDOWN", (f"previous process exited gracefully (signal={prev.get('signal')}), ran ~{up:.0f}s; "
                                                 "same container restarted: `docker restart`/`compose restart`, Docker daemon/host restart, "
                                                 "or restart policy after a clean exit")
    if prev.get("signal"):
        return "SIGTERM_BUT_NOT_CLEAN", (f"received {prev['signal']} but never finished shutting down (docker stop grace period "
                                          f"expired -> SIGKILL); ran ~{up:.0f}s; {memtxt}")
    return "DIED_WITHOUT_CLEAN_SHUTDOWN", (f"no signal seen and no graceful shutdown: SIGKILL — the cgroup OOM-killer when memory "
                                            f"is near the limit — or a hard crash/host reboot. Last heartbeat {hb_age:.0f}s before this boot; "
                                            f"ran ~{up:.0f}s; {memtxt}")


def _install_signal_logging(service: str) -> None:
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev_handler = signal.getsignal(sig)

            def _handler(signum, frame, _prev=prev_handler, _name=sig.name):
                try:
                    with _lock:
                        _state["signal"] = _name
                        _write()
                    logger.warning("BOOT FORENSICS [%s]: received %s — graceful shutdown requested (uptime %.0fs)",
                                   service, _name, time.time() - _state.get("started_at", time.time()))
                except Exception:
                    pass
                if callable(_prev):
                    _prev(signum, frame)
                elif _prev == signal.SIG_DFL:
                    signal.signal(signum, signal.SIG_DFL)
                    os.kill(os.getpid(), signum)

            signal.signal(sig, _handler)
        except Exception:
            pass  # not the main thread / unsupported — forensics is best-effort


def record_boot(service: str) -> dict:
    global _started
    now = time.time()
    prev = None
    try:
        with open(_STATE_PATH) as f:
            prev = json.load(f)
    except FileNotFoundError:
        prev = None
    except Exception as e:
        prev = {"corrupt": str(e)}
    try:
        cause, detail = _classify(prev, now)
    except Exception as e:
        # Valid JSON but the wrong shape/types (list, str, non-numeric timestamps...).
        # Without this the exception escapes BEFORE _write() below, so the bad file is
        # never replaced and every later boot raises too: forensics stays dead for good
        # and the heartbeat thread never starts. Contract: never raise into the caller.
        cause, detail = "UNKNOWN", f"previous boot state unusable ({type(e).__name__}: {e})"
    mem = memory_snapshot()
    with _lock:
        _state.clear()
        _state.update({"service": service, "boot_id": uuid.uuid4().hex[:8], "pid": os.getpid(), "started_at": now,
                       "last_heartbeat": now, "clean_shutdown": False, "signal": None, "mem": mem})
        _write()
    lvl = logging.ERROR if cause in ("DIED_WITHOUT_CLEAN_SHUTDOWN", "SIGTERM_BUT_NOT_CLEAN") else logging.WARNING if cause != "FRESH_CONTAINER" else logging.INFO
    logger.log(lvl, "BOOT FORENSICS [%s] pid=%s boot_id=%s cause=%s — %s | now: cgroup %s/%s MB", service, os.getpid(),
               _state["boot_id"], cause, detail, _mb(mem["used"]), _mb(mem["limit"]))
    if not _started:
        _started = True
        _install_signal_logging(service)
        threading.Thread(target=_heartbeat_loop, args=(service,), name="boot-forensics-heartbeat", daemon=True).start()
    return {"cause": cause, "detail": detail}


def mark_clean_shutdown(reason: str = "shutdown-event") -> None:
    try:
        with _lock:
            _state["clean_shutdown"] = True
            _state["shutdown_reason"] = reason
            _state["last_heartbeat"] = time.time()
            _write()
    except Exception:
        pass
