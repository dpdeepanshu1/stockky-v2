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


def _get_str(name: str, default: str) -> str:
    """Trimmed env string; a missing, blank or whitespace-only variable gives `default`.

    docker-compose / .env files routinely carry `NAME=` (an empty string), and
    `os.getenv(NAME, default)` returns that empty string instead of the default."""
    return (os.getenv(name) or "").strip() or default


def _get_bool(name: str, default: bool) -> bool:
    # A blank / whitespace-only value is "unset", NOT "false": with the old
    # `os.getenv(name, str(default))` a stray `LOSS_BRAKE_ENABLED=` (or
    # MARKET_GATE_ENABLED / QUALITY_GATE_ENABLED ...) silently switched a
    # default-True safety gate OFF.
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    try:
        return float(((os.getenv(name) or "").strip() or str(default)))
    except (TypeError, ValueError):
        return default


def _get_int(name: str, default: int) -> int:
    try:
        return int(((os.getenv(name) or "").strip() or str(default)))
    except (TypeError, ValueError):
        return default


PORT = _get_int("PORT", 8006)

# ── Database (same instance as every other Stockky service) ────────────────
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
ORACLE_DSN = (os.getenv("ORACLE_DSN") or "").strip()
ORACLE_USER = (os.getenv("ORACLE_USER") or "").strip() or "ADMIN"
ORACLE_PASSWORD = os.getenv("ORACLE_PASSWORD") or ""
if not ORACLE_PASSWORD.strip():  # passwords are kept verbatim; whitespace-only = unset
    ORACLE_PASSWORD = ""
ORACLE_WALLET_PASSWORD = os.getenv("ORACLE_WALLET_PASSWORD") or ""
if not ORACLE_WALLET_PASSWORD.strip():
    ORACLE_WALLET_PASSWORD = ""
ORACLE_WALLET_DIR = (os.getenv("ORACLE_WALLET_DIR") or "").strip() or "/oracle_wallet"

DB_POOL_SIZE = _get_int("DB_POOL_SIZE", 3)
DB_MAX_OVERFLOW = _get_int("DB_MAX_OVERFLOW", 3)
DB_POOL_RECYCLE = _get_int("DB_POOL_RECYCLE", 1800)
DB_POOL_TIMEOUT = _get_int("DB_POOL_TIMEOUT", 30)

# ── Dhan (order execution — SAME account/credentials as real-trade-service) ─
# This service only READS the trade_credentials row real-trade-service
# already owns and refreshes. It never writes/refreshes the token itself —
# see auth/dhan_credentials_ro.py.
DHAN_CREDENTIAL_ENC_KEY = (os.getenv("DHAN_CREDENTIAL_ENC_KEY") or "").strip()

# ── Angel One (FREE market data feed — separate account from Dhan) ─────────
ANGELONE_CLIENT_ID = (os.getenv("ANGELONE_CLIENT_ID") or "").strip()
ANGELONE_MPIN = (os.getenv("ANGELONE_MPIN") or "").strip()
ANGELONE_API_KEY = (os.getenv("ANGELONE_API_KEY") or "").strip()
ANGELONE_TOTP_SECRET = (os.getenv("ANGELONE_TOTP_SECRET") or "").strip()
ANGELONE_STATIC_IP = _get_str("ANGELONE_STATIC_IP", "")
ANGELONE_WS_URL = _get_str("ANGELONE_WS_URL", "wss://smartapisocket.angelone.in/smart-stream")
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
SCAN_UNIVERSE_SOURCE = _get_str("SCAN_UNIVERSE_SOURCE", "all_nse_eq")

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
# 2026-09-18 (user audit finding, fixed): this is now compared directly
# against REAL cumulative day volume (shares) from the mode-3 WS feed —
# see feed/ws_client.py's docstring. It used to be divided by 5000 and
# compared against a tick-COUNT proxy, since mode-1 carried no real
# volume field at all. If you were relying on the old env value, note
# the units changed: 50_000 here now means "50,000 shares traded so far
# today", not "10 ticks in the rolling window" — re-tune if needed.
MAX_SPREAD_PCT = _get_float("MAX_SPREAD_PCT", 0.5)
# 2026-09-18 (user audit finding, fixed): this was defined and documented
# (see the MIN_PREFERRED_SCALP_POSITIONS comment below, STATUS.md) as a
# hard risk gate that "stays exactly as strict regardless of position
# count" — but it was never actually enforced anywhere; the mode-1 feed
# had no bid/ask to check it against. Now enforced in screening/engine.py
# via feed/ws_client.py's mode-3 depth data (fails OPEN, not closed, when
# a symbol's depth genuinely hasn't arrived yet — see that gate's comment).

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
SCALP_PRODUCT_TYPE = _get_str("SCALP_PRODUCT_TYPE", "INTRADAY")  # NOT "CNC"
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
SCALP_EXCHANGE_SEGMENT = _get_str("SCALP_EXCHANGE_SEGMENT", "NSE_EQ")
USE_SUPER_ORDER = _get_bool("USE_SUPER_ORDER", True)
# First-live-trade safety valve (recommended, not enforced): forces qty=1
# on the very first REAL order this process ever places after startup,
# regardless of the computed adaptive size, so you see Dhan's actual
# response shape before trusting it at normal size. Set to false once
# you've seen a clean first fill.
FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE = _get_bool("FIRST_LIVE_ORDER_MIN_QTY_OVERRIDE", True)

# ── EOD square-off ───────────────────────────────────────────────────────────
EOD_SQUAREOFF_TIME_IST = _get_str("EOD_SQUAREOFF_TIME_IST", "15:00")

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
EOD_SELL_RETRY_DELAY_SECONDS = _get_float("EOD_SELL_RETRY_DELAY_SECONDS", 2.0)

# ── Exit-placement retry backoff (2026-09-20 audit fix) ───────────────────────
# Mirrors real-trade-service's EXIT_RETRY_* knobs (same names, same defaults —
# born from that service's session40 DATAMATICS incident). Gates repeated flat
# -SELL PLACEMENT attempts across cycles (run_stagnation_exit runs every fast
# loop tick against every OPEN position) — a placement that keeps failing
# outright (not just slow to fill) now backs off exponentially instead of
# being retried every single cycle. See orders/exit_retry.py.
EXIT_RETRY_BASE_COOLDOWN_SECONDS = _get_float("EXIT_RETRY_BASE_COOLDOWN_SECONDS", 60.0)
EXIT_RETRY_MAX_COOLDOWN_SECONDS = _get_float("EXIT_RETRY_MAX_COOLDOWN_SECONDS", 900.0)
EXIT_RETRY_ALERT_THRESHOLD = _get_int("EXIT_RETRY_ALERT_THRESHOLD", 5)

