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
EDIS_MORNING_CHECK_TIME_IST = os.getenv("EDIS_MORNING_CHECK_TIME_IST", "09:00")

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
STAGNATION_EXIT_MINUTES = _get_float("STAGNATION_EXIT_MINUTES", 45.0)
STAGNATION_EXIT_BAND_PCT = _get_float("STAGNATION_EXIT_BAND_PCT", 0.35)

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

# this session: user asked for Trade History to only retain "today" /
# "last 3 days" and for the ledger to actually only store that much —
# orders/reconcile.py::run_retention_cleanup() deletes CLOSED positions
# (never OPEN/EXIT_LEGS_REJECTED — those are live exposure, never auto-
# deleted regardless of age) whose closed_at is older than this many days.
# Runs at most once per IST calendar day (see main.py's fast-reconcile
# loop + ScalpGateState.retention_cleanup_last_run_date). A manual
# POST /trades/cleanup is also available for an on-demand run.
TRADE_HISTORY_RETENTION_DAYS = _get_float("TRADE_HISTORY_RETENTION_DAYS", 3.0)

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