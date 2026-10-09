"""dhan_data/config.py - blank-safe env readers for the Dhan Data API integration (group 270).

Every value is read with the `(os.getenv(X) or "").strip() or default` pattern used across this repo, so an
empty `X=` line in an env_file can never crash an import or turn a number into "".
Values are read at CALL time (not import time) so tests and `docker compose up` env changes behave.
"""
from __future__ import annotations

import os

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def env_float(name: str, default: float, minimum: float | None = None) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        v = float(raw) if raw else default
    except (TypeError, ValueError):
        v = default
    if v != v:  # NaN
        v = default
    if minimum is not None and v < minimum:
        v = minimum
    return v


def env_int(name: str, default: int, minimum: int | None = None) -> int:
    return int(env_float(name, float(default), None if minimum is None else float(minimum)))


def env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


# ---- master switches ------------------------------------------------------------------------------------------
def enabled() -> bool:
    """DHAN_DATA_ENABLED (default ON). Off = no Dhan data call is ever made."""
    return env_flag("DHAN_DATA_ENABLED", True)


def base_url() -> str:
    return env_str("DHAN_DATA_BASE_URL", "https://api.dhan.co/v2").rstrip("/")


def scrip_master_url() -> str:
    return env_str("DHAN_SCRIP_MASTER_URL", "https://images.dhan.co/api-data/api-scrip-master.csv")


def http_timeout_s() -> float:
    return env_float("DHAN_HTTP_TIMEOUT_S", 6.0, 1.0)


# ---- provider order ------------------------------------------------------------------------------------------
_KNOWN = ("dhan", "angelone", "yfinance")


def _order(var: str) -> list[str]:
    raw = env_str(var, "dhan,angelone,yfinance").lower()
    out: list[str] = []
    for part in raw.split(","):
        p = part.strip()
        if p in _KNOWN and p not in out:
            out.append(p)
    return out or list(_KNOWN)


def quote_order() -> list[str]:
    return _order("QUOTE_PROVIDER_ORDER")


def history_order() -> list[str]:
    return _order("HISTORY_PROVIDER_ORDER")


def position(order: list[str]) -> str:
    """Where the Dhan stage sits in a waterfall that already has AngelOne and yfinance stages.

    "off"            - dhan not in the list, or DHAN_DATA_ENABLED=0
    "first"          - dhan ahead of angelone and yfinance
    "after_angelone" - angelone, then dhan (yfinance, if listed, later)
    "after_yfinance" - dhan listed after yfinance
    """
    if not enabled() or "dhan" not in order:
        return "off"
    i = order.index("dhan")
    ia = order.index("angelone") if "angelone" in order else None
    iy = order.index("yfinance") if "yfinance" in order else None
    before = [x for x in (ia, iy) if x is not None and x < i]
    if not before:
        return "first"
    if iy is not None and iy < i:
        return "after_yfinance"
    return "after_angelone"


def quote_position() -> str:
    return position(quote_order())


def history_position() -> str:
    return position(history_order())


# ---- rate / batching -----------------------------------------------------------------------------------------
def quote_min_interval_s() -> float:
    return env_float("DHAN_QUOTE_MIN_INTERVAL_S", 1.1, 0.2)


def quote_max_batch() -> int:
    return env_int("DHAN_QUOTE_MAX_BATCH", 1000, 1)


def quote_batch_window_s() -> float:
    return env_float("DHAN_QUOTE_BATCH_WINDOW_MS", 250.0, 0.0) / 1000.0


def quote_fresh_s() -> float:
    return env_float("DHAN_QUOTE_FRESH_S", 2.0, 0.0)


def quote_wait_s() -> float:
    """How long a single /quote caller waits for the next batch before falling through to the next provider."""
    return env_float("DHAN_QUOTE_WAIT_S", 3.5, 0.2)


def hist_max_per_sec() -> float:
    return env_float("DHAN_HIST_MAX_PER_SEC", 4.0, 0.2)


def intraday_chunk_days() -> int:
    """Days of intraday candles asked per request. Dhan's docs differ between versions (5 vs 90 days): VERIFY."""
    return env_int("DHAN_INTRADAY_CHUNK_DAYS", 30, 1)


def breaker_fails() -> int:
    return env_int("DHAN_BREAKER_FAILS", 5, 1)


def breaker_recovery_s() -> float:
    return env_float("DHAN_BREAKER_RECOVERY_S", 60.0, 5.0)


def auth_pause_s() -> float:
    """After an auth / subscription failure the Dhan stage is skipped for this long (no per-second retries)."""
    return env_float("DHAN_AUTH_PAUSE_S", 60.0, 5.0)


# ---- live poller (group 270 phase 1) ------------------------------------------------------------------------------
def live_poller_enabled() -> bool:
    return env_flag("DHAN_LIVE_POLLER", True)


def live_poll_interval_s() -> float:
    return env_float("DHAN_LIVE_POLL_S", 1.2, 0.5)


def live_max_symbols() -> int:
    return env_int("DHAN_LIVE_MAX_SYMBOLS", 900, 1)


def hourly_max_days() -> int:
    """Longest window requested for hourly/intraday candles (blank or non-numeric -> 60; floor 1)."""
    return env_int("DHAN_HOURLY_MAX_DAYS", 60, 1)


# ---- websocket (phase 4, opt-in) ---------------------------------------------------------------------------------
def ws_enabled() -> bool:
    return env_flag("DHAN_WS_ENABLED", False)


def ws_url() -> str:
    return env_str("DHAN_WS_URL", "wss://api-feed.dhan.co")


def ws_max_instruments() -> int:
    return env_int("DHAN_WS_MAX_INSTRUMENTS", 4000, 1)


# ---- credentials ------------------------------------------------------------------------------------------------
def creds_cache_s() -> float:
    return env_float("DHAN_CREDS_CACHE_S", 60.0, 1.0)


def enc_key() -> str:
    return env_str("DHAN_CREDENTIAL_ENC_KEY", "")


# ---- shadow comparison -------------------------------------------------------------------------------------------
def shadow_compare() -> bool:
    return env_flag("DHAN_SHADOW_COMPARE", False)