# ── Arming ──────────────────────────────────────────────────────────────────
# Starts DISARMED — must be armed explicitly via POST /arm after startup.
_STARTUP_ARMED_DEFAULT = False

# ── Shared Dhan account-wide order-rate budget ──────────────────────────────
DAILY_ORDER_BUDGET = _get_int("POSITION_STOCKS_DAILY_ORDER_BUDGET", 300)

# ── Daily loss kill switch (tighter than real-trade-service's, by design) ──
MAX_DAILY_LOSS_PCT_OF_POOL = _get_float("MAX_DAILY_LOSS_PCT_OF_POOL", 4.0)

# ── Trade gates (2026-10-02 loss-day fix) ───────────────────────────────────
# Added after a day where ~19 scalp trades netted about -₹370 (avg loss -1.3%,
# avg win +0.3%). Three cheap, fail-open gates; each can be switched off with
# its env var. They apply to AUTO entries only — a manual /cycle/run is an
# explicit human action and bypasses them, same as it bypasses auto-pilot.
#
# 1. Market filter: no new entries while Nifty is trading below its day open
#    by more than MARKET_GATE_MIN_NIFTY_CHANGE_PCT. Source is the API
#    gateway's /market/indices (same endpoint real-trade-service uses).
#    Fetch failure => ALLOW (a broken data feed must never freeze trading).
MARKET_GATE_ENABLED = _get_bool("MARKET_GATE_ENABLED", True)
MARKET_GATE_MIN_NIFTY_CHANGE_PCT = _get_float("MARKET_GATE_MIN_NIFTY_CHANGE_PCT", -0.10)
MARKET_GATE_CACHE_TTL_S = _get_float("MARKET_GATE_CACHE_TTL_S", 120.0)
# Group 221 (2026-10-07): the gate above measures Nifty against TODAY'S OPEN, so a market that gapped
# down 1.2% and has been flat since the open reads 0.0% and is let through. This second check blocks
# on the change against the PREVIOUS SESSION's close (gateway key nifty_vs_prev_close). The -0.75 default
# is my assumption (roughly a bottom-fifth Nifty day); there is no outcome data behind it. Ignored for a
# stale / fallback gateway payload and when the key is missing (older gateway), so it fails open.
MARKET_GATE_PREV_CLOSE_ENABLED = _get_bool("MARKET_GATE_PREV_CLOSE_ENABLED", True)
MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT = _get_float("MARKET_GATE_MIN_NIFTY_PREV_CLOSE_PCT", -0.75)
MARKET_GATE_TIMEOUT_S = _get_float("MARKET_GATE_TIMEOUT_S", 3.0)
_API_GATEWAY_URL = (os.getenv("API_GATEWAY_URL") or "").strip().rstrip("/") or "https://api-gateway-puwd.onrender.com"
def _env_url(name: str, default: str, rstrip: bool = True) -> str:
    """URL setting from the environment with a blank-safe fallback.

    os.getenv(name, default) only falls back when the variable is UNSET, so an empty or
    whitespace-only value (a blank Render dashboard variable, `NAME=` in an env_file) overrode a working
    default and every request went to "/path". Blank / whitespace-only (and, with rstrip, slash-only)
    values now use `default`; padded values are trimmed.
    """
    raw = (os.getenv(name) or "").strip()
    if rstrip:
        raw = raw.rstrip("/")
    return raw or (default.rstrip("/") if rstrip else default)


MARKET_INDICES_URL = _env_url("MARKET_INDICES_URL", f"{_API_GATEWAY_URL}/market/indices", rstrip=False)

# 2. Loss brake (softer and earlier than MAX_DAILY_LOSS_PCT_OF_POOL's 4% kill
#    switch): pause new entries after N consecutive losing closes today (for
#    LOSS_BRAKE_COOLDOWN_MINUTES after the last one), and stop for the rest of
#    the day once today's realized loss reaches LOSS_BRAKE_DAILY_PCT_OF_POOL.
LOSS_BRAKE_ENABLED = _get_bool("LOSS_BRAKE_ENABLED", True)
LOSS_BRAKE_MAX_CONSECUTIVE_LOSSES = _get_int("LOSS_BRAKE_MAX_CONSECUTIVE_LOSSES", 3)
LOSS_BRAKE_COOLDOWN_MINUTES = _get_int("LOSS_BRAKE_COOLDOWN_MINUTES", 60)
LOSS_BRAKE_DAILY_PCT_OF_POOL = _get_float("LOSS_BRAKE_DAILY_PCT_OF_POOL", 1.5)

# 3. No same-symbol re-entry for the rest of the day after it closed at a loss
#    (the 30-minute SYMBOL_REENTRY_COOLDOWN_MINUTES guard still applies to
#    symbols that closed at a profit).
SYMBOL_BLOCK_AFTER_LOSS_TODAY = _get_bool("SYMBOL_BLOCK_AFTER_LOSS_TODAY", True)

# ── Phase 2: candle-range volatility stop/target (2026-10-02) ───────────────
# OFF by default. The legacy "ATR proxy" in orders/adaptive.py averages the
# change between consecutive ticks (~0.02-0.1%), so stop*1.5 always fell under
# MIN_STOP_PCT and every trade got the same 2% stop / ~4% target. When enabled,
# volatility is measured from real candle ranges: ticks are bucketed into
# ADAPTIVE_BAR_MINUTES-minute bars, and the mean (high-low)/close of the last
# ADAPTIVE_BAR_LOOKBACK bars sets the stop. Needs ADAPTIVE_BAR_MIN_BARS bars
# of history; otherwise the legacy logic runs unchanged (fail-safe).
ADAPTIVE_BAR_ATR_ENABLED = _get_bool("ADAPTIVE_BAR_ATR_ENABLED", True)  # 2026-10-05: ON (was off) - the legacy tick proxy always landed on the 2% stop floor / ~4.4% target
ADAPTIVE_BAR_MINUTES = _get_int("ADAPTIVE_BAR_MINUTES", 5)
ADAPTIVE_BAR_LOOKBACK = _get_int("ADAPTIVE_BAR_LOOKBACK", 6)
ADAPTIVE_BAR_MIN_BARS = _get_int("ADAPTIVE_BAR_MIN_BARS", 3)
ADAPTIVE_BAR_STOP_MULT = _get_float("ADAPTIVE_BAR_STOP_MULT", 1.3)
ADAPTIVE_BAR_STOP_MIN_PCT = _get_float("ADAPTIVE_BAR_STOP_MIN_PCT", 0.8)
ADAPTIVE_BAR_STOP_MAX_PCT = _get_float("ADAPTIVE_BAR_STOP_MAX_PCT", 2.0)
ADAPTIVE_BAR_TARGET_RR = _get_float("ADAPTIVE_BAR_TARGET_RR", 1.8)
ADAPTIVE_BAR_TARGET_MAX_PCT = _get_float("ADAPTIVE_BAR_TARGET_MAX_PCT", 3.5)

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

