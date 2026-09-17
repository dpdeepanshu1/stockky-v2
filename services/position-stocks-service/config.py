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
ANGELONE_WS_HEARTBEAT_INTERVAL_S = _get_float("ANGELONE_WS_HEARTBEAT_INTERVAL_S", 10.0)
# BUG FIX (session14, live-tested): was 25.0. AngelOne's own reference
# client (smartapi-python's SmartWebSocketV2) sends its "ping" heartbeat
# every 10s, not 25s. Tightening to match exactly, since the 25s interval
# didn't prevent the observed ~120s disconnect cycle either way — this
# alone may not be the full fix (see feed/ws_client.py's close_code/reason
# logging added this session for the real diagnosis), but matching the
# documented reference behavior exactly is the correct baseline regardless.
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
# DECISION (2026-09-16, session46 — user call, goal stated as "enter quality
# stock, buy and sell on time, maximum profit, lower the loss"): when open
# positions are BELOW this count, the service tries harder to find a
# quality entry — but "tries harder" only ever means widening HOW MANY
# ranked candidates get a shot, never LOWERING the bar any candidate must
# clear. Concretely, when under-preferred:
#   1. screening/engine.py's per-window pct-change thresholds relax by
#      MIN_PREFERRED_THRESHOLD_RELAX_PCT (bounded, floor-protected) — this
#      only affects which candidates get RANKED at all, it's a signal-
#      sensitivity knob, not a quality/risk gate.
#   2. main.py widens QUALITY_GATE_TOP_N by MIN_PREFERRED_EXTRA_TOP_N so
#      more ranked candidates get checked against quality_gate.py.
# Explicitly NEVER touched by this: MIN_FUNDAMENTAL_SCORE, MIN_TECHNICAL_SCORE,
# MIN_MARKET_CAP_CR (screening/quality_gate.py's real quality bar),
# MAX_SPREAD_PCT, RISK_PER_TRADE_PCT, circuit_breaker, restricted-symbol
# filtering — every hard risk/capital-safety gate stays exactly as strict
# as when there ARE enough positions open. Being under-preferred is a
# reason to look at more candidates, never a reason to accept a worse one.
MIN_PREFERRED_SCALP_POSITIONS = _get_int("MIN_PREFERRED_SCALP_POSITIONS", 1)
MIN_PREFERRED_THRESHOLD_RELAX_PCT = _get_float("MIN_PREFERRED_THRESHOLD_RELAX_PCT", 15.0)
MIN_PREFERRED_EXTRA_TOP_N = _get_int("MIN_PREFERRED_EXTRA_TOP_N", 2)

# ── Risk / capital sizing (CONFIRMED by user, tracking doc §3.5 / §5 item 9) ─
# User confirmed 2% of the scalp pool risked per single trade.
RISK_PER_TRADE_PCT = _get_float("RISK_PER_TRADE_PCT", 2.0)
RISK_PER_TRADE_PCT_CONFIRMED = _get_bool("RISK_PER_TRADE_PCT_CONFIRMED", True)

# ── Capital split with real-trade-service ───────────────────────────────────
SCALP_POOL_CAPITAL_SHARE_PCT = _get_float("SCALP_POOL_CAPITAL_SHARE_PCT", 50.0)

# ── Order execution ──────────────────────────────────────────────────────────
SCALP_PRODUCT_TYPE = os.getenv("SCALP_PRODUCT_TYPE", "INTRADAY")  # NOT "CNC"
# BUG FIX (session38): this previously defaulted to "INTRA", which is not
# a value Dhan's actual REST API accepts for productType — the real enum
# (per Dhan's official API docs and confirmed by real-trade-service's own
# working code, which consistently uses "INTRADAY" for every same-day
# round-trip order) is CNC / INTRADAY / MARGIN / MTF / CO / BO. The dhanhq
# SDK does not validate or translate this locally — it just uppercases
# whatever string is passed and forwards it straight to Dhan's server,
# which then rejects the ENTIRE super-order payload with a generic
# catch-all error ("Missing required fields, bad values for parameters
# etc.") that gives no indication which field was actually wrong. Live-
# confirmed: every single entry attempt for this service failed with
# exactly that error, on every candidate, every cycle — consistent with
# `first_live_order_done` never once flipping true. Not overridden by any
# env var in docker-compose.yml/.env.example, so this default is what was
# actually running.
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

# 2026-09-15 fix (session40 — mirrors real-trade-service's DATAMATICS-storm
# hardening, applied here even though this service's EOD sweep runs at most
# ONCE per day per config.EOD_SQUAREOFF_TIME_IST guard, not in a fast
# every-45s retry loop, so the open-ended-retry-storm failure mode itself
# doesn't apply the same way. What DOES apply: a single flat-SELL attempt
# that fails on a transient blip (not one of the three classified permanent-
# for-today rejection types already handled in eod_squareoff.py) used to be
# given up on immediately, leaving a real position OPEN past hard-flat time
# with zero further attempts until tomorrow's sweep. These settings bound a
# small in-call retry for genuinely transient failures only.
EOD_SELL_RETRY_ATTEMPTS = _get_int("EOD_SELL_RETRY_ATTEMPTS", 3)
EOD_SELL_RETRY_DELAY_SECONDS = float(os.getenv("EOD_SELL_RETRY_DELAY_SECONDS", "2.0"))

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

