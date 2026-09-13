"""
config.py — position-stocks-service.

DESIGN NOTE (isolation): this service intentionally does NOT import
anything from real-trade-service at runtime. Every constant/helper it
needs (tz_utils, oracle_compat, the Dhan tick-rounding/security-cache
logic, the AngelOne session/scrip-master logic) is duplicated here as its
own copy — see STATUS.md for the full rationale. This means the two
services can be deployed, restarted, and modified completely
independently; a bug or slow query in one can never block the other.

Shares the SAME physical database as every other Stockky service (same
DATABASE_URL / ORACLE_* contract as real-trade-service) but ALL new
tables are prefixed `scalp_` so they can never collide with the
existing `trade_*` tables. The one exception is `trade_credentials`,
which this service only ever READS (never writes) — see
auth/dhan_credentials_ro.py's module docstring for why.
"""
from __future__ import annotations

import os


def _get_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


PORT = _get_int("PORT", 8006)

# ── Database (same instance as every other Stockky service) ────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")
ORACLE_DSN = os.getenv("ORACLE_DSN", "")
ORACLE_USER = os.getenv("ORACLE_USER", "ADMIN")
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD", "")
ORACLE_WALLET_PASSWORD = os.getenv("ORACLE_WALLET_PASSWORD", "")
ORACLE_WALLET_DIR = os.getenv("ORACLE_WALLET_DIR", "/oracle_wallet")

DB_POOL_SIZE = _get_int("DB_POOL_SIZE", 3)
DB_MAX_OVERFLOW = _get_int("DB_MAX_OVERFLOW", 3)
DB_POOL_RECYCLE = _get_int("DB_POOL_RECYCLE", 1800)
DB_POOL_TIMEOUT = _get_int("DB_POOL_TIMEOUT", 30)

# ── Dhan (order execution — SAME account/credentials as real-trade-service) ─
# This service only READS the trade_credentials row real-trade-service
# already owns and refreshes. It never writes/refreshes the token itself —
# see auth/dhan_credentials_ro.py.
DHAN_CREDENTIAL_ENC_KEY = os.getenv("DHAN_CREDENTIAL_ENC_KEY", "")

# ── Angel One (FREE market data feed — separate account from Dhan) ─────────
ANGELONE_CLIENT_ID = os.getenv("ANGELONE_CLIENT_ID", "")
ANGELONE_MPIN = os.getenv("ANGELONE_MPIN", "")
ANGELONE_API_KEY = os.getenv("ANGELONE_API_KEY", "")
ANGELONE_TOTP_SECRET = os.getenv("ANGELONE_TOTP_SECRET", "")
ANGELONE_STATIC_IP = os.getenv("ANGELONE_STATIC_IP", "")
ANGELONE_WS_URL = os.getenv(
    "ANGELONE_WS_URL", "wss://smartapisocket.angelone.in/smart-stream"
)
# SmartAPI: up to 3 concurrent WS connections per client code. We only need 1.
ANGELONE_WS_MAX_SYMBOLS_PER_CONNECTION = _get_int(
    "ANGELONE_WS_MAX_SYMBOLS_PER_CONNECTION", 1000
)
ANGELONE_WS_HEARTBEAT_INTERVAL_S = _get_float("ANGELONE_WS_HEARTBEAT_INTERVAL_S", 25.0)
ANGELONE_WS_RECONNECT_BACKOFF_S = _get_float("ANGELONE_WS_RECONNECT_BACKOFF_S", 3.0)
ANGELONE_WS_RECONNECT_BACKOFF_MAX_S = _get_float(
    "ANGELONE_WS_RECONNECT_BACKOFF_MAX_S", 60.0
)

# ── Scan universe ────────────────────────────────────────────────────────────
SCAN_UNIVERSE_SOURCE = os.getenv("SCAN_UNIVERSE_SOURCE", "all_nse_eq")

# ── Screening windows ────────────────────────────────────────────────────────
SCAN_WINDOWS_MINUTES = [1, 5, 15, 60]
# 1m added 2026-09-12: a much shorter/noisier window than 5m, so its
# threshold defaults meaningfully lower than 5m's — tune via env once you've
# seen how much noise vs. signal it surfaces in practice.
MIN_PCT_CHANGE_1M = _get_float("MIN_PCT_CHANGE_1M", 0.5)
MIN_PCT_CHANGE_5M = _get_float("MIN_PCT_CHANGE_5M", 1.0)
MIN_PCT_CHANGE_15M = _get_float("MIN_PCT_CHANGE_15M", 1.5)
MIN_PCT_CHANGE_60M = _get_float("MIN_PCT_CHANGE_60M", 2.5)
MIN_AVG_VOLUME = _get_int("MIN_AVG_VOLUME", 50_000)
MAX_SPREAD_PCT = _get_float("MAX_SPREAD_PCT", 0.5)

# ── Adaptive target / stoploss bands (user-specified ranges) ────────────────
MIN_TARGET_PCT = _get_float("MIN_TARGET_PCT", 3.0)
MAX_TARGET_PCT = _get_float("MAX_TARGET_PCT", 8.0)
MIN_STOP_PCT = _get_float("MIN_STOP_PCT", 2.0)
MAX_STOP_PCT = _get_float("MAX_STOP_PCT", 5.0)