_ANALYSIS_INTELLIGENCE_URL = _env_url("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com")
TECHNICAL_URL = _env_url("TECHNICAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/technical")
FUNDAMENTAL_URL = _env_url("FUNDAMENTAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/fundamental")
EVENT_URL = _env_url("EVENT_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/event")

# Lenient floors — same philosophy as real-trade-service's VOLUME_SHOCK_*
# quality gate: only reject when data IS available and clearly below floor;
# missing/timed-out data never rejects on its own.
MIN_FUNDAMENTAL_SCORE = _get_float("MIN_FUNDAMENTAL_SCORE", 40.0)
MIN_TECHNICAL_SCORE = _get_float("MIN_TECHNICAL_SCORE", 40.0)
MIN_MARKET_CAP_CR = _get_float("MIN_MARKET_CAP_CR", 500.0)  # ₹500 crore floor — excludes micro-caps

# ── Minimum stock price gate (2026-09-18, session67) ────────────────────────
# Penny stocks (₹0.19, ₹0.50, etc.) look great on a percentage-change scan
# because a 1-paise tick is already a 5% move. They also have near-zero
# absolute P&L even on a "win" (6481 shares × ₹0.43 move = ₹2,787), and
# Dhan frequently rejects them outright (T2T, circuit-limit, or exchange
# restriction). Checked in entry.py BEFORE quality gate — no network call
# needed, just a price comparison. Default ₹20 keeps all real intraday
# names; override via env to raise if you want only mid/large-cap territory.
MIN_STOCK_PRICE = _get_float("MIN_STOCK_PRICE", 20.0)

# ── EOD overnight carry (2026-09-18, session67) ──────────────────────────────
# By default EOD squareoff closes ALL positions at 15:00 unconditionally.
# When OVERNIGHT_HOLD_ENABLED=true, a position MAY be carried to the next
# day instead of being force-closed, but ONLY when ALL four conditions hold:
#   1. The position is currently in profit (unrealized P&L > 0).
#   2. Its fundamental_score at entry time >= OVERNIGHT_MIN_FUNDAMENTAL_SCORE
#      (stricter than the intraday floor of MIN_FUNDAMENTAL_SCORE).
#   3. Its technical_score at entry time >= OVERNIGHT_MIN_TECHNICAL_SCORE.
#   4. Its market_cap_cr >= OVERNIGHT_MIN_MARKET_CAP_CR (larger names only —
#      a penny/micro-cap held overnight is a gap-down risk with thin liquidity).
# If quality data was missing at entry (None fields on ScalpCandidateLog),
# the position is squaredoff anyway — "unknown quality" is not good enough
# for an overnight hold. When a position IS carried, it is converted to a
# real CNC holding (see the 2026-09-18 fix note below) — its old
# INTRADAY stop/target legs are cancelled as part of that conversion and
# NOT re-armed (a fresh same-day protective order against an
# unsettled/pending-eDIS CNC holding isn't something this codebase's
# order-placement wrapper has been verified to support correctly — see
# convert_position()'s docstring in execution/dhan_client.py). So a
# carried position has NO live stop-loss/target from the moment of
# conversion until whoever/whatever manages it the next day — this is a
# known, deliberate gap, not an oversight; the carry notification says
# so explicitly each time it fires.
OVERNIGHT_HOLD_ENABLED          = _get_bool("OVERNIGHT_HOLD_ENABLED", False)
OVERNIGHT_MIN_FUNDAMENTAL_SCORE = _get_float("OVERNIGHT_MIN_FUNDAMENTAL_SCORE", 60.0)
OVERNIGHT_MIN_TECHNICAL_SCORE   = _get_float("OVERNIGHT_MIN_TECHNICAL_SCORE", 60.0)
OVERNIGHT_MIN_MARKET_CAP_CR     = _get_float("OVERNIGHT_MIN_MARKET_CAP_CR", 2000.0)

# 2026-09-18 (user audit finding, fixed): two problems found in the carry
# path itself, both fixed in orders/eod_squareoff.py:
#  1. Every entry here uses product_type=INTRADAY (see SCALP_PRODUCT_TYPE
#     above, "NOT CNC" — for the same-day-eDIS reason explained there).
#     An INTRADAY position is force-squared-off by DHAN'S OWN broker-side
#     RMS before/at market close regardless of what this app decides —
#     so simply skipping this app's own EOD squareoff call, as before,
#     carried NOTHING: Dhan would flatten it anyway minutes later, at
#     whatever price prevailed then, with no further stop/target control
#     in between. Fixed by explicitly converting qualifying positions
#     INTRADAY -> CNC via Dhan's own /positions/convert endpoint
#     (execution/dhan_client.py::convert_position) before Dhan's RMS
#     cutoff — only a real CNC holding can actually survive to the next
#     day. This inherits real-trade-service's own well-documented CDSL
#     eDIS constraint (selling it tomorrow needs manual TPIN verification
#     in the Dhan app first) — the carry notification says this
#     explicitly now.
#  2. No aggregate cap: every position individually clearing the quality
#     bar above would carry, with no limit on how much of the pool sits
#     exposed to overnight gap risk at once. This caps total carried
#     value (ranked by combined quality score, highest first) at this %
#     of total_allocated_capital; positions beyond the cap are
#     squared off instead of carried, same pattern real-trade-service
#     already uses for its own OVERNIGHT_HOLD_MAX_EXPOSURE_PCT.
OVERNIGHT_HOLD_MAX_EXPOSURE_PCT_OF_POOL = _get_float("OVERNIGHT_HOLD_MAX_EXPOSURE_PCT_OF_POOL", 30.0)