# ── Entry range-position hard gate (this session — "buy/sell timing ... not
# high low aware or price aware") ───────────────────────────────────────────
# screening/engine.py already applies a SOFT range-position penalty to
# composite_score (0.50×/0.75× near the day-high) and orders/adaptive.py
# already tightens target/stop near the day-high — but neither of those
# actually stops a candidate sitting AT the day's high from being bought;
# they only make it less likely to rank first / give it a smaller target.
# If nothing better is available that cycle, a candidate right at its peak
# for the day could still be entered — textbook "buy on the high point".
# This is a genuine hard floor, checked in orders/entry.py right before an
# order is placed: reject outright (not just deprioritise) when LTP is
# within the top (1 - MAX_ENTRY_RANGE_POSITION) of today's observed
# high/low range. Fails OPEN (never rejects) when there isn't enough real
# tick depth to trust the range yet, same philosophy as every other gate
# in this service.
MAX_ENTRY_RANGE_POSITION = _get_float("MAX_ENTRY_RANGE_POSITION", 0.92)
MIN_TICKS_FOR_RANGE_GATE = _get_int("MIN_TICKS_FOR_RANGE_GATE", 10)

# ── Same-symbol re-entry guard (this session) ───────────────────────────────
# Root cause of the reported "takes a trade, makes profit, exits, then buys
# again right away and makes a loss" pattern — live example in the user's
# own trade history: NAHARINDUS sold at ₹139.79 (TARGET_HIT, 01:47pm), then
# bought again at ₹139.50 only 7 minutes later (01:54pm) — essentially the
# SAME price the first trade just took profit at, not a fresh dip — and
# that one stopped out. Nothing previously distinguished "a genuinely new
# signal on a symbol we haven't touched" from "the same symbol re-firing
# the instant its last trade closed, at/above the price we just exited".
# Both conditions below must be true to allow a re-entry within the
# cooldown window — enough time must have passed, OR price must have
# pulled back meaningfully below the last exit (a real dip worth taking):
SYMBOL_REENTRY_COOLDOWN_MINUTES = _get_int("SYMBOL_REENTRY_COOLDOWN_MINUTES", 30)
SYMBOL_REENTRY_MIN_PULLBACK_PCT = _get_float("SYMBOL_REENTRY_MIN_PULLBACK_PCT", 1.0)

# ── Shared Dhan account-wide order-rate budget (tracking doc §3.8) ─────────
# Dhan's own account-wide cap is roughly 5,000-7,000 orders/day, shared with
# real-trade-service (same Dhan account). This is a soft, fail-open governor
# — see capital/shared_order_budget.py's docstring and models.py's
# SharedOrderBudget for the full rationale.
SHARED_DAILY_ORDER_BUDGET = _get_int("SHARED_DAILY_ORDER_BUDGET", 5000)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# ── Admin auth (Layer 1) — SAME mechanism, SAME env vars as real-trade-
# service's config.py (auth/admin_auth.py docstring there has the full
# rationale). Deliberately not imported from real-trade-service (this
# service duplicates everything per the isolation note at the top of this
# file) — but it reads the exact same ADMIN_USERNAME / ADMIN_PASSWORD_HASH
# (or _B64) / SESSION_SECRET keys out of the SAME .env, so logging in with
# your one admin password works identically on both services' dashboards.
# Generate the hash once with:
#   python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('yourpassword'))"
# See real-trade-service/config.py's comment for why ADMIN_PASSWORD_HASH_B64
# exists (docker-compose .env $ interpolation) — same applies here.
_ADMIN_HASH_B64 = os.getenv("ADMIN_PASSWORD_HASH_B64", "")
if _ADMIN_HASH_B64 and not os.getenv("ADMIN_PASSWORD_HASH"):
    try:
        import base64 as _b64
        ADMIN_PASSWORD_HASH = _b64.b64decode(_ADMIN_HASH_B64).decode("utf-8").strip()
    except Exception:
        ADMIN_PASSWORD_HASH = ""
else:
    ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_IDLE_TIMEOUT_MINUTES = _get_int("SESSION_IDLE_TIMEOUT_MINUTES", 30)

# ── Notifications (session41 fix — STATUS.md open item #7) ─────────────
# SAME env vars / SAME notification-scheduler-service routing convention
# as real-trade-service's config.py + notifier.py, duplicated here per
# this file's isolation note at the top (this service must not import
# real-trade-service at runtime). Lets the existing CRITICAL log lines in
# execution/dhan_client.py and orders/reconcile.py (order-type mismatches,
# dead EOD SELLs, unresolvable legacy exit-order backfills) actually reach
# a human instead of sitting log-only.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
NOTIFICATION_SERVICE_URL = os.getenv(
    "NOTIFICATION_SERVICE_URL",
    "http://notification-scheduler-service:8000/notification",
).rstrip("/")

# ── Cross-service PnL sync (Issue #2 fix) ──────────────────────────────
# Used by capital/ledger.py's sync_peer_pnl() to fetch real-trade-service's
# realized_pnl_today so position-stocks' daily-loss kill switch is aware of
# losses booked by the PEER service on the same shared Dhan account.
# Read-only, no auth — only /status/REAL is hit, which is a public endpoint.
REAL_TRADE_SERVICE_URL = os.getenv(
    "REAL_TRADE_SERVICE_URL",
    "http://real-trade-service:8005",
).rstrip("/")