# ── Position limits ──────────────────────────────────────────────────────────
MAX_CONCURRENT_SCALP_POSITIONS = _get_int("MAX_CONCURRENT_SCALP_POSITIONS", 5)
MIN_PREFERRED_SCALP_POSITIONS = _get_int("MIN_PREFERRED_SCALP_POSITIONS", 1)

# ── Risk / capital sizing (CONFIRMED by user, tracking doc §3.5 / §5 item 9) ─
# User confirmed 2% of the scalp pool risked per single trade.
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", 2.0)
RISK_PER_TRADE_PCT_CONFIRMED = _get_bool("RISK_PER_TRADE_PCT_CONFIRMED", True)

# ── Capital split with real-trade-service ───────────────────────────────────
SCALP_POOL_CAPITAL_SHARE_PCT = _get_float("SCALP_POOL_CAPITAL_SHARE_PCT", 50.0)

# ── Order execution ──────────────────────────────────────────────────────────
SCALP_PRODUCT_TYPE = os.getenv("SCALP_PRODUCT_TYPE", "INTRA")  # NOT "CNC"
SCALP_EXCHANGE_SEGMENT = os.getenv("SCALP_EXCHANGE_SEGMENT", "NSE_EQ")
USE_SUPER_ORDER = _get_bool("USE_SUPER_ORDER", True)
# First-live-trade safety valve (recommended, not enforced): forces qty=1
# on the very first REAL order this process ever places after startup,
# regardless of the computed adaptive size, so you see Dhan's actual
# response shape before trusting it at normal size. Set to false once
# you've seen a clean first fill.
FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE = _get_bool("FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE", True)

# ── EOD square-off ───────────────────────────────────────────────────────────
EOD_SQUAREOFF_TIME_IST = os.getenv("EOD_SQUAREOFF_TIME_IST", "15:00")

# ── Arming ──────────────────────────────────────────────────────────────────
# Starts DISARMED — must be armed explicitly via POST /arm after startup.
_STARTUP_ARMED_DEFAULT = False

# ── Shared Dhan account-wide order-rate budget ──────────────────────────────
DAILY_ORDER_BUDGET = _get_int("POSITION_STOCKS_DAILY_ORDER_BUDGET", 300)

# ── Daily loss kill switch (tighter than real-trade-service's, by design) ──
MAX_DAILY_LOSS_PCT_OF_POOL = _get_float("MAX_DAILY_LOSS_PCT_OF_POOL", 4.0)

# ── Quality gate — fundamental/technical/news pre-check (session 6) ────────
# Applied ONLY to the top few candidates the fast price/volume screen already
# ranked highest — never the whole scan universe — so it stays "quick" as
# requested: the expensive calls happen for a handful of symbols per cycle,
# not hundreds. Mirrors real-trade-service's candidate_engine/candidates.py
# quality-gate pattern and reuses the SAME shared analysis-intelligence-service
# endpoints (this is a shared read-only backend, not real-trade-service's own
# state — calling it doesn't violate this service's real-trade isolation).
# Every call is best-effort with a short timeout and fails OPEN (missing data
# is leniently treated as "unknown", never an automatic reject) — a slow or
# unhealthy analysis-intelligence-service must never stall or block the scalp
# loop, matching this service's core isolation promise.
QUALITY_GATE_ENABLED = _get_bool("QUALITY_GATE_ENABLED", True)
QUALITY_GATE_TOP_N = _get_int("QUALITY_GATE_TOP_N", 3)
# Deliberately much shorter than real-trade-service's 12-60s timeouts — this
# loop ticks every 10s, so anything slower than a couple seconds isn't "quick".
QUALITY_GATE_TIMEOUT_S = _get_float("QUALITY_GATE_TIMEOUT_S", 2.5)

_ANALYSIS_INTELLIGENCE_URL = os.getenv(
    "ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com"
).rstrip("/")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/technical").rstrip("/")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/fundamental").rstrip("/")
EVENT_URL = os.getenv("EVENT_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/event").rstrip("/")

# Lenient floors — same philosophy as real-trade-service's VOLUME_SHOCK_*
# quality gate: only reject when data IS available and clearly below floor;
# missing/timed-out data never rejects on its own.
MIN_FUNDAMENTAL_SCORE = _get_float("MIN_FUNDAMENTAL_SCORE", 40.0)
MIN_TECHNICAL_SCORE = _get_float("MIN_TECHNICAL_SCORE", 40.0)
MIN_MARKET_CAP_CR = _get_float("MIN_MARKET_CAP_CR", 500.0)  # ₹500 crore floor — excludes micro-caps

# ── Shared Dhan account-wide order-rate budget (tracking doc §3.8) ─────────
# Dhan's own account-wide cap is roughly 5,000-7,000 orders/day, shared with
# real-trade-service (same Dhan account). This is a soft, fail-open governor
# — see capital/shared_order_budget.py's docstring and models.py's
# SharedOrderBudget for the full rationale.
SHARED_DAILY_ORDER_BUDGET = _get_int("SHARED_DAILY_ORDER_BUDGET", 5000)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