# ── Overnight protective stop (2026-09-19, option 3 fix) ─────────────────────
# After a position is converted INTRADAY -> CNC for overnight carry, the
# old bracket's stop/target legs are cancelled (they were INTRADAY-product
# orders and cannot protect a CNC holding). This config controls the
# STOP_LOSS_MARKET order placed immediately after conversion as a genuine
# protective stop — the trigger price is set at entry_price * (1 -
# OVERNIGHT_STOP_LOSS_PCT / 100.0), rounded to the nearest valid tick.
#
# STOP_LOSS_MARKET mechanics (verified against dhanhq SDK 2.2.0, _order.py):
#   order_type  = "STOP_LOSS_MARKET"
#   price       = 0          (no limit price — fill at market once triggered)
#   trigger_price = <level>  (exchange activates the order when LTP <= this)
#   product_type  = "CNC"    (must match the converted holding)
#   transaction_type = "SELL"
# The SDK's place_order() accepts trigger_price as an explicit kwarg and
# passes it as "triggerPrice" in the REST payload — confirmed in _order.py
# line 83 / line 126. Dhan's server then holds this as a passive order in
# the order book (status "PENDING" until triggered), NOT as an immediate
# market sell. Post-placement, we read the order back via get_order_list()
# and verify the broker echoes it as STOP_LOSS_MARKET / PENDING (or
# TRANSIT) before committing overnight_stop_order_id — if that check fails
# we do NOT carry the position and square it off instead.
#
# Default 4 % stop below entry — adjust to taste.  6 % is a common
# overnight gap-risk budget for large-cap Indian equities; 4 % is tighter
# (smaller loss if wrong, but also more vulnerable to a morning shake-out
# before the real move).  Set to 0 to disable protective-stop placement
# while keeping the conversion itself (NOT recommended — leaving a CNC
# holding unprotected overnight is the original bug this fixes).
OVERNIGHT_STOP_LOSS_PCT = _get_float("OVERNIGHT_STOP_LOSS_PCT", 4.0)

# 2026-09-19 (audit finding): pre-market CDSL eDIS check — see
# execution/dhan_client.py's edis_verification_summary and main.py's
# scheduled call to it. Same reasoning as real-trade-service's own
# EDIS_MORNING_CHECK_ENABLED.
EDIS_MORNING_CHECK_TIME_IST = _get_str("EDIS_MORNING_CHECK_TIME_IST", "09:00")

# ── Manual exit: cancel-then-sell delay (2026-09-18, session67) ─────────────
# close_position_now() cancels all super-order legs, then immediately fires
# a plain MARKET SELL. If the cancel hasn't propagated on Dhan's side yet
# the SELL can race against a still-live exit leg and get rejected or
# partially double-fill. A small sleep between cancel and sell gives the
# broker time to acknowledge the cancellation. 0 = no delay (original behaviour).
MANUAL_EXIT_CANCEL_WAIT_S = _get_float("MANUAL_EXIT_CANCEL_WAIT_S", 0.5)

# ── Quality-gate cache fallback (session68) ─────────────────────────────────
# screening/quality_gate.py is fail-open, always — a timeout or non-200
# leaves a field None, treated as "unknown = pass" (by design, so a slow
# analysis-intelligence-service never blocks the scalp loop). Confirmed root
# cause of MANGALAM/GEEKAYWIRE/JISLJALEQS (all ₹30-32, thin liquidity)
# entering on 2026-09-17 despite the MIN_MARKET_CAP_CR floor. When a live
# fetch is missing a field, the gate now falls back to the last value this
# service successfully fetched for that symbol (models.ScalpQualityCache),
# as long as it isn't older than QUALITY_CACHE_MAX_AGE_HOURS — live data
# always wins when present; this only fills the gap on a timeout. A symbol
# with no prior successful fetch (first time ever seen) still fails open,
# unchanged — this removes the blind spot for repeat offenders only.
QUALITY_CACHE_MAX_AGE_HOURS = _get_float("QUALITY_CACHE_MAX_AGE_HOURS", 48.0)

# ── Stagnation early-exit tuning (session68/69) ─────────────────────────────
# 2026-09-17: MANGALAM/GEEKAYWIRE/JISLJALEQS all sat within a tiny P&L band
# the entire session (target/stop never triggered) and only closed at 15:00
# EOD squareoff, while TREL (fund=49, tech=78 — a genuinely decent
# candidate) kept hitting INSUFFICIENT_CAPITAL / MAX_CONCURRENT_SCALP_
# POSITIONS the whole day. A position that hasn't moved meaningfully in
# STAGNATION_EXIT_MINUTES is dead capital with zero edge left — closing it
# early frees that capital/slot for a better candidate the SAME session
# instead of parking it until EOD for no reason.
# STAGNATION_EXIT_BAND_PCT is the ± move (from entry) still considered
# "flat"; a position that HAS moved past this band is left alone — target/
# stop logic already owns that case.
# The on/off switch itself (session69) moved OUT of config/env and onto
# ScalpGateState.stagnation_exit_enabled — a DB-backed runtime toggle set
# via POST /stagnation-exit/enable|disable (frontend button on the Pipeline
# tab), same pattern as auto_pilot_enabled/service_enabled, so it can be
# flipped without a redeploy. These two numbers stay config.py-only tuning
# knobs, same as MAX_ENTRY_RANGE_POSITION etc.
STAGNATION_EXIT_MINUTES = _get_float("STAGNATION_EXIT_MINUTES", 30.0)  # 2026-10-05: 45 -> 30
STAGNATION_EXIT_BAND_PCT = _get_float("STAGNATION_EXIT_BAND_PCT", 0.35)

# ── No-follow-through exit (2026-10-05, scalp review) ───────────────────────
# A trade that never shows follow-through bleeds charges plus a small loss until
# the stagnation window (or the stop) closes it. If a position has not reached
# +NO_FOLLOWTHROUGH_MIN_GAIN_PCT at its best within NO_FOLLOWTHROUGH_EXIT_MINUTES,
# close it and free the slot. Runs inside orders/eod_squareoff.py::
# run_stagnation_exit, so it shares that function's DB-backed on/off switch
# (ScalpGateState.stagnation_exit_enabled) and its STAGNATION_EXIT status.
# NO_FOLLOWTHROUGH_EXIT_ENABLED=false turns just this rule off.
NO_FOLLOWTHROUGH_EXIT_ENABLED = _get_bool("NO_FOLLOWTHROUGH_EXIT_ENABLED", True)
NO_FOLLOWTHROUGH_EXIT_MINUTES = _get_float("NO_FOLLOWTHROUGH_EXIT_MINUTES", 20.0)
NO_FOLLOWTHROUGH_MIN_GAIN_PCT = _get_float("NO_FOLLOWTHROUGH_MIN_GAIN_PCT", 0.5)

# ── Breakeven stop buffer (2026-09-18 audit) ────────────────────────────────
# orders/breakeven.py previously moved the STOP_LOSS_LEG to EXACTLY
# entry_price when a position's unrealized gain crossed its trigger. A
# position that then round-trips back to entry realizes a small NET LOSS
# after brokerage/slippage on the exit SELL — not a true breakeven. Moving
# the stop a couple of ticks above entry (still below the current price by
# construction, same clamp orders/adaptive.py already applies) means a
# worst-case round-trip exit realizes ~flat instead of a guaranteed small
# loss. 0 = restore the original exact-entry behaviour.
BREAKEVEN_STOP_BUFFER_TICKS = int(_get_float("BREAKEVEN_STOP_BUFFER_TICKS", 2))

