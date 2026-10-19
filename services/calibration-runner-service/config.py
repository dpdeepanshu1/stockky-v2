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

# one job at a time, never while a heavy job would starve the trading containers
NICE = _i("RUNNER_NICE", 10)
MAX_OUTPUT_BYTES = _i("RUNNER_MAX_OUTPUT_BYTES", 5 * 1024 * 1024)
KEEP_RUNS = _i("RUNNER_KEEP_RUNS", 120)
KEEP_DAYS = _i("RUNNER_KEEP_DAYS", 45)
# heavy jobs (downloads, replays, model forecasts) are refused in these IST windows unless the caller confirms
GUARD_MARKET_HOURS = _b("RUNNER_GUARD_MARKET_HOURS", True)
SCHEDULER_ENABLED = _b("RUNNER_SCHEDULER_ENABLED", False)   # off until you switch it on in the page
TOKEN_TTL_S = _i("RUNNER_TOKEN_TTL_S", 3600)
# group 447: a Telegram message (through the notification service) when an AUTOMATIC run finishes or is blocked; manual runs only if asked
NOTIFY_ENABLED = _b("RUNNER_NOTIFY_ENABLED", True)
NOTIFY_MANUAL = _b("RUNNER_NOTIFY_MANUAL", False)
# group 447: a new result of a job / pack replaces the older one (Runs & downloads shows one line per job); set false to keep the history again
KEEP_LATEST_ONLY = _b("RUNNER_KEEP_LATEST_ONLY", True)
