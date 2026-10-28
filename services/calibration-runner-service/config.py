"""calibration-runner-service settings (group 442). Everything comes from the environment; nothing here changes a trading setting."""
from __future__ import annotations

import os
from pathlib import Path


def _s(k: str, d: str = "") -> str:
    return (os.environ.get(k) or d).strip()


def _i(k: str, d: int) -> int:
    try:
        return int(_s(k, str(d)))
    except ValueError:
        return d


def _b(k: str, d: bool) -> bool:
    v = _s(k, "1" if d else "0").lower()
    return v in ("1", "true", "yes", "on")


SESSION_SECRET = _s("SESSION_SECRET")
DATA_DIR = Path(_s("RUNNER_DATA_DIR", "/data/runner"))
SCRIPTS_DIR = Path(_s("RUNNER_SCRIPTS_DIR", "/app/scripts"))
BAR_DIR = Path(_s("RUNNER_BAR_DIR", "/data/bars"))
PYTHON = _s("PYTHON_BIN", "python3")

REAL_TRADE_URL = _s("REAL_TRADE_URL", "http://real-trade-service:8005").rstrip("/")
POSITION_STOCKS_URL = _s("POSITION_STOCKS_URL", "http://position-stocks-service:8006").rstrip("/")
GATEWAY_URL = _s("API_GATEWAY_URL", "http://api-gateway:8000").rstrip("/")
MARKET_DATA_URL = _s("MARKET_DATA_URL", "http://market-data-service:8001").rstrip("/")
ANALYSIS_URL = _s("ANALYSIS_URL", "http://analysis-intelligence-service:8000").rstrip("/")
DECISION_URL = _s("DECISION_URL", "http://decision-prediction-service:8000").rstrip("/")
NOTIFICATION_URL = _s("NOTIFICATION_URL", "http://notification-scheduler-service:8000").rstrip("/")
KRONOS_URL = _s("KRONOS_URL", "http://kronos-forecast-service:8010").rstrip("/")
TIMESFM_URL = _s("TIMESFM_URL", "http://timesfm-forecast-service:8011").rstrip("/")

# E235: several tests at once. Two runs never overlap when one writes what the other reads or writes (lock keys come from each job's own
# steps, jobs.derive_locks); at most RUNNER_MAX_HEAVY heavy replays / downloads run together and a second one waits while memory is short.
# RUNNER_WORKERS=1 restores one-at-a-time.
WORKERS = max(1, min(8, _i("RUNNER_WORKERS", 4)))
MAX_HEAVY = max(1, min(WORKERS, _i("RUNNER_MAX_HEAVY", 2)))
MIN_FREE_MB = max(0, _i("RUNNER_MIN_FREE_MB", 1500))
# E235: parallel pool processes INSIDE one heavy replay (the "Parallel workers" option of a test; was 2)
DEFAULT_POOL_JOBS = max(1, min(8, _i("RUNNER_POOL_JOBS", 3)))
NICE = _i("RUNNER_NICE", 10)
MAX_OUTPUT_BYTES = _i("RUNNER_MAX_OUTPUT_BYTES", 5 * 1024 * 1024)
KEEP_RUNS = _i("RUNNER_KEEP_RUNS", 120)
KEEP_DAYS = _i("RUNNER_KEEP_DAYS", 45)
# heavy jobs (downloads, replays, model forecasts) are refused in these IST windows unless the caller confirms
GUARD_MARKET_HOURS = _b("RUNNER_GUARD_MARKET_HOURS", True)
SCHEDULER_ENABLED = _b("RUNNER_SCHEDULER_ENABLED", True)    # E126: on by default (a timetable saved earlier with enabled=false still wins; switch it on in the page)
TOKEN_TTL_S = _i("RUNNER_TOKEN_TTL_S", 3600)
# group 447: a Telegram message (through the notification service) when an AUTOMATIC run finishes or is blocked; manual runs only if asked
NOTIFY_ENABLED = _b("RUNNER_NOTIFY_ENABLED", True)
NOTIFY_MANUAL = _b("RUNNER_NOTIFY_MANUAL", False)
# group 447: a new result of a job / pack replaces the older one (Runs & downloads shows one line per job); set false to keep the history again
KEEP_LATEST_ONLY = _b("RUNNER_KEEP_LATEST_ONLY", True)
# group 466: heavy jobs (replays, backtests, probes) get 6 h (jobs.py reads RUNNER_HEAVY_TIMEOUT_S itself; never below 1 h)
HEAVY_TIMEOUT_S = max(3600, _i("RUNNER_HEAVY_TIMEOUT_S", 6 * 3600))
# group 466: a Telegram message that could not be delivered is kept in <data>/outbox and retried every OUTBOX_RETRY_S, dropped after OUTBOX_MAX_AGE_S
OUTBOX_RETRY_S = max(10, _i("RUNNER_OUTBOX_RETRY_S", 60))
OUTBOX_MAX_AGE_S = max(600, _i("RUNNER_OUTBOX_MAX_AGE_S", 24 * 3600))
# group 466: an AUTOMATIC run waits at most this long for the other services to answer /health before it starts (0 = do not wait)
PREFLIGHT_S = max(0, _i("RUNNER_PREFLIGHT_S", 60))

# group 476: auto-calibration. The switches that have a test are set from the test results (see autocalib.py); every change is sent to Telegram.
AUTOCALIB_ENABLED = _b("AUTOCALIB_ENABLED", True)          # master switch; the page can change it (stored in <data>/state/autocalib.json, which then wins)
AUTOCALIB_DRY_RUN = _b("AUTOCALIB_DRY_RUN", False)         # true = decide and report, apply nothing
AUTOCALIB_COOLDOWN_H = max(0, _i("AUTOCALIB_COOLDOWN_H", 24))        # a switch changed by auto-calibration is left alone this long
AUTOCALIB_MAX_AGE_DAYS = max(1, _i("AUTOCALIB_MAX_AGE_DAYS", 40))    # a test result older than this is not evidence
AUTOCALIB_STEP_PCT = max(1, min(100, _i("AUTOCALIB_STEP_PCT", 25)))  # a number moves at most this % per evaluation

# Market Weather (Part 1 step 4): morning 08:00 / refine 08:50 / lock 09:14 / tick every 5 min, SHADOW only: they log and send the brief, they never change a switch.
WEATHER_ENABLED = _b("WEATHER_ENABLED", False)      # off until you switch it on (PUT /weather/settings, stored in <data>/state/weather.json, which then wins)
WEATHER_STAMP = _b("WEATHER_STAMP", False)          # true = also post a STAMP-ONLY overlay (entries {}) so trades carry the state; never moves a dial
WEATHER_APPLY = _b("WEATHER_APPLY", False)          # true = LIVE: send tighten-only dials, but only while the evidence gate passes AND the service itself has MARKET_STATE_MODE=live (weather_apply.py)
WEATHER_GATE_MODE = (_s("WEATHER_GATE_MODE", "replay_only") or "replay_only").strip().lower()   # replay_only (default) | strict (replay USEFUL AND shadow report PASS) | off; unknown = strict
WEATHER_NOTIFY = _b("WEATHER_NOTIFY", True)         # Telegram brief at 08:00 and a message whenever the state changes