# 2026-10-05: breakeven used to trigger at 40% of target (~1.8% on a 4.4%
# target), which these trades rarely reached. The trigger is now
# min(40% of target, BREAKEVEN_TRIGGER_MAX_PCT). 0 disables the cap.
# (Only affects positions opened after this change - the trigger is stored per
# position at entry. The breakeven feature itself is still the DB-backed
# ScalpGateState.breakeven_stop_enabled toggle, OFF by default.)
BREAKEVEN_TRIGGER_MAX_PCT = _get_float("BREAKEVEN_TRIGGER_MAX_PCT", 1.0)

# ── Scalp ranking v2 (2026-10-07, group 219) ─────────────────────────────────
# The composite score was pct_change x volume_weight x ... . volume_weight = min(day_volume / MIN_AVG_VOLUME, 3), so
# once a stock passed 3x the floor (150k shares at the 50k default, true for almost every symbol soon after the open)
# every candidate got the same weight, and the score was just "biggest mover first", which is the move most likely to
# be over. v2 (screening/engine.py): (1) the move's contribution is capped at SCAN_PCT_CAP_MULT x the window's own
# threshold, so size alone stops winning and the quality terms (range position, VWAP extension, consistency, volume
# pace) decide among the strong movers; (2) the saturated volume weight is replaced by a mild liquidity factor
# (day volume vs 3x the floor, max 1.0) times a volume-PACE weight: this window's volume per minute against the
# symbol's own pace earlier in the session. Unknown pace (early session, no history after a restart) is neutral 1.0.
# Candidate.pct_change is still the real move; only the score changes. SCAN_RANKING_V2_ENABLED=0 restores the old formula.
#   SCAN_PCT_CAP_MULT              0 disables the cap
#   SCAN_RVOL_MIN_WEIGHT / MAX     clamp for the volume-pace weight
#   SCAN_RVOL_MIN_BASELINE_MIN     minutes of session needed before a window starts for its baseline to be trusted
#   SCAN_RVOL_SAMPLE_S             spacing of the cumulative-volume snapshots kept per symbol
SCAN_RANKING_V2_ENABLED = _get_bool("SCAN_RANKING_V2_ENABLED", True)
SCAN_PCT_CAP_MULT = _get_float("SCAN_PCT_CAP_MULT", 3.0)
SCAN_RVOL_MIN_WEIGHT = _get_float("SCAN_RVOL_MIN_WEIGHT", 0.5)
SCAN_RVOL_MAX_WEIGHT = _get_float("SCAN_RVOL_MAX_WEIGHT", 2.0)
SCAN_RVOL_MIN_BASELINE_MIN = _get_float("SCAN_RVOL_MIN_BASELINE_MIN", 10.0)
SCAN_RVOL_SAMPLE_S = _get_float("SCAN_RVOL_SAMPLE_S", 5.0)

# ── Entry cost gate (2026-10-07, group 218) ──────────────────────────────────
# real-trade-service has had an edge-vs-transaction-cost gate since 2026-09-18 (its cost_model.py, Gate 5.6); this
# service had none, so a scalp whose target barely covered its own costs was bought anyway. orders/cost_gate.py is a
# standalone INTRADAY copy of that estimate (services do not share code at runtime). The statutory rates below use the
# SAME env names as real-trade-service, so one value in .env applies to both. Defaults assume Dhan's Rs 0 brokerage
# plan, exactly as real-trade-service does - set BROKERAGE_PER_ORDER from a real contract note if that is not true.
#   SCALP_COST_GATE_ENABLED            0 turns the gate off
#   SCALP_MIN_EDGE_TO_COST_RATIO       expected Rs edge at the TARGET must be at least this multiple of round-trip cost
#   SCALP_COST_SLIPPAGE_ALLOWANCE_PCT  extra round-trip cost (% of trade value) for spread / slippage, on top of the levies
SCALP_COST_GATE_ENABLED = _get_bool("SCALP_COST_GATE_ENABLED", True)
SCALP_MIN_EDGE_TO_COST_RATIO = _get_float("SCALP_MIN_EDGE_TO_COST_RATIO", 3.0)
SCALP_COST_SLIPPAGE_ALLOWANCE_PCT = _get_float("SCALP_COST_SLIPPAGE_ALLOWANCE_PCT", 0.10)
BROKERAGE_PER_ORDER = _get_float("BROKERAGE_PER_ORDER", 0.0)              # flat Rs per executed leg
STT_INTRADAY_SELL_PCT = _get_float("STT_INTRADAY_SELL_PCT", 0.025)         # SELL leg only
# group 262: Dhan's published NSE equity card = 0.00297 % exchange transaction charge (since 1 Oct 2024; was 0.00325 here,
# 0.00345 on the dashboards) + 0.0001 % IPFT levy, GST on both. Same env names/semantics as real-trade-service.
EXCHANGE_TXN_PCT = _get_float("EXCHANGE_TXN_PCT", 0.00297)                 # both legs, NSE transaction charge
IPFT_PCT = _get_float("IPFT_PCT", 0.0001)                                  # both legs, IPFT levy
SEBI_TURNOVER_PCT = _get_float("SEBI_TURNOVER_PCT", 0.0001)                # both legs
GST_PCT = _get_float("GST_PCT", 18.0)                                      # on brokerage + exchange + IPFT + SEBI
STAMP_DUTY_BUY_PCT_INTRADAY = _get_float("STAMP_DUTY_BUY_PCT_INTRADAY", 0.003)  # BUY leg only

# ── Trailing stop (2026-10-07, group 217) ────────────────────────────────────
# Until now a scalp's bracket was fixed: the target (about 1.4-3.5%) or the stop, plus a one-time breakeven move
# that is OFF by default. A winner that ran +1.5% and fell back gave it all up. orders/trailing.py ratchets the
# Super Order's STOP_LOSS_LEG up behind the peak price. It only ever raises the stop, never lowers it, and never
# touches the target. Env switch (no DB toggle, so no schema change): TRAILING_STOP_ENABLED=0 turns it off.
#   TRAIL_ACTIVATE_PCT           peak gain vs entry needed before the trail starts
#   TRAIL_DISTANCE_STOP_FRACTION trail distance = this fraction of the position's own adaptive stop %...
#   TRAIL_MIN_DISTANCE_PCT       ...but never tighter than this % below the peak
#   TRAIL_MIN_STEP_PCT           only modify the leg when the new stop is at least this % of entry above the old one
#   TRAIL_MIN_INTERVAL_S         per-position minimum seconds between modify attempts
#   TRAIL_RETRY_BACKOFF_S        per-position wait after a rejected modify
TRAILING_STOP_ENABLED = _get_bool("TRAILING_STOP_ENABLED", True)
TRAIL_ACTIVATE_PCT = _get_float("TRAIL_ACTIVATE_PCT", 1.0)
TRAIL_DISTANCE_STOP_FRACTION = _get_float("TRAIL_DISTANCE_STOP_FRACTION", 0.6)
TRAIL_MIN_DISTANCE_PCT = _get_float("TRAIL_MIN_DISTANCE_PCT", 0.4)
TRAIL_MIN_STEP_PCT = _get_float("TRAIL_MIN_STEP_PCT", 0.15)
TRAIL_MIN_INTERVAL_S = _get_float("TRAIL_MIN_INTERVAL_S", 20.0)
TRAIL_RETRY_BACKOFF_S = _get_float("TRAIL_RETRY_BACKOFF_S", 60.0)

# this session: user asked for Trade History to only retain "today" /
# "last 3 days" and for the ledger to actually only store that much —
# orders/reconcile.py::run_retention_cleanup() deletes CLOSED positions
# (never OPEN/EXIT_LEGS_REJECTED — those are live exposure, never auto-
# deleted regardless of age) whose closed_at is older than this many days.
# Runs at most once per IST calendar day (see main.py's fast-reconcile
# loop + ScalpGateState.retention_cleanup_last_run_date). A manual
# POST /trades/cleanup is also available for an on-demand run.
TRADE_HISTORY_RETENTION_DAYS = _get_float("TRADE_HISTORY_RETENTION_DAYS", 3.0)

# ── Cumulative brokerage ledger (2026-10-08, group 258) ──────────────────────
# orders/charges_ledger.py books every settled closed position into scalp_charges_ledger (never purged by the
# retention job above) so the Charges tab can show brokerage since the start. Rate card = the one the Charges
# tab uses (Rs 20 or 0.03% per executed leg, whichever is lower). NOTE: BROKERAGE_PER_ORDER (default 0, used by
# the entry cost gate) assumes Dhan's Rs 0 plan; these two feed no gate, they only measure.
# Set both to 0 if your Dhan contract note shows no brokerage on intraday.
CHARGES_BROKERAGE_PCT = _get_float("CHARGES_BROKERAGE_PCT", 0.03)       # % of leg value
CHARGES_BROKERAGE_CAP_RS = _get_float("CHARGES_BROKERAGE_CAP_RS", 20.0)  # flat cap per leg

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

# ── Entry slippage / day-gain guards (2026-10-05, scalp review) ─────────────
# UNITEDPOLY: signal LTP 44.58, broker filled 48.14 (~8% higher) - the entry is
# a pure MARKET order sent some seconds after the scan tick. Right before the
# order goes out, orders/entry.py re-reads the live tick and aborts when it is
# more than ENTRY_MAX_SLIPPAGE_PCT above the signal price, or when the last
# tick is older than ENTRY_MAX_TICK_AGE_S (current price unknown). 0 disables
# each check. Both fail open when there is no tick at all.
# 2026-10-07 (group216): default 0.5 -> 0.25. The adaptive stop is only 0.8-2.0%, so a 0.5% tolerance let one entry
# give away up to ~60% of the tightest stop before the trade started. Set ENTRY_MAX_SLIPPAGE_PCT=0.5 to restore.
ENTRY_MAX_SLIPPAGE_PCT = _get_float("ENTRY_MAX_SLIPPAGE_PCT", 0.25)
ENTRY_MAX_TICK_AGE_S = _get_float("ENTRY_MAX_TICK_AGE_S", 45.0)
# group280 (plan Phase C3): Dhan 5-level depth read from market-data /quote just before an entry. Reject a name whose
# bid-ask spread is above ENTRY_DEPTH_MAX_SPREAD_PCT (0 = off) or whose best-5 book (both sides, Rs) is below
# ENTRY_MIN_BOOK_VALUE (0 = off). Unknown depth (no answer, timeout, no Dhan depth) never blocks. ENTRY_DEPTH_GATE=0 = off.
ENTRY_DEPTH_GATE = _get_bool("ENTRY_DEPTH_GATE", True)
ENTRY_DEPTH_MAX_SPREAD_PCT = _get_float("ENTRY_DEPTH_MAX_SPREAD_PCT", 0.5)
ENTRY_MIN_BOOK_VALUE = _get_float("ENTRY_MIN_BOOK_VALUE", 0.0)
ENTRY_DEPTH_TIMEOUT_S = _get_float("ENTRY_DEPTH_TIMEOUT_S", 2.0)
# group292: age limit for the market-data /quote (and /depth) book the depth gate judges on. A book older than this is
# re-read once, then treated as unknown depth (never blocks, never sizes). 0 = off. Needs the group 289 market-data
# build: an older market-data sends no `age_s`, which is "unknown" and follows ENTRY_DEPTH_QUOTE_AGE_UNKNOWN
# (allow = use it as before, refuse = treat as unknown depth).
ENTRY_DEPTH_MAX_QUOTE_AGE_S = _get_float("ENTRY_DEPTH_MAX_QUOTE_AGE_S", 20.0)
ENTRY_DEPTH_QUOTE_AGE_UNKNOWN = (os.getenv("ENTRY_DEPTH_QUOTE_AGE_UNKNOWN") or "allow").strip().lower()
# group283 (plan C3, size down): cap an entry's order value at ENTRY_BOOK_MAX_SHARE_PCT % of ONE side of the best-5 book
# (book_value_5 / 2), so a small cap's order does not eat its own touch. 0 = off (default). Unknown depth never shrinks.
# Needs ENTRY_DEPTH_GATE on (it reuses the same market-data /quote read).
ENTRY_BOOK_MAX_SHARE_PCT = _get_float("ENTRY_BOOK_MAX_SHARE_PCT", 0.0)
# group286 (plan C3 size-down, 20 levels): cap an entry at ENTRY_DEPTH20_MAX_SHARE_PCT % of the ask-side shares Dhan's 20-level
# book shows within ENTRY_DEPTH20_SLIP_PCT % of the best ask. 0 = off (default). Needs market-data DHAN_DEPTH20_ENABLED=1 and
# ENTRY_DEPTH_GATE on; when market-data has no 20-level book yet (warming, stale, off) the order is not shrunk.
# ENTRY_DEPTH20_WAIT_S = how long the first ask for a symbol waits for its book (adds up to that to the entry).
ENTRY_DEPTH20_SLIP_PCT = _get_float("ENTRY_DEPTH20_SLIP_PCT", 0.0)
ENTRY_DEPTH20_MAX_SHARE_PCT = _get_float("ENTRY_DEPTH20_MAX_SHARE_PCT", 50.0)
ENTRY_DEPTH20_WAIT_S = _get_float("ENTRY_DEPTH20_WAIT_S", 1.0)
# group280 (plan Phase C1): read-only Dhan order-update WebSocket (execution/order_ws.py). Off by default; nothing in the
# trading path depends on it. See GET /orders/ws-status.
DHAN_ORDER_WS_ENABLED = _get_bool("DHAN_ORDER_WS_ENABLED", False)
# group287: when the order-book dead-parent check proves nothing but the order-update WebSocket saw the entry BUY fully
# TRADED, keep the row OPEN instead of booking ERROR. Default 0 (the disagreement is only logged as WS_CROSSCHECK) until the
# event shapes are confirmed live. Needs DHAN_ORDER_WS_ENABLED=1.
RECONCILE_USE_ORDER_EVENTS = _get_bool("RECONCILE_USE_ORDER_EVENTS", False)
# Reject stocks already up more than this % on the day vs the exchange's
# previous close (mode-3 feed). 0 disables. Fails open when no previous close.
MAX_DAY_GAIN_PCT = _get_float("MAX_DAY_GAIN_PCT", 7.0)
# Log + Telegram alert when the real entry fill differs from the signal price
# by more than this % (stale tick, wrong order matched). 0 disables.
# group216: alert default 1.0 -> 0.5 so a fill that cost more than half the tightest stop is visible. 0 disables.
ENTRY_FILL_SLIPPAGE_ALERT_PCT = _get_float("ENTRY_FILL_SLIPPAGE_ALERT_PCT", 0.5)


def _parse_int_set(raw) -> frozenset:
    out = set()
    for part in str(raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return frozenset(out)


# Scan windows paused (comma-separated minutes). 2026-10-05 review: 15m made 7
# of 11 trades at -Rs38 (bought after the move, no follow-through); 1m is the
# noisiest. 5m and 60m stay on. DISABLED_SCAN_WINDOWS=none re-enables all.
DISABLED_SCAN_WINDOWS = _parse_int_set(_get_str("DISABLED_SCAN_WINDOWS", "1,15"))

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

# 2026-10-06 (group192): a Super Order is ACCEPTED by Dhan's API and then rejected a moment later by RMS
# (e.g. "not allowed to be traded in Intraday"), so the rejection only shows up in orders/reconcile.py as a
# dead ENTRY_LEG, long after attempt_entry() returned. HEGAM was re-bought 13 times in four minutes because
# nothing remembered those rejections. After a dead entry the symbol is skipped for
# ENTRY_REJECT_COOLDOWN_MINUTES, and once it has had ENTRY_REJECT_MAX_PER_SYMBOL_DAY dead entries today it is
# skipped for the rest of the day. 0 disables the respective guard.
ENTRY_REJECT_COOLDOWN_MINUTES = _get_int("ENTRY_REJECT_COOLDOWN_MINUTES", 30)
ENTRY_REJECT_MAX_PER_SYMBOL_DAY = _get_int("ENTRY_REJECT_MAX_PER_SYMBOL_DAY", 2)

# 2026-10-06 (group209, item 15): a margin / insufficient-funds rejection is about the account, not the symbol,
# so it pauses ALL new entries for this many minutes (the next candidate would be rejected the same way) and does
# not count against the symbol's own ENTRY_REJECT_* limits. An unclassified BUY placement failure rests just that
# symbol for the second knob. 0 disables the respective pause. See orders/entry_pause.py.
ENTRY_MARGIN_PAUSE_MINUTES = _get_int("ENTRY_MARGIN_PAUSE_MINUTES", 5)
# Group 210 (item 14): periodic sweep of symbol locks that no live position backs (0 = off). A claim younger
# than the min age is never swept (its position row may not exist yet).
SYMBOL_LOCK_SWEEP_INTERVAL_S = _get_float("SYMBOL_LOCK_SWEEP_INTERVAL_S", 60.0)
# GROUP 232: how often (seconds) today's closed rows are re-checked against Dhan's real entry/exit
# fills and corrected (orders/reconcile.py::auto_repair_closed_entry_prices). 0 = off.
ENTRY_REPAIR_AUTO_INTERVAL_S = _get_float("ENTRY_REPAIR_AUTO_INTERVAL_S", 300.0)
SYMBOL_LOCK_SWEEP_MIN_AGE_S = _get_float("SYMBOL_LOCK_SWEEP_MIN_AGE_S", 600.0)
ENTRY_ORDER_FAILED_COOLDOWN_MINUTES = _get_int("ENTRY_ORDER_FAILED_COOLDOWN_MINUTES", 5)

# ── Shared Dhan account-wide order-rate budget (tracking doc §3.8) ─────────
# Dhan's own account-wide cap is roughly 5,000-7,000 orders/day, shared with
# real-trade-service (same Dhan account). This is a soft, fail-open governor
# — see capital/shared_order_budget.py's docstring and models.py's
# SharedOrderBudget for the full rationale.
SHARED_DAILY_ORDER_BUDGET = _get_int("SHARED_DAILY_ORDER_BUDGET", 5000)

LOG_LEVEL = _get_str("LOG_LEVEL", "INFO")

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
_ADMIN_HASH_B64 = (os.getenv("ADMIN_PASSWORD_HASH_B64") or "").strip()
if _ADMIN_HASH_B64 and not (os.getenv("ADMIN_PASSWORD_HASH") or "").strip():
    try:
        import base64 as _b64
        ADMIN_PASSWORD_HASH = _b64.b64decode(_ADMIN_HASH_B64).decode("utf-8").strip()
    except Exception:
        ADMIN_PASSWORD_HASH = ""
else:
    ADMIN_PASSWORD_HASH = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
ADMIN_USERNAME = _get_str("ADMIN_USERNAME", "admin")

SESSION_SECRET = (os.getenv("SESSION_SECRET") or "").strip()
SESSION_IDLE_TIMEOUT_MINUTES = _get_int("SESSION_IDLE_TIMEOUT_MINUTES", 30)

# ── Notifications (session41 fix — STATUS.md open item #7) ─────────────
# SAME env vars / SAME notification-scheduler-service routing convention
# as real-trade-service's config.py + notifier.py, duplicated here per
# this file's isolation note at the top (this service must not import
# real-trade-service at runtime). Lets the existing CRITICAL log lines in
# execution/dhan_client.py and orders/reconcile.py (order-type mismatches,
# dead EOD SELLs, unresolvable legacy exit-order backfills) actually reach
# a human instead of sitting log-only.
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
NOTIFICATION_SERVICE_URL = _env_url("NOTIFICATION_SERVICE_URL", "http://notification-scheduler-service:8000/notification",)

# ── Cross-service PnL sync (Issue #2 fix) ──────────────────────────────
# Used by capital/ledger.py's sync_peer_pnl() to fetch real-trade-service's
# realized_pnl_today so position-stocks' daily-loss kill switch is aware of
# losses booked by the PEER service on the same shared Dhan account.
# Read-only, no auth — only /status/REAL is hit, which is a public endpoint.
REAL_TRADE_SERVICE_URL = os.getenv(
    "REAL_TRADE_SERVICE_URL",
    "http://real-trade-service:8005",
).rstrip("/")

# ── Session 72 (open-issues sweep) ─────────────────────────────────────────
# #8: after a capital-sizing skip (INSUFFICIENT_CAPITAL / _FOR_MIN_QTY) a
# symbol is not re-attempted for this many seconds, unless available capital
# has since grown by CAPITAL_STARVED_RETRY_ON_GROWTH_PCT (a position closed).
# Previously the same unaffordable symbol was retried every 10s cycle (TREL
# 16x in 7 minutes), spamming candidate-log rows and quality-gate HTTP calls.
CAPITAL_STARVED_COOLDOWN_S = _get_float("CAPITAL_STARVED_COOLDOWN_S", 120.0)
CAPITAL_STARVED_RETRY_ON_GROWTH_PCT = _get_float("CAPITAL_STARVED_RETRY_ON_GROWTH_PCT", 25.0)
# #5: a *_PENDING_RECONCILE sentinel that still cannot be resolved this many
# days after the row closed is rewritten to *_UNRESOLVED (explicit, no longer
# "pending forever") and alerted once. See orders/reconcile.py::resolve_stuck_pending.
# group163 (item 4): cheap checks BEFORE the quality gate. The gate costs up to
# QUALITY_GATE_TOP_N HTTP calls per cycle (~13 s), and used to run even when
# nothing could be entered (SATIN, COMSYN: unaffordable; or every slot full).
# 0 restores the old order (quality gate first, affordability/slots checked in attempt_entry).
ENTRY_PRECHECK = _get_bool("ENTRY_PRECHECK", True)
PENDING_RECONCILE_MAX_AGE_DAYS = _get_int("PENDING_RECONCILE_MAX_AGE_DAYS", 3)
PENDING_RECONCILE_SWEEP_INTERVAL_S = _get_float("PENDING_RECONCILE_SWEEP_INTERVAL_S", 600.0)

# ── Opening-quality gate (group 268, 2026-10-09) ─────────────────────────────
# The entry window now opens at 09:15 (ENTRY_NO_BEFORE_IST default, see main.py) instead of 09:30. The 14-day trade
# breakdown showed entries before 10:30 won 1 of 10 (-Rs 154 gross), so instead of a fixed clock block every entry between
# the open and OPENING_GATE_SETTLE_IST must pass screening/opening_gate.py (gap vs previous close, holding above the day's
# open and the previous close, opening-range position, day-range position, Nifty direction). After the settle time the
# normal rules apply unchanged. Missing data before the settle time FAILS CLOSED (the entry is skipped), unlike the other
# gates. OPENING_GATE_ENABLED=0 turns the whole gate off (the 09:15 start then lets every normal rule through at the open).
OPENING_GATE_ENABLED = _get_bool("OPENING_GATE_ENABLED", True)
OPENING_GATE_SETTLE_IST = _get_str("OPENING_GATE_SETTLE_IST", "10:00")
OPENING_GATE_MIN_MINUTES_AFTER_OPEN = _get_float("OPENING_GATE_MIN_MINUTES_AFTER_OPEN", 5.0)   # no entry in the first minutes
OPENING_GATE_MAX_GAP_UP_PCT = _get_float("OPENING_GATE_MAX_GAP_UP_PCT", 3.0)       # open vs previous close; 0 disables
OPENING_GATE_MAX_GAP_DOWN_PCT = _get_float("OPENING_GATE_MAX_GAP_DOWN_PCT", 1.0)   # gap DOWN larger than this is skipped; 0 disables
OPENING_GATE_MAX_RANGE_POS = _get_float("OPENING_GATE_MAX_RANGE_POS", 0.88)        # stricter than MAX_ENTRY_RANGE_POSITION; 0 disables
OPENING_GATE_OR_MINUTES = _get_int("OPENING_GATE_OR_MINUTES", 15)                  # opening range = first N minutes from 09:15
OPENING_GATE_MIN_OR_POS = _get_float("OPENING_GATE_MIN_OR_POS", 0.5)               # after the OR is built: price in its upper half or above it
OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT = _get_float("OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT", 1.0)  # ...but not extended more than this above OR high
OPENING_GATE_MIN_OR_TICKS = _get_int("OPENING_GATE_MIN_OR_TICKS", 10)             # fewer opening-range ticks (e.g. after a restart) = skip
OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT = _get_float("OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT", 0.0)
OPENING_GATE_NIFTY_MIN_VS_PREV_PCT = _get_float("OPENING_GATE_NIFTY_MIN_VS_PREV_PCT", 0.0)

# group 269: shadow mode. OPENING_GATE_SHADOW=1 -> while the gate is active (09:15 until the settle time) NO entry is placed;
# a symbol that passes every gate check is logged once per day as SKIPPED "OPENING_SHADOW:WOULD_ENTER ltp=..." (with the same
# gap/range/OR/Nifty numbers as an ENTERED row) so the 09:15 entries can be judged against later prices before real money is
# used. Rejected symbols are logged as before. Default 0 = the gate is live (group 268 behaviour).
OPENING_GATE_SHADOW = _get_bool("OPENING_GATE_SHADOW", False)

# ── Opening gate: previous-day candle checks (group 270, 2026-10-09) ─────────
# Read from market-data-service /history (daily candles) by a background thread (screening/prev_day.py), never on the entry
# path. Inside the gate window (09:15 - OPENING_GATE_SETTLE_IST) the entry is skipped when the data is not cached yet (fails
# closed; the next 10 s scan finds it).
MARKET_DATA_URL = _env_url("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com")
OPENING_GATE_PREVDAY_TIMEOUT_S = _get_float("OPENING_GATE_PREVDAY_TIMEOUT_S", 8.0)
OPENING_GATE_MIN_PREVDAY_CLOSE_POS = _get_float("OPENING_GATE_MIN_PREVDAY_CLOSE_POS", 0.5)   # previous day closed in the upper half of its range; 0 disables
OPENING_GATE_MIN_STOP_ATR_FRAC = _get_float("OPENING_GATE_MIN_STOP_ATR_FRAC", 0.3)           # stop % must be >= this x daily ATR %; 0 disables
OPENING_GATE_PREVDAY_MAX_AGE_DAYS = int(_get_float("OPENING_GATE_PREVDAY_MAX_AGE_DAYS", 6.0))   # group 273: a last daily candle older than this is treated as no data (stale history); 6 covers a long weekend + holiday
