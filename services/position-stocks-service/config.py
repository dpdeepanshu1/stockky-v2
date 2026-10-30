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

# LOG FIX (2026-10-12): "QueuePool limit of size 2 overflow 2 reached" killed a
# trading-loop cycle because the shared .env pool tuning (2+1) is far too small
# for this service (loop + pre-open + ledger + API threads all share it). Floor
# it so a low shared setting can never starve the loop again.
DB_POOL_SIZE = max(4, _get_int("DB_POOL_SIZE", 4))
DB_MAX_OVERFLOW = max(4, _get_int("DB_MAX_OVERFLOW", 4))
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
# E255: AngelOne only streams ~1000 tokens per connection; open ceil(tokens/1000) connections, at most this many (SmartAPI limit is 3).
ANGELONE_WS_MAX_CONNECTIONS = max(1, min(3, _get_int("ANGELONE_WS_MAX_CONNECTIONS", 3)))
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

# ── Tick source (group301) ───────────────────────────────────────────────────
# "angelone_ws" (default) = this service's own AngelOne SmartWebSocketV2 feed (feed/ws_client.py, unchanged).
# "market_data"           = poll market-data-service POST /quotes/bulk instead (feed/md_poller.py), so the scalper
#                           runs on whatever provider order market-data has (Dhan first when QUOTE_PROVIDER_ORDER says so)
#                           and this service needs no AngelOne login or WS connection. Anything else = "angelone_ws".
# The poller sends one request per POSITION_BULK_POLL_S: open positions first (every request), then the next
# POSITION_BULK_CHUNK symbols of the universe, round robin. Dhan allows 1 bulk request per second.
POSITION_FEED_SOURCE = _get_str("POSITION_FEED_SOURCE", "angelone_ws").lower()
# E255: "hybrid" = AngelOne WS (up to 3 connections) as the primary feed PLUS the Dhan bulk poller as a gap filler: it only polls
# open positions and symbols the WS has not ticked for POSITION_HYBRID_STALE_S seconds (or never), so Dhan covers anything AngelOne misses.
if POSITION_FEED_SOURCE not in ("angelone_ws", "market_data", "hybrid"):
    POSITION_FEED_SOURCE = "angelone_ws"
POSITION_HYBRID_STALE_S = max(2.0, _get_float("POSITION_HYBRID_STALE_S", 20.0))
POSITION_BULK_POLL_S = max(0.2, _get_float("POSITION_BULK_POLL_S", 1.0))
POSITION_BULK_CHUNK = min(1000, max(1, _get_int("POSITION_BULK_CHUNK", 500)))
POSITION_BULK_TIMEOUT_S = max(1.0, _get_float("POSITION_BULK_TIMEOUT_S", 8.0))
POSITION_BULK_MAX_AGE_S = max(1.0, _get_float("POSITION_BULK_MAX_AGE_S", 30.0))

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

# ── Market Weather (market-state layer, 2026-10-23) ──────────────────────────────────────────────────────────────────────────────────
# MARKET_STATE_MODE: off | shadow | live (ships shadow). The calibration-runner may only TIGHTEN, for one day, through control/kit.py overlays,
# and only while the mode is `live`. MARKET_STATE_RISK_MULT (0.1 .. 1.0, ships 1.0) multiplies RISK_PER_TRADE_PCT in capital/ledger.py at call time.
def _ms_float(name: str, default: float, lo: float, hi: float) -> float:
    v = _get_float(name, default)
    if v != v:                       # NaN
        return float(default)
    return min(hi, max(lo, v))


def _ms_mode(name: str = "MARKET_STATE_MODE", default: str = "shadow") -> str:
    v = _get_str(name, default).lower()
    return v if v in ("off", "shadow", "live") else default


MARKET_STATE_MODE = _ms_mode()
MARKET_STATE_RISK_MULT = _ms_float("MARKET_STATE_RISK_MULT", 1.0, 0.1, 1.0)

# ── Short selling (Part 2 step 2, 2026-10-23) ──────────────────────────────────────────────────────────────────────────────────────────
# SHORT_MODE: off | shadow | live (ships off). shadow = orders/short_entry.py records would-be shorts, no order. live (short selling step 8) = orders/short_live.py places
# a real SELL Super Order WITH its broker stop leg, sized to at most SHORT_LIVE_QTY_CAP shares. Read by the short cycle, capital/ledger.py and the live entry.
# Anything else reads as `off`. A short that cannot be closed cleanly must never exist, so the exit side (orders/side.py, eod_squareoff, reconcile) came first.
def _short_mode(name: str = "SHORT_MODE", default: str = "off") -> str:
    v = _get_str(name, default).strip().lower()
    return v if v in ("off", "shadow", "live") else default


SHORT_MODE = _short_mode()
# Short selling step 9: the most shares ONE live short may have, whatever the budget says. Ships 1000 since E233 (was 1), so the normal sizing (risk budget, cost gate,
# depth, SHORT_MAX_POSITIONS) decides the size; set 1 for single-share shorts. On the calibrator's PROTECTED list. 1 .. 1000. A junk value reads as 1.
SHORT_LIVE_QTY_CAP = max(1, min(1000, _get_int("SHORT_LIVE_QTY_CAP", 1000)))
# E108 (T6): a live short is refused until the Dhan SELL probe (orders/sell_probe.py, runner job dhan_sell_probe) has a PASS on record. Ships true: SHORT_MODE=live is then one
# switch after the probe, and a mistaken early flip places nothing. false = no probe needed (loosens a rail, so the Switches page asks to confirm).
SHORT_LIVE_REQUIRES_PROBE = _get_bool("SHORT_LIVE_REQUIRES_PROBE", True)
# E114: plain-order short fallback (orders/plain_short.py). When Dhan refuses the SELL Super Order, or USE_SUPER_ORDER is off, the short is opened as a plain MARKET SELL
# and a BUY STOP_LOSS_MARKET order (the broker stop) is placed at once; if that stop cannot be placed the short is covered immediately and no further plain short is tried
# that day. A short never exists without a stop. Only reached when SHORT_MODE=live (the SELL probe rule above still applies). false = the old behaviour: a refused Super
# Order = no short today. The switch gates NEW plain shorts only: the guard that manages an already open plain short keeps running whatever the switch says.
SHORT_PLAIN_FALLBACK = _get_bool("SHORT_PLAIN_FALLBACK", True)
# E243 (28 Oct 2026): the first real SELL probe on NTPC sent a MARKET entry; Dhan's row read back as orderType LIMIT at the last price (300.40), filledQty 0, and it rested
# unfilled for 20 s (a SELL at the last price only matches a buyer at or above it, and the best bid sits below). With this on, a short's SELL entry is a LIMIT a few ticks UNDER
# the last price (a "marketable limit": it fills at the best bid at once, never worse than the cushion). false = the old MARKET entry. Used by the live short and by the SELL
# probe alike, so the probe tests exactly what the live short sends.
SHORT_ENTRY_MARKETABLE_LIMIT = _get_bool("SHORT_ENTRY_MARKETABLE_LIMIT", True)
SHORT_ENTRY_CUSHION_PCT = max(0.05, min(1.0, _get_float("SHORT_ENTRY_CUSHION_PCT", 0.3)))
SHORT_PLAIN_FILL_WAIT_S = max(2.0, min(30.0, _get_float("SHORT_PLAIN_FILL_WAIT_S", 8.0)))      # how long to wait for the SELL to show as filled before cancelling it
SHORT_PLAIN_STOP_GRACE_S = max(1.0, min(120.0, _get_float("SHORT_PLAIN_STOP_GRACE_S", 6.0)))   # price this long past the stop with the broker stop still resting: cover by hand
# E107 (P2 item 17): rank the bearish short candidates against each other (screening/shortlist.py build_short) and try them best-first, so the capital and the
# SHORT_MAX_POSITIONS slots go to the strongest short, not to whichever came first in scan order. Live in shadow AND live mode. It never adds a candidate and never
# loosens a gate: every short still walks the same fail-closed gates. SHORT_SHORTLIST_ENABLED=0 restores scan order. Pool = names quality-checked per cycle.
SHORT_SHORTLIST_ENABLED = _get_bool("SHORT_SHORTLIST_ENABLED", True)
SHORT_SHORTLIST_MAX = max(1, min(10, _get_int("SHORT_SHORTLIST_MAX", 5)))
SHORT_SHORTLIST_EXTENDED_PENALTY = _get_float("SHORT_SHORTLIST_EXTENDED_PENALTY", 15.0)   # already far below its zone: a late short chases the move
# Short selling step 4: a short risks this fraction of what a long risks per trade (capital/ledger.py reserve_capital(side="SHORT")). 0.1 .. 1.0, ships 0.5.
# Only ever lowers risk: 1.0 = same as a long, never above it.
SHORT_RISK_MULT = _ms_float("SHORT_RISK_MULT", 0.5, 0.1, 1.0)
# Short selling step 6 (2026-10-24): the Market Weather short dial. Multiplies a short's risk on top of SHORT_RISK_MULT, read at call time by
# capital/ledger.py and orders/short_entry.py. 0.0 .. 1.0, ships 1.0 (no change); 0 = no short at all today. Only the market-state overlay (tighten-only,
# gone at 15:30 IST or on restart) or a person lowers it. It never touches a long.
MARKET_STATE_SHORT_MULT = _ms_float("MARKET_STATE_SHORT_MULT", 1.0, 0.0, 1.0)

# Short selling step 12 (2026-10-24): a short's stop is tightened to this fraction of its normal distance when Market Weather says stressed / panic (the plan's
# "tighter stop" rows). 0.3 .. 1.0, 1.0 = off. Position size is still worked out from the NORMAL stop, so a tighter stop lowers the rupee risk, never raises the size.
SHORT_STOP_MULT_STRESSED = _ms_float("SHORT_STOP_MULT_STRESSED", 0.8, 0.3, 1.0)
SHORT_STOP_MULT_PANIC = _ms_float("SHORT_STOP_MULT_PANIC", 0.6, 0.3, 1.0)

# Short selling step 5 (shadow entry, 2026-10-23): the mirrored entry rules, read at call time by orders/short_gates.py and orders/short_entry.py.
# None touches a long rule, and with the short mode off none is read at all.
SHORT_MAX_POSITIONS = max(1, min(5, _get_int("SHORT_MAX_POSITIONS", 2)))               # would-be shorts unresolved at once (later: real shorts)
SHORT_ENTRY_START_IST = _get_str("SHORT_ENTRY_START_IST", "09:45")                      # the day's VWAP rests on too few trades before this
SHORT_ENTRY_CUTOFF_IST = _get_str("SHORT_ENTRY_CUTOFF_IST", "13:30")                    # a late short has little time to work and a lot of time to be squeezed
SHORT_SQUAREOFF_TIME_IST = _get_str("SHORT_SQUAREOFF_TIME_IST", "14:55")                # shadow shorts are closed and live shorts covered here (orders/eod_squareoff.run_short_squareoff; 5 min before the long square-off)
SHORT_VWAP_TOLERANCE_PCT = _ms_float("SHORT_VWAP_TOLERANCE_PCT", 0.15, 0.0, 2.0)        # refuse when price is MORE than this % above the day's VWAP
SHORT_MIN_ENTRY_RANGE_POSITION = _ms_float("SHORT_MIN_ENTRY_RANGE_POSITION", 0.08, 0.0, 0.5)  # refuse at / below this place in the day range (0 = the low)
SHORT_MAX_DAY_FALL_PCT = _ms_float("SHORT_MAX_DAY_FALL_PCT", 5.0, 0.0, 20.0)            # mirror of MAX_DAY_GAIN_PCT; 0 = off
SHORT_UPPER_CIRCUIT_BUFFER_PCT = _ms_float("SHORT_UPPER_CIRCUIT_BUFFER_PCT", 3.0, 0.5, 20.0)  # a locked-up stock cannot be bought back
SHORT_MIN_BOARD_CONVICTION = _ms_float("SHORT_MIN_BOARD_CONVICTION", 50.0, 0.0, 100.0)  # Market Board bearish conviction needed
SHORT_GAP_UP_REFUSE_PCT = _ms_float("SHORT_GAP_UP_REFUSE_PCT", 1.0, 0.0, 10.0)          # gapped up this much and still above the open: no short; 0 = off

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
# E39: real cap on NEW automatic entries per IST day (orders/entry_cap.py). 0 = off. 10 ships (E46: two full turns of the 5 scalp slots, the same 2-turns ratio real-trade has with 3 slots and a cap of 6). Change it on the calibration page.
MAX_ENTRIES_PER_DAY = _get_int("POSITION_STOCKS_MAX_ENTRIES_PER_DAY", 10)

# ── Daily loss kill switch (tighter than real-trade-service's, by design) ──
MAX_DAILY_LOSS_PCT_OF_POOL = _get_float("MAX_DAILY_LOSS_PCT_OF_POOL", 4.0)
# A11 (2026-10-22): account-wide daily loss cap. Net realized loss today of BOTH engines (this pool + real-trade-service)
# as a % of their combined capital; at or above this, reserve_capital() refuses new entries. 0 disables. An extra layer on
# top of MAX_DAILY_LOSS_PCT_OF_POOL, not a replacement. Same env name as real-trade-service so one value governs both.
ACCOUNT_MAX_DAILY_LOSS_PCT = _get_float("ACCOUNT_MAX_DAILY_LOSS_PCT", 3.0)

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
# group 332: the Nifty-weak gates are OFF by default (a weak market must not stop entries; some stocks do well on down days).
# MARKET_GATE_ENABLED=1 / MARKET_GATE_PREV_CLOSE_ENABLED=1 bring them back.
MARKET_GATE_ENABLED = _get_bool("MARKET_GATE_ENABLED", False)
MARKET_GATE_MIN_NIFTY_CHANGE_PCT = _get_float("MARKET_GATE_MIN_NIFTY_CHANGE_PCT", -0.10)
MARKET_GATE_CACHE_TTL_S = _get_float("MARKET_GATE_CACHE_TTL_S", 120.0)
# Group 221 (2026-10-07): the gate above measures Nifty against TODAY'S OPEN, so a market that gapped
# down 1.2% and has been flat since the open reads 0.0% and is let through. This second check blocks
# on the change against the PREVIOUS SESSION's close (gateway key nifty_vs_prev_close). The -0.75 default
# is my assumption (roughly a bottom-fifth Nifty day); there is no outcome data behind it. Ignored for a
# stale / fallback gateway payload and when the key is missing (older gateway), so it fails open.
MARKET_GATE_PREV_CLOSE_ENABLED = _get_bool("MARKET_GATE_PREV_CLOSE_ENABLED", False)
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
ADAPTIVE_BAR_STOP_MIN_PCT = _get_float("ADAPTIVE_BAR_STOP_MIN_PCT", 1.2)   # E251: 0.8 -> 1.2 (11 of 14 trades sat on the 0.8 floor; ARVIND 28 Oct stopped in 1 minute)
ADAPTIVE_BAR_STOP_MAX_PCT = _get_float("ADAPTIVE_BAR_STOP_MAX_PCT", 2.0)
ADAPTIVE_BAR_TARGET_RR = _get_float("ADAPTIVE_BAR_TARGET_RR", 2.0)   # 2026-10-22: 1.8 -> 2.0 (reward:risk 1:2)
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

# group 332: "best 3 to 5" shortlist (screening/shortlist.py). With it on, up to SHORTLIST_MAX ranked names are quality-checked, the
# passers are re-ranked on scan score + Market Board conviction + quality + zone score, and the best SHORTLIST_MAX are tried
# best-first. Never lowers a gate. SHORTLIST_ENABLED=0 restores the old scan-order behaviour.
SHORTLIST_ENABLED = _get_bool("SHORTLIST_ENABLED", True)
# E259: with this on, quality-passers that the opening gate would refuse RIGHT NOW (gap above / below the limits, under the open or previous close,
# near the day high, outside the opening-range rules, weak previous day, Nifty weak) are taken out BEFORE the shortlist is ranked, so they no longer
# use up the SHORTLIST_MAX slots (5 gap-up names were blocking every cycle 09:15-10:00). It never loosens a gate: entry.py still runs the same
# opening_gate.reject_reason on whatever is tried. Names whose data is simply not ready (no prev-day candle, no day stats) stay in. 0 = old behaviour.
SHORTLIST_GATE_PREFILTER = _get_bool("SHORTLIST_GATE_PREFILTER", True)
SHORTLIST_MAX = max(1, min(10, _get_int("SHORTLIST_MAX", 5)))
SHORTLIST_MIN_SCORE = _get_float("SHORTLIST_MIN_SCORE", 0.0)
SHORTLIST_W_SCAN = _get_float("SHORTLIST_W_SCAN", 0.40)
SHORTLIST_W_BOARD = _get_float("SHORTLIST_W_BOARD", 0.30)
SHORTLIST_W_QUALITY = _get_float("SHORTLIST_W_QUALITY", 0.20)
SHORTLIST_W_ZONE = _get_float("SHORTLIST_W_ZONE", 0.10)
SHORTLIST_BEAR_PENALTY = _get_float("SHORTLIST_BEAR_PENALTY", 20.0)
SHORTLIST_EXTENDED_PENALTY = _get_float("SHORTLIST_EXTENDED_PENALTY", 10.0)
SHORTLIST_BOARD_TIMEOUT_S = _get_float("SHORTLIST_BOARD_TIMEOUT_S", 3.0)
# group 335: when the best pick is skipped for a reason that only concerns that symbol (VWAP, liquidity, depth, circuit, re-entry,
# opening gate, price guard ...), try the next shortlisted pick instead of ending the cycle. Skips that end every attempt (not armed,
# kill switch, order budget, max positions, margin pause) still end it. SHORTLIST_FALLTHROUGH=0 restores stop-at-first-skip.
SHORTLIST_FALLTHROUGH = _get_bool("SHORTLIST_FALLTHROUGH", True)
# group 338: the scalper is long-only, so a quality-gate passer the Market Board ranks BEARISH is dropped from the shortlist whenever at least
# one non-bearish passer remains (it is never the only reason for an empty shortlist). SHORTLIST_DROP_BEARISH=0 = penalty only (group332).
SHORTLIST_DROP_BEARISH = _get_bool("SHORTLIST_DROP_BEARISH", True)
# group 338: Top picks cards (screening/top_picks.py, GET /top-picks). Display only.
#   TOP_PICKS_N              cards shown (the best 3 to 5; default 5)
#   TOP_PICKS_MIN_SHOWN      below this many fresh engine picks, strongest bullish board rows fill the list (flagged "board preview")
#   TOP_PICKS_FRESH_S        an entry cycle's shortlist counts as fresh this long
#   TOP_PICKS_MIN_CONVICTION board rows below this conviction never appear as preview picks
TOP_PICKS_N = max(1, min(10, _get_int("TOP_PICKS_N", 5)))
TOP_PICKS_MIN_SHOWN = max(0, min(10, _get_int("TOP_PICKS_MIN_SHOWN", 3)))
TOP_PICKS_FRESH_S = max(30.0, _get_float("TOP_PICKS_FRESH_S", 900.0))
TOP_PICKS_MIN_CONVICTION = _get_float("TOP_PICKS_MIN_CONVICTION", 60.0)
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
MIN_FUNDAMENTAL_SCORE = _get_float("MIN_FUNDAMENTAL_SCORE", 25.0)   # 2026-10-19: 40 -> 25 (intraday momentum; liquidity gates are the genuine-stock guard). Overnight carry keeps its own 60.
MIN_TECHNICAL_SCORE = _get_float("MIN_TECHNICAL_SCORE", 30.0)   # 2026-10-19: 40 -> 30 (daily indicators lag a same-day move). Overnight carry keeps its own 60.
MIN_MARKET_CAP_CR = _get_float("MIN_MARKET_CAP_CR", 200.0)  # 2026-10-19: 500 -> 200 crore; the turnover floors (ENTRY_MIN_*_TURNOVER_CR) keep thin stocks out. Overnight carry keeps 2000.

# ── Minimum stock price gate (2026-09-18, session67) ────────────────────────
# Penny stocks (₹0.19, ₹0.50, etc.) look great on a percentage-change scan
# because a 1-paise tick is already a 5% move. They also have near-zero
# absolute P&L even on a "win" (6481 shares × ₹0.43 move = ₹2,787), and
# Dhan frequently rejects them outright (T2T, circuit-limit, or exchange
# restriction). Checked in entry.py BEFORE quality gate — no network call
# needed, just a price comparison. Default ₹20 keeps all real intraday
# names; override via env to raise if you want only mid/large-cap territory.
MIN_STOCK_PRICE = _get_float("MIN_STOCK_PRICE", 10.0)   # 2026-10-19: 20 -> 10

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

# E252 (2026-10-28): adaptive stagnation / no-follow-through (orders/exit_adaptive.py). After E251 the target is 2.4-3 %, so the fixed +0.5 % / +-0.35 % no longer
# mean the same thing, and both rules closed trades whether or not a better candidate was waiting. EXIT_ADAPTIVE_ENABLED=false = the fixed numbers and the old behaviour.
#   gain needed = NFT_TARGET_FRAC x the trade's own target %, clamped NFT_GAIN_MIN_PCT..NFT_GAIN_MAX_PCT
#   flat band   = STAGNATION_BAND_TARGET_FRAC x target %, clamped STAGNATION_BAND_MIN_PCT..STAGNATION_BAND_MAX_PCT
#   A flat / no-follow-through trade is closed only when it is FADING (price on the wrong side of the day VWAP, or lower highs over the last EXIT_FADE_WINDOW_MIN
#   minutes) or a qualifying candidate was actually skipped for capital / slots in the last EXIT_SLOT_PRESSURE_WINDOW_MIN minutes. Otherwise it is left to its stop,
#   target or the EOD sweep, except that EXIT_HARD_MAX_MINUTES is a hard maximum holding time (the old rules then apply unchanged).
EXIT_ADAPTIVE_ENABLED = _get_bool("EXIT_ADAPTIVE_ENABLED", True)
NFT_TARGET_FRAC = _get_float("NFT_TARGET_FRAC", 0.2)
NFT_GAIN_MIN_PCT = _get_float("NFT_GAIN_MIN_PCT", 0.4)
NFT_GAIN_MAX_PCT = _get_float("NFT_GAIN_MAX_PCT", 0.8)
STAGNATION_BAND_TARGET_FRAC = _get_float("STAGNATION_BAND_TARGET_FRAC", 0.15)
STAGNATION_BAND_MIN_PCT = _get_float("STAGNATION_BAND_MIN_PCT", 0.25)
STAGNATION_BAND_MAX_PCT = _get_float("STAGNATION_BAND_MAX_PCT", 0.6)
EXIT_FADE_WINDOW_MIN = _get_float("EXIT_FADE_WINDOW_MIN", 10.0)
EXIT_SLOT_PRESSURE_WINDOW_MIN = _get_float("EXIT_SLOT_PRESSURE_WINDOW_MIN", 10.0)
EXIT_HARD_MAX_MINUTES = _get_float("EXIT_HARD_MAX_MINUTES", 90.0)

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
# group 480 (2026-10-21): Dhan charges INTRADAY brokerage per executed order as the LOWER of Rs 20 and 0.03 % of the order value (dhan.co/pricing).
# The gate used only the flat BROKERAGE_PER_ORDER (default 0), so it priced brokerage at Rs 0 and under-counted the round trip by about 0.06 %.
# Each leg now costs max(BROKERAGE_PER_ORDER, min(SCALP_COST_GATE_BROKERAGE_CAP_RS, value x SCALP_COST_GATE_BROKERAGE_PCT / 100)).
# Rollback: SCALP_COST_GATE_BROKERAGE_PCT=0 (the old Rs 0 behaviour). Feeds only this gate, not the charges ledger.
SCALP_COST_GATE_BROKERAGE_PCT = _get_float("SCALP_COST_GATE_BROKERAGE_PCT", 0.03)
SCALP_COST_GATE_BROKERAGE_CAP_RS = _get_float("SCALP_COST_GATE_BROKERAGE_CAP_RS", 20.0)

# group 329 (2026-10-12): two pre-buy checks a market professional makes before any entry. Both FAIL OPEN (missing data never blocks).
#   CIRCUIT_GATE_ENABLED        0 turns the circuit-band check off
#   CIRCUIT_UPPER_BUFFER_PCT    no BUY within this % of the exchange UPPER circuit (locked / no room; a scalp target of
#                               MIN_TARGET_PCT needs room above the price). 0 disables this side.
#   CIRCUIT_LOWER_BUFFER_PCT    no BUY within this % of the LOWER circuit (may lock down; a stop or the EOD sell might not execute)
#   EARNINGS_DAY_BLOCK          no BUY on a stock whose results / earnings are dated today (gap risk a stop cannot cover); the date
#                               comes from the event service answer the quality gate already fetches (QUALITY_GATE_ENABLED must be on)
# The limits come from market-data's /quote row (the same cached read the depth gate uses), not from the AngelOne frame.
CIRCUIT_GATE_ENABLED = _get_bool("CIRCUIT_GATE_ENABLED", True)
CIRCUIT_UPPER_BUFFER_PCT = _get_float("CIRCUIT_UPPER_BUFFER_PCT", 1.0)
CIRCUIT_LOWER_BUFFER_PCT = _get_float("CIRCUIT_LOWER_BUFFER_PCT", 2.0)
EARNINGS_DAY_BLOCK = _get_bool("EARNINGS_DAY_BLOCK", True)

# group 330 (2026-10-12): India VIX, negative news and a 09:08 pre-market plan for the scalper. All FAIL OPEN, each with its own switch.
#   VIX_GATE_ENABLED     master switch (VIX read from market-data /quote/INDIAVIX, cached VIX_CACHE_TTL_S)
#   VIX_BLOCK_ABOVE      no new entries at / above this VIX level (0 = off)
#   VIX_CAUTION_ABOVE    at / above this level, or VIX up VIX_SPIKE_PCT % on the day: position size x VIX_CAUTION_SIZE_MULT
#   NEWS_NEGATIVE_BLOCK_ENABLED / NEWS_NEGATIVE_BLOCK_SCORE   quality gate: skip a stock whose event score (0-100, 50 = neutral) is at / below the
#                        score or that carries a regulatory action / delisting risk / fresh earnings miss (same event-service answer, no extra call)
#   PREOPEN_*            09:08 snapshot: warms caches and records the indicative gap of the first PREOPEN_MAX_SYMBOLS F&O names; a gap-up of
#                        PREOPEN_GAP_UP_BLOCK_PCT or a gap-down of PREOPEN_GAP_DOWN_BLOCK_PCT blocks the entry until PREOPEN_GAP_GATE_UNTIL_IST
VIX_GATE_ENABLED = _get_bool("VIX_GATE_ENABLED", True)
VIX_BLOCK_ABOVE = _get_float("VIX_BLOCK_ABOVE", 0.0)   # group 332: 0 = no VIX block (fear must not stop entries); VIX_CAUTION_* still shrinks size. 24 = old block
VIX_CAUTION_ABOVE = _get_float("VIX_CAUTION_ABOVE", 18.0)
VIX_CAUTION_SIZE_MULT = _get_float("VIX_CAUTION_SIZE_MULT", 0.7)
VIX_SPIKE_PCT = _get_float("VIX_SPIKE_PCT", 10.0)
# E107 (P2 item 16, the two decisions): (1) VIX_CAUTION_PCTILE - caution also starts at this percentile of India VIX's own last ~12 months of daily closes (75 = the
# top quarter of its own year), not only at the fixed VIX_CAUTION_ABOVE; whichever is LOWER wins, so it can only add caution. 0 = off (fixed level only). Needs the
# VIX history from market-data; without it the fixed level alone applies. (2) PREOPEN_GAP_ATR_MULT - a stock's indicated pre-open gap is also judged against its OWN
# daily ATR %: a gap of at least this many ATRs (never under PREOPEN_GAP_ATR_FLOOR_PCT) is blocked until the gap gate time, so a calm stock's 2% gap counts and a wild
# stock's does not; whichever is LOWER (fixed % or ATR multiple) wins, so it can only block more. 0 = off. Both fail open when the data is missing.
VIX_CAUTION_PCTILE = max(0.0, min(99.0, _get_float("VIX_CAUTION_PCTILE", 75.0)))
VIX_HISTORY_TIMEOUT_S = _get_float("VIX_HISTORY_TIMEOUT_S", 5.0)
VIX_CACHE_TTL_S = _get_float("VIX_CACHE_TTL_S", 120.0)
VIX_FETCH_TIMEOUT_S = _get_float("VIX_FETCH_TIMEOUT_S", 3.0)
NEWS_NEGATIVE_BLOCK_ENABLED = _get_bool("NEWS_NEGATIVE_BLOCK_ENABLED", True)
NEWS_NEGATIVE_BLOCK_SCORE = _get_float("NEWS_NEGATIVE_BLOCK_SCORE", 35.0)
PREOPEN_SNAPSHOT_ENABLED = _get_bool("PREOPEN_SNAPSHOT_ENABLED", True)
PREOPEN_SNAPSHOT_TIME_IST = _get_str("PREOPEN_SNAPSHOT_TIME_IST", "09:10")   # group 399: was 09:08
PREOPEN_GAP_GATE_UNTIL_IST = _get_str("PREOPEN_GAP_GATE_UNTIL_IST", "10:00")
PREOPEN_GAP_UP_BLOCK_PCT = _get_float("PREOPEN_GAP_UP_BLOCK_PCT", 6.0)
PREOPEN_GAP_ATR_MULT = max(0.0, min(5.0, _get_float("PREOPEN_GAP_ATR_MULT", 1.5)))
PREOPEN_GAP_ATR_FLOOR_PCT = max(0.0, _get_float("PREOPEN_GAP_ATR_FLOOR_PCT", 1.5))
PREOPEN_GAP_DOWN_BLOCK_PCT = _get_float("PREOPEN_GAP_DOWN_BLOCK_PCT", 3.0)
PREOPEN_MAX_PRICE_AGE_S = _get_float("PREOPEN_MAX_PRICE_AGE_S", 420.0)   # group 399: was 180 (no order updates in the 09:08-09:15 buffer)
PREOPEN_MAX_SYMBOLS = _get_int("PREOPEN_MAX_SYMBOLS", 150)
PREOPEN_FETCH_TIMEOUT_S = _get_float("PREOPEN_FETCH_TIMEOUT_S", 6.0)

# group 391 (2026-10-15): 09:10 opening plan (screening/opening_plan.py). At 09:15 a symbol has no 5-minute bars yet, so adaptive.compute() fell to the
# legacy path whose stop floor is MIN_STOP_PCT (2%) and target ~4.4%. The plan turns each symbol's previous-session daily ATR (screening/prev_day.py,
# cache only, no network on the entry path) into an expected 5-minute bar range (daily ATR % x OPENING_PLAN_BAR_FRACTION) and feeds the SAME bar-level
# stop/target rules as the live path, until live bars exist or OPENING_PLAN_USE_UNTIL_IST. OPENING_PLAN_ENABLED=0 = old behaviour.
# OPENING_PLAN_BAR_FRACTION is a tunable assumption (a typical 5-minute range as a share of the daily ATR), not a backtested value.
OPENING_PLAN_ENABLED = _get_bool("OPENING_PLAN_ENABLED", True)
OPENING_PLAN_TIME_IST = _get_str("OPENING_PLAN_TIME_IST", "09:10")
OPENING_PLAN_USE_UNTIL_IST = _get_str("OPENING_PLAN_USE_UNTIL_IST", "10:00")
OPENING_PLAN_BAR_FRACTION = _get_float("OPENING_PLAN_BAR_FRACTION", 0.25)

# group 392 (2026-10-15): opening protection (orders/opening_protect.py). From 09:15 until OPENING_PROTECT_UNTIL_IST the automatic entry is sized at
# OPENING_SIZE_MULT of the normal quantity (nearest share, never under 1), and after OPENING_PAUSE_STOPOUTS stop-outs since the bell no new automatic
# entry is taken until the window ends. Manual BUYs are not touched (a person confirmed them). OPENING_PROTECT_ENABLED=0 = off.
OPENING_PROTECT_ENABLED = _get_bool("OPENING_PROTECT_ENABLED", True)
OPENING_PROTECT_UNTIL_IST = _get_str("OPENING_PROTECT_UNTIL_IST", "09:45")
OPENING_SIZE_MULT = _get_float("OPENING_SIZE_MULT", 0.5)
OPENING_PAUSE_STOPOUTS = _get_int("OPENING_PAUSE_STOPOUTS", 2)   # 0 = no pause

# group 333 (2026-10-12): VWAP confirmation (orders/vwap_gate.py). No long below the day's VWAP (Dhan average_price on the /quote row, the same row the
# depth and circuit gates read) by more than the tolerance; before ENTRY_VWAP_MIN_TIME_IST the average rests on too few trades to judge.
# ENTRY_VWAP_MAX_EXTENSION_PCT > 0 also refuses a chase that far ABOVE the VWAP (0 = off). Fails open on missing data.
ENTRY_VWAP_GATE_ENABLED = _get_bool("ENTRY_VWAP_GATE_ENABLED", True)
ENTRY_VWAP_TOLERANCE_PCT = _get_float("ENTRY_VWAP_TOLERANCE_PCT", 0.15)
ENTRY_VWAP_MAX_EXTENSION_PCT = _get_float("ENTRY_VWAP_MAX_EXTENSION_PCT", 0.0)
ENTRY_VWAP_MIN_TIME_IST = _get_str("ENTRY_VWAP_MIN_TIME_IST", "09:45")

# group 333 (2026-10-12): liquidity floor (orders/liquidity_gate.py): normal daily turnover (20-session mean of volume x close, crore Rs), today's turnover
# so far, and relative volume vs the normal pace of the session. A floor of 0 turns that test off; unknown inputs never block.
ENTRY_LIQUIDITY_GATE_ENABLED = _get_bool("ENTRY_LIQUIDITY_GATE_ENABLED", True)
ENTRY_MIN_AVG_TURNOVER_CR = _get_float("ENTRY_MIN_AVG_TURNOVER_CR", 3.0)
ENTRY_MIN_DAY_TURNOVER_CR = _get_float("ENTRY_MIN_DAY_TURNOVER_CR", 1.0)
ENTRY_LIQ_MIN_TIME_IST = _get_str("ENTRY_LIQ_MIN_TIME_IST", "10:00")
ENTRY_RVOL_MIN_TIME_IST = _get_str("ENTRY_RVOL_MIN_TIME_IST", "09:45")
ENTRY_MIN_RVOL = _get_float("ENTRY_MIN_RVOL", 0.35)

# group 336 (2026-10-12): expiry / Friday / pre-holiday rules (orders/calendar_gate.py), catalyst check (orders/catalyst_gate.py), losing-window
# auto-disable + stop/target width advice (orders/window_health.py). All fail open; each has a switch.
CALENDAR_RULES_ENABLED = _get_bool("CALENDAR_RULES_ENABLED", True)
CALENDAR_EXPIRY_WEEKDAY = _get_int("CALENDAR_EXPIRY_WEEKDAY", 1)               # 0=Mon..4=Fri; 1 = Tuesday (NSE index weekly expiry since Sept 2025 - verify)
CALENDAR_EXPIRY_ENTRY_CUTOFF_IST = _get_str("CALENDAR_EXPIRY_ENTRY_CUTOFF_IST", "14:30")      # blank = no cut-off
CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST = _get_str("CALENDAR_PRE_HOLIDAY_ENTRY_CUTOFF_IST", "14:30")
CALENDAR_FRIDAY_ENTRY_CUTOFF_IST = _get_str("CALENDAR_FRIDAY_ENTRY_CUTOFF_IST", "")
CALENDAR_EXPIRY_SIZE_MULT = _get_float("CALENDAR_EXPIRY_SIZE_MULT", 0.8)
CALENDAR_PRE_HOLIDAY_SIZE_MULT = _get_float("CALENDAR_PRE_HOLIDAY_SIZE_MULT", 0.8)
CALENDAR_FRIDAY_SIZE_MULT = _get_float("CALENDAR_FRIDAY_SIZE_MULT", 0.9)
CALENDAR_EXPIRY_CARRY_MAX = _get_int("CALENDAR_EXPIRY_CARRY_MAX", 2)           # carry caps are used by real-trade only; kept so the knobs match
CALENDAR_PRE_HOLIDAY_CARRY_MAX = _get_int("CALENDAR_PRE_HOLIDAY_CARRY_MAX", 1)
CALENDAR_FRIDAY_CARRY_MAX = _get_int("CALENDAR_FRIDAY_CARRY_MAX", 2)
CATALYST_NO_NEWS_ACTION = _get_str("CATALYST_NO_NEWS_ACTION", "size")           # size | block | off
CATALYST_MOVE_PCT = _get_float("CATALYST_MOVE_PCT", 4.0)
CATALYST_NO_NEWS_SIZE_MULT = _get_float("CATALYST_NO_NEWS_SIZE_MULT", 0.6)
WINDOW_AUTODISABLE_ENABLED = _get_bool("WINDOW_AUTODISABLE_ENABLED", True)
WINDOW_AUTODISABLE_DAYS = _get_int("WINDOW_AUTODISABLE_DAYS", 7)
WINDOW_AUTODISABLE_MIN_TRADES = _get_int("WINDOW_AUTODISABLE_MIN_TRADES", 8)
WINDOW_AUTODISABLE_MAX_WIN_RATE_PCT = _get_float("WINDOW_AUTODISABLE_MAX_WIN_RATE_PCT", 35.0)
WINDOW_TUNING_MIN_TRADES = _get_int("WINDOW_TUNING_MIN_TRADES", 20)
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
# E252 (2026-10-28): SYNGENE 28 Oct - bought 389.45, the trail started at +1.0 % and sat 0.78 % under the peak (0.6 x its 1.3 % stop), exited 391.15 (+0.44 %),
# then the stock ran to 412.7 (the 2.5 % target was 399.20). The fixed distance / activation ignored how much the stock moves. Adaptive version (orders/exit_adaptive.py):
#   distance   = max(old distance, TRAIL_BAR_RANGE_MULT x the stock's own average 5-minute bar range), never wider than TRAIL_MAX_DIST_STOP_FRAC x its stop
#   activation = max(TRAIL_ACTIVATE_PCT, TRAIL_ACTIVATE_TARGET_FRAC x the trade's own target %), capped at TRAIL_ACTIVATE_MAX_PCT
# TRAIL_ADAPTIVE_ENABLED=false = the old fixed numbers. Breakeven + ticks stays the first lock either way.
TRAIL_ADAPTIVE_ENABLED = _get_bool("TRAIL_ADAPTIVE_ENABLED", True)
TRAIL_BAR_RANGE_MULT = _get_float("TRAIL_BAR_RANGE_MULT", 1.0)
TRAIL_MAX_DIST_STOP_FRAC = _get_float("TRAIL_MAX_DIST_STOP_FRAC", 1.0)
TRAIL_ACTIVATE_TARGET_FRAC = _get_float("TRAIL_ACTIVATE_TARGET_FRAC", 0.5)
TRAIL_ACTIVATE_MAX_PCT = _get_float("TRAIL_ACTIVATE_MAX_PCT", 2.0)

# -- group 340 (2026-10-12): stop integrity, candle-history levels, sizing, scalp target cap -------------------------------------
# Stop-breach watchdog (orders/stop_watchdog.py): FEDDERSHOL's stop was trailed to 58.85 yet the trade ended at 58.41 - nothing noticed the
# price sitting under its own stop. Price under an OPEN position's stored stop for STOP_WATCHDOG_GRACE_S seconds = the broker stop did not
# fire: close at market (status STOP_BREACH_EXIT) and alert. STOP_WATCHDOG_ENABLED=0 = off.
STOP_WATCHDOG_ENABLED = _get_bool("STOP_WATCHDOG_ENABLED", True)
STOP_WATCHDOG_GRACE_S = _get_float("STOP_WATCHDOG_GRACE_S", 20.0)
STOP_WATCHDOG_MIN_BREACH_TICKS = _get_int("STOP_WATCHDOG_MIN_BREACH_TICKS", 1)   # price must be this many ticks UNDER the stop
STOP_WATCHDOG_MAX_TICK_AGE_S = _get_float("STOP_WATCHDOG_MAX_TICK_AGE_S", 15.0)  # an older price does not count
STOP_WATCHDOG_RETRY_S = _get_float("STOP_WATCHDOG_RETRY_S", 30.0)                # wait after a rejected watchdog exit
# Read-back after every stop move (dhan_client.verify_stop_leg): the stop is saved as moved only when Dhan's own leg shows it. Unreadable =
# fails open (as before). STOP_VERIFY_ENABLED=0 = off.
STOP_VERIFY_ENABLED = _get_bool("STOP_VERIFY_ENABLED", True)
STOP_VERIFY_RETRY_WAIT_S = _get_float("STOP_VERIFY_RETRY_WAIT_S", 0.4)           # one re-read after this pause before calling a move unconfirmed
# Candle history for the volatility stop (orders/adaptive.py): a symbol with fewer than ADAPTIVE_BAR_MIN_BARS tick-buffer bars fell back to
# the tick proxy, which lands on the 2% stop / ~4% target floor (COFFEEDAY, SYNCOMF, CLEANMAX on 2026-10-12). Now the bar store, then ONE
# short cached market-data /history call, supplies the bars first.
ADAPTIVE_HISTORY_ENABLED = _get_bool("ADAPTIVE_HISTORY_ENABLED", True)
ADAPTIVE_HISTORY_TIMEOUT_S = _get_float("ADAPTIVE_HISTORY_TIMEOUT_S", 2.0)
ADAPTIVE_HISTORY_CACHE_S = _get_float("ADAPTIVE_HISTORY_CACHE_S", 300.0)
ADAPTIVE_HISTORY_MISS_CACHE_S = _get_float("ADAPTIVE_HISTORY_MISS_CACHE_S", 120.0)
# Scalp target cap: no trade on 2026-10-12 got past +2.3% (targets 3.5-4.4%, clocks cut trades at 20-30 min). A target above the cap is cut to
# it; if reward:risk then falls under SCALP_TARGET_CAP_MIN_RR the stop is tightened to target/MIN_RR (never under SCALP_TARGET_CAP_MIN_STOP_PCT).
# Risk-based sizing makes a tighter stop a larger position for the SAME rupee risk. 0 = no cap.
SCALP_TARGET_CAP_PCT = _get_float("SCALP_TARGET_CAP_PCT", 3.0)   # E251: 2.5 -> 3.0 so a 1.5 % stop keeps 2:1 instead of being squeezed back to 1.25 %
SCALP_TARGET_CAP_MIN_RR = _get_float("SCALP_TARGET_CAP_MIN_RR", 2.0)   # 2026-10-22: 1.5 -> 2.0 (reward:risk 1:2)
SCALP_TARGET_CAP_MIN_STOP_PCT = _get_float("SCALP_TARGET_CAP_MIN_STOP_PCT", 1.2)   # E251: 0.8 -> 1.2
SCALP_TARGET_CAP_MIN_STOP_ATR_MULT = _get_float("SCALP_TARGET_CAP_MIN_STOP_ATR_MULT", 1.0)   # 2026-10-19: stop floor after a target cap = this x the symbol ATR proxy %; 0 = off

# ── Pullback re-arm entries (2026-10-19, orders/pullback_rearm.py) ────────────────────────────────────────────────
# A stock skipped for being at / extended above its high is re-checked on a pullback + bounce instead of being forgotten.
PULLBACK_REARM_ENABLED = _get_bool("PULLBACK_REARM_ENABLED", True)
REARM_MIN_DAY_PCT = _get_float("REARM_MIN_DAY_PCT", 1.5)                 # arm only a stock already up this much on the scan window
REARM_MIN_PULLBACK_PCT = _get_float("REARM_MIN_PULLBACK_PCT", 0.8)       # peak -> low, at least this deep ...
REARM_MAX_PULLBACK_PCT = _get_float("REARM_MAX_PULLBACK_PCT", 3.5)       # ... and no deeper (deeper = the move failed; dropped)
REARM_BOUNCE_PCT = _get_float("REARM_BOUNCE_PCT", 0.3)                   # price must be this far off the low
REARM_HOLD_S = _get_float("REARM_HOLD_S", 30.0)                          # the low must be at least this old (no falling knife)
REARM_MIN_BELOW_PEAK_PCT = _get_float("REARM_MIN_BELOW_PEAK_PCT", 0.3)  # not back at the high (that is chasing again)
REARM_MAX_AGE_MIN = _get_float("REARM_MAX_AGE_MIN", 90.0)                # an armed setup expires after this
REARM_MAX_ATTEMPTS = _get_int("REARM_MAX_ATTEMPTS", 2)                   # per symbol per day
REARM_MAX_ARMED = _get_int("REARM_MAX_ARMED", 25)
REARM_STOP_BUFFER_PCT = _get_float("REARM_STOP_BUFFER_PCT", 0.15)        # stop sits this far under the pullback low
REARM_MIN_STOP_PCT = _get_float("REARM_MIN_STOP_PCT", 1.0)               # ... but never closer than this to the entry
# Share rounding (orders/sizing.py): floor() lost up to a share (CLEANMAX: budget ~1.9 shares -> 1). Round up when the extra share costs at most
# SIZING_MAX_OVERSHOOT_PCT over the budget; when ONE share costs more than SIZING_ONE_SHARE_MAX_MULT x the budget, skip instead of forcing 1
# share. SIZING_ROUNDING_ENABLED=0 = old behaviour.
SIZING_ROUNDING_ENABLED = _get_bool("SIZING_ROUNDING_ENABLED", True)
# E30 (E37): trade journal notes (reason + feeling, orders/journal_notes.py). OFF = PUT /journal/notes is refused (409), a manual buy ignores `reason` / `emotion`,
# and GET /journal/notes reports enabled=false. Notes already saved stay. Descriptive only: no gate, score or size ever reads a note.
JOURNAL_NOTES_ENABLED = _get_bool("JOURNAL_NOTES_ENABLED", True)
SIZING_MAX_OVERSHOOT_PCT = _get_float("SIZING_MAX_OVERSHOOT_PCT", 10.0)
SIZING_ONE_SHARE_MAX_MULT = _get_float("SIZING_ONE_SHARE_MAX_MULT", 1.5)
# Cheap-stock floor (0 = off). Ships OFF: set e.g. 50 to skip sub-Rs50 names once the journal supports it.
ENTRY_MIN_PRICE = _get_float("ENTRY_MIN_PRICE", 0.0)
# Optional cut-off for ordinary days (blank = none). The two latest entries on 2026-10-12 (14:12, 14:29) lost; two trades prove nothing, so OFF.
SCALP_ENTRY_CUTOFF_IST = _get_str("SCALP_ENTRY_CUTOFF_IST", "14:00")   # group 479: default 14:00 (was blank). No new scalp after this on any day; the 21 Oct review showed late entries are the weakest and the real-trade 14:00 cut-off (group 470) is the same rule. Blank is NOT "off" now (blank = default); set 23:59 to disable.
# Reconcile announces the same closed-trade fill correction once per process per position (was every 5 minutes, all day).
RECONCILE_LOG_ONCE = _get_bool("RECONCILE_LOG_ONCE", True)

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
# group389: the checks above fail open when the symbol has NO tick at all, so a MARKET entry could go out with no live price
# to compare against (no stale-tick, no slippage check). ENTRY_REQUIRE_LIVE_TICK (default on) makes the automatic entry skip
# such a symbol with reason NO_LIVE_TICK until the feed has a tick for it. ENTRY_REQUIRE_LIVE_TICK=0 restores the old behaviour.
ENTRY_REQUIRE_LIVE_TICK = _get_bool("ENTRY_REQUIRE_LIVE_TICK", True)
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
# group322: no depth reading for a symbol (no answer, timeout, quote without a book). "allow" = enter anyway (old behaviour);
# "refuse" = never without a reading; "auto" (default) = allow until depth has been seen flowing, then refuse a name with none.
# ENTRY_DEPTH_PROVEN_WINDOW_S = how long a seen reading counts as "flowing" (21600 = 6 h, 0 = whole process).
ENTRY_DEPTH_MISSING = (os.getenv("ENTRY_DEPTH_MISSING") or "auto").strip().lower()
ENTRY_DEPTH_PROVEN_WINDOW_S = _get_float("ENTRY_DEPTH_PROVEN_WINDOW_S", 21600.0)
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
# group427: order-flow record stored on every new position (scalp_positions.flow_json). RECORD ONLY: nothing reads it to block, size or order.
# ORDER_FLOW_RECORD=0 switches it off; ORDER_FLOW_DEPTH20=0 skips the one extra /depth/{symbol} call (needs market-data DHAN_DEPTH20_ENABLED=1).
ORDER_FLOW_RECORD = _get_bool("ORDER_FLOW_RECORD", True)
ORDER_FLOW_DEPTH20 = _get_bool("ORDER_FLOW_DEPTH20", True)
ORDER_FLOW_DEPTH20_TIMEOUT_S = max(0.2, _get_float("ORDER_FLOW_DEPTH20_TIMEOUT_S", 1.5))
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
# group 448: when the shared order budget / symbol lock cannot reach the DB, refuse the new BUY (true, default) instead of allowing it (false = old behaviour).
SHARED_GATES_FAIL_CLOSED = _get_bool("SHARED_GATES_FAIL_CLOSED", True)

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

# group 460: "Change password" on the login page. OFF unless true here; the new password is one DB row shared with the other trading service
# (keep the flag the same on both). ADMIN_IGNORE_PASSWORD_OVERRIDE=true is lock-out recovery: the env hash is used again.
ADMIN_PASSWORD_CHANGE_ENABLED = (os.getenv("ADMIN_PASSWORD_CHANGE_ENABLED") or "").strip().lower() in ("1", "true", "yes", "on")
ADMIN_IGNORE_PASSWORD_OVERRIDE = (os.getenv("ADMIN_IGNORE_PASSWORD_OVERRIDE") or "").strip().lower() in ("1", "true", "yes", "on")

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
# group345 (13 Oct log: SHRIRAMFIN / SAKSOFT / INDIAGLYCO (price guard, up ~10%) and GRMOVER (range gate) were quality-gated and rejected again every ~11 s):
# a symbol that fails one of these price-only entry gates is left out of the candidate list for this many seconds. 0 = off.
GATE_REJECT_COOLDOWN_S = _get_float("GATE_REJECT_COOLDOWN_S", 60.0)
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
OPENING_GATE_MIN_MINUTES_AFTER_OPEN = _get_float("OPENING_GATE_MIN_MINUTES_AFTER_OPEN", 0.0)   # group 399: 0 = entries from 09:15:00 (group 390: 0.5, group 268: 5 min)
OPENING_GATE_MAX_GAP_UP_PCT = _get_float("OPENING_GATE_MAX_GAP_UP_PCT", 3.0)       # open vs previous close; 0 disables
OPENING_GATE_MAX_GAP_DOWN_PCT = _get_float("OPENING_GATE_MAX_GAP_DOWN_PCT", 1.0)   # gap DOWN larger than this is skipped; 0 disables
OPENING_GATE_MAX_RANGE_POS = _get_float("OPENING_GATE_MAX_RANGE_POS", 0.88)        # stricter than MAX_ENTRY_RANGE_POSITION; 0 disables
OPENING_GATE_OR_MINUTES = _get_int("OPENING_GATE_OR_MINUTES", 15)                  # opening range = first N minutes from 09:15
OPENING_GATE_MIN_OR_POS = _get_float("OPENING_GATE_MIN_OR_POS", 0.5)               # after the OR is built: price in its upper half or above it
OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT = _get_float("OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT", 1.0)  # ...but not extended more than this above OR high
OPENING_GATE_MIN_OR_TICKS = _get_int("OPENING_GATE_MIN_OR_TICKS", 10)             # fewer opening-range ticks (e.g. after a restart) = skip
OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT = _get_float("OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT", 0.0)
OPENING_GATE_NIFTY_MIN_VS_PREV_PCT = _get_float("OPENING_GATE_NIFTY_MIN_VS_PREV_PCT", 0.0)

# group 269 shadow mode REMOVED (group370): the opening gate is always live; OPENING_GATE_ENABLED=false is its only off switch.
# (old text: OPENING_GATE_SHADOW=1 -> while the gate is active (09:15 until the settle time) NO entry is placed;
# a symbol that passes every gate check is logged once per day as SKIPPED "OPENING_SHADOW:WOULD_ENTER ltp=..." (with the same
# gap/range/OR/Nifty numbers as an ENTERED row) so the 09:15 entries can be judged against later prices before real money is
# used. Rejected symbols are logged as before. Default 0 = the gate is live (group 268 behaviour).

# ── Opening gate: previous-day candle checks (group 270, 2026-10-09) ─────────
# Read from market-data-service /history (daily candles) by a background thread (screening/prev_day.py), never on the entry
# path. Inside the gate window (09:15 - OPENING_GATE_SETTLE_IST) the entry is skipped when the data is not cached yet (fails
# closed; the next 10 s scan finds it).
MARKET_DATA_URL = _env_url("MARKET_DATA_URL", "http://market-data-service:8001")
OPENING_GATE_PREVDAY_TIMEOUT_S = _get_float("OPENING_GATE_PREVDAY_TIMEOUT_S", 8.0)
OPENING_GATE_MIN_PREVDAY_CLOSE_POS = _get_float("OPENING_GATE_MIN_PREVDAY_CLOSE_POS", 0.5)   # previous day closed in the upper half of its range; 0 disables
OPENING_GATE_MIN_STOP_ATR_FRAC = _get_float("OPENING_GATE_MIN_STOP_ATR_FRAC", 0.3)           # stop % must be >= this x daily ATR %; 0 disables
STOP_ATR_FLOOR_AFTER_WINDOW = _get_bool("STOP_ATR_FLOOR_AFTER_WINDOW", True)   # E229: keep the stop >= OPENING_GATE_MIN_STOP_ATR_FRAC x daily ATR % after the opening window too (set false to restore the old behaviour)
OPENING_GATE_PREVDAY_MAX_AGE_DAYS = int(_get_float("OPENING_GATE_PREVDAY_MAX_AGE_DAYS", 6.0))   # group 273: a last daily candle older than this is treated as no data (stale history); 6 covers a long weekend + holiday


# ── Market Board (group305-310) ───────────────────────────────────────────────
# Read-only board (heatmap + stock popup) built from the tick stream already in memory, plus shadow logging of the new signals.
# NOTHING here can place an order unless BOARD_GATE_MODE=enforce. Every value is blank-safe (_get_*).
BOARD_ENABLED = _get_bool("BOARD_ENABLED", True)
BOARD_UNIVERSE_DEFAULT = _get_str("BOARD_UNIVERSE_DEFAULT", "all").lower()          # all | fno | liquid
if BOARD_UNIVERSE_DEFAULT not in ("all", "fno", "liquid"):
    BOARD_UNIVERSE_DEFAULT = "all"
BOARD_ROW_LIMIT = min(300, max(10, _get_int("BOARD_ROW_LIMIT", 130)))               # max rows per side
BOARD_TOP_SYMBOLS = max(50, _get_int("BOARD_TOP_SYMBOLS", 400))                     # bar store keeps this many by turnover
BOARD_MIN_DAY_VOLUME = max(0, _get_int("BOARD_MIN_DAY_VOLUME", 200000))             # shares; below it a symbol is not tracked
BOARD_LIQUID_TOP_N = max(10, _get_int("BOARD_LIQUID_TOP_N", 100))                   # the "liquid" universe = top N by turnover
BOARD_ONESIDED_RATIO = max(1.0, _get_float("BOARD_ONESIDED_RATIO", 2.0))
BOARD_CACHE_S = max(0.0, _get_float("BOARD_CACHE_S", 1.0))
BOARD_ZONES_ENABLED = _get_bool("BOARD_ZONES_ENABLED", True)
BOARD_ZONE_TF_MIN = min(15, max(1, _get_int("BOARD_ZONE_TF_MIN", 5)))
BOARD_ZONE_ATR_MULT = max(0.5, _get_float("BOARD_ZONE_ATR_MULT", 1.5))
BOARD_USE_MS_ENGINE = _get_bool("BOARD_USE_MS_ENGINE", True)                          # Group 349 / live since group354: Market Board zones/swings from stockky_ms.compat_board (any error falls back to the legacy code per call; set 0 to force legacy)
BOARD_AB_ENABLED = _get_bool("BOARD_AB_ENABLED", True)                                 # group362: while the engine serves the board, also run the legacy maths on the same bars and count the differences (observational, never changes what is served)
BOARD_AB_MAX_COMPARES = max(0, _get_int("BOARD_AB_MAX_COMPARES", 100000))             # comparisons per process (0 = no cap)
BOARD_ZONE_FULL_RANGE = _get_bool("BOARD_ZONE_FULL_RANGE", False)                   # zone = whole candle instead of body + wick
BOARD_CHASE_ATR = max(0.0, _get_float("BOARD_CHASE_ATR", 1.5))                      # > this many ATRs past the zone = "extended"
# off = nothing; shadow = log what the chase guard WOULD refuse; enforce = actually refuse those entries (fails open on error)
# group354: default is now "enforce" (the chase guard only ever REFUSES an entry, and fails open on missing data). "auto" = shadow until the shadow report's written promotion rule for the chase guard is met (BOARD_PROMOTE_MIN_OBS
# observations over BOARD_PROMOTE_MIN_SESSIONS sessions, bootstrap CI says what it blocks does clearly worse), then enforce; re-checked
# every BOARD_AUTO_REFRESH_S over the last BOARD_AUTO_DAYS days and drops back to shadow if the verdict stops holding.
BOARD_GATE_MODE = _get_str("BOARD_GATE_MODE", "enforce").lower()
if BOARD_GATE_MODE != "off":
    BOARD_GATE_MODE = "enforce"          # group370: always live; a stale "shadow" / "auto" in an old .env is enforce
# group354: DAILY MARKET-STRUCTURE GATE on the automatic scalp entry (orders/structure_gate.py). "Good scalp, bad daily location?" - reads the
# technical service's daily market_structure summary via api-gateway GET /stock/{symbol}/structure (cached, fail-open: no / stale /
# insufficient structure = no opinion, never a block). LIVE by default; the scalper buys strength on purpose, so the defaults block only the
# clear daily-structure no-gos (bearish daily structure, a fresh bearish BOS, almost no room under the next daily resistance, an extreme
# stretch / chase). Set a limit to 0 to switch that single test off.
#   STRUCTURE_GATE_MODE   enforce (default, always live: refuse with STRUCTURE_* reason) | log (E72: evaluate + record, never refuses; ONLY via PS_STRUCTURE_GATE_MODE) | off (kill switch); a stale "shadow" = enforce
def _ps(name: str) -> str:
    """group365/372: the scalper may stay looser than real-trade on the SHARED structure names. `PS_<name>` wins when it is set (a blank one is ignored),
    otherwise the shared `<name>` is read as before."""
    return "PS_" + name if (os.getenv("PS_" + name) or "").strip() else name


# E72: a third value, `log` (evaluate + record + store the snapshot, NEVER refuse), selectable ONLY through the explicit PS_STRUCTURE_GATE_MODE. The shared
# name `STRUCTURE_GATE_MODE` is Real Trade's switch (shipped `log`), so a shared `STRUCTURE_GATE_MODE=log` must NOT quietly loosen the scalper: through
# the shared name anything but "off" still means enforce, exactly as before. Default stays enforce.
_gate_mode_raw = _get_str(_ps("STRUCTURE_GATE_MODE"), "enforce").lower()
STRUCTURE_GATE_MODE = "off" if _gate_mode_raw == "off" else ("log" if _gate_mode_raw == "log" and _ps("STRUCTURE_GATE_MODE").startswith("PS_") else "enforce")
STRUCTURE_REQUIRE_DATA = _get_bool(_ps("STRUCTURE_REQUIRE_DATA"), True)                     # true = refuse (STRUCTURE_NODATA) when no usable daily structure
STRUCTURE_BEARISH_BLOCK = _get_bool(_ps("STRUCTURE_BEARISH_BLOCK"), True)                    # bearish daily trend / fresh (not failed) bearish BOS
STRUCTURE_BEARISH_BOS_BARS = max(1, _get_int("STRUCTURE_BEARISH_BOS_BARS", 5))
# E72/E73 (D-vol, Real Trade parity): a fresh bullish daily breakout on thin volume. enforce (default since E73) = a refusal reason (STRUCTURE_* skip); log = only an OBSERVATION on the verdict / snapshot / counters; off. Needs ms_bos_vol_ratio in the structure summary; without it = no opinion.
_bv_raw = _get_str(_ps("STRUCTURE_BREAKOUT_VOLUME_MODE"), "enforce").lower()
# E73: enforce is the shipped default (everything live). Same rule as the gate itself: `log` only through the explicit PS_ name, so Real Trade's shared name cannot loosen the scalper.
STRUCTURE_BREAKOUT_VOLUME_MODE = "off" if _bv_raw == "off" else ("log" if _bv_raw == "log" and _ps("STRUCTURE_BREAKOUT_VOLUME_MODE").startswith("PS_") else "enforce")
STRUCTURE_BREAKOUT_MIN_VOL_RATIO = max(0.0, _get_float(_ps("STRUCTURE_BREAKOUT_MIN_VOL_RATIO"), 1.2))   # same 1.2 x average as the candle volume_confirmed flag; 0 = off
STRUCTURE_BREAKOUT_BARS = max(1, _get_int("STRUCTURE_BREAKOUT_BARS", 5))                                # "fresh" = the breakout was at most this many daily bars ago
STRUCTURE_MAX_CHASE_ATR = max(0.0, _get_float(_ps("STRUCTURE_MAX_CHASE_ATR"), 4.0))          # live price above the nearest daily demand/imbalance zone top, in daily ATR
STRUCTURE_MAX_STRETCH_ATR = max(0.0, _get_float(_ps("STRUCTURE_MAX_STRETCH_ATR"), 5.0))      # live price above the daily EMA20, in daily ATR
STRUCTURE_MIN_ROOM_ATR = max(0.0, _get_float(_ps("STRUCTURE_MIN_ROOM_ATR"), 0.3))            # room to the next daily resistance, in daily ATR
STRUCTURE_MAX_AGE_DAYS = max(0.0, _get_float("STRUCTURE_MAX_AGE_DAYS", 5.0))            # (group415: was 7) last structure bar older than this = stale = no opinion
STRUCTURE_MAX_PRICE_DEV_PCT = max(0.0, _get_float("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0)) # live price vs structure close further apart = split / bad data = no opinion
STRUCTURE_FETCH_TIMEOUT_S = max(0.5, _get_float("STRUCTURE_FETCH_TIMEOUT_S", 2.0))
STRUCTURE_CACHE_TTL_S = max(30.0, _get_float("STRUCTURE_CACHE_TTL_S", 600.0))
STRUCTURE_FAIL_TTL_S = max(10.0, _get_float("STRUCTURE_FAIL_TTL_S", 60.0))
# group356/372 (WS-6): what the gate saw per symbol / IST day and what the stock did over the next 1/5/10/20 sessions (scalp_structure_observations,
# background evaluator, GET /structure/edge). Read-only evidence: nothing in the entry path reads it back. STRUCTURE_OBS_ENABLED=false = record and evaluate nothing.
STRUCTURE_OBS_ENABLED = _get_bool("STRUCTURE_OBS_ENABLED", True)
STRUCTURE_OBS_EVAL_INTERVAL_S = max(60.0, _get_float("STRUCTURE_OBS_EVAL_INTERVAL_S", 3600.0))
STRUCTURE_OBS_BATCH = max(1, _get_int("STRUCTURE_OBS_BATCH", 25))                       # symbols per evaluator pass
STRUCTURE_OBS_HISTORY_TIMEOUT_S = max(1.0, _get_float("STRUCTURE_OBS_HISTORY_TIMEOUT_S", 8.0))
STRUCTURE_OBS_GIVE_UP_DAYS = max(5.0, _get_float("STRUCTURE_OBS_GIVE_UP_DAYS", 45.0))    # no bars after the day for this long = closed
STRUCTURE_OBS_BENCH_SYMBOL = (os.getenv("STRUCTURE_OBS_BENCH_SYMBOL") or "NIFTY").strip()   # group402: market benchmark for the edge report (market-data /history name); empty = no benchmark
STRUCTURE_EDGE_MIN_N = max(1, _get_int("STRUCTURE_EDGE_MIN_N", 100))                    # finished rows per side before the edge report draws a conclusion
BOARD_LOG_EVERY_S = max(60, _get_int("BOARD_LOG_EVERY_S", 300))                     # shadow snapshot cadence in market hours
BOARD_LOG_TOP_N = max(1, _get_int("BOARD_LOG_TOP_N", 20))                           # rows per side per snapshot
BOARD_CONVICTION_WEIGHTS = _get_str("BOARD_CONVICTION_WEIGHTS", "")                 # "strength=0.5,volume=0.2,..." (optional)
FNO_SYMBOLS_EXTRA = _get_str("FNO_SYMBOLS_EXTRA", "")                               # comma separated, added to the F&O list
FNO_SYMBOLS_EXCLUDE = _get_str("FNO_SYMBOLS_EXCLUDE", "")                           # comma separated, removed from it
FNO_LIST_REFRESH = _get_bool("FNO_LIST_REFRESH", True)                              # daily background refresh from Dhan's list
FNO_LIST_URL = _get_str("FNO_LIST_URL", "https://images.dhan.co/api-data/api-scrip-master.csv")
# shadow report / promotion rule (group309)
BOARD_PROMOTE_MIN_OBS = max(1, _get_int("BOARD_PROMOTE_MIN_OBS", 30))
BOARD_PROMOTE_MIN_SESSIONS = max(1, _get_int("BOARD_PROMOTE_MIN_SESSIONS", 10))
# E237: /board/shadow-report result cache (seconds); 0 disables. A stale entry is served at once while a refresh runs in the background.
BOARD_REPORT_CACHE_S = max(0, _get_int("BOARD_REPORT_CACHE_S", 300))
BOARD_COST_PCT = max(0.0, _get_float("BOARD_COST_PCT", 0.12))                       # flat round-trip cost estimate, % of price
BOARD_BACKFILL_ENABLED = _get_bool("BOARD_BACKFILL_ENABLED", True)                  # group311: fill today's earlier minutes after a mid-session restart
BOARD_BACKFILL_TOP_N = max(1, _get_int("BOARD_BACKFILL_TOP_N", 120))                 # backfill the N most liquid symbols that start late
BOARD_BACKFILL_PER_CYCLE = max(1, _get_int("BOARD_BACKFILL_PER_CYCLE", 6))          # history fetches per runner pass (6 s apart while a queue exists)
BOARD_BACKFILL_TIMEOUT_S = max(1.0, _get_float("BOARD_BACKFILL_TIMEOUT_S", 8.0))
BOARD_BACKFILL_RETRY_S = max(60, _get_int("BOARD_BACKFILL_RETRY_S", 900))            # a symbol is retried no sooner than this (hit or miss)
# group345: an empty / failed history answer is not asked again for this long, and a mostly-empty batch pauses the backfill (see feed/bar_backfill.py)
BOARD_BACKFILL_EMPTY_RETRY_S = max(60, _get_int("BOARD_BACKFILL_EMPTY_RETRY_S", 3600))
BOARD_BACKFILL_PAUSE_S = max(30, _get_int("BOARD_BACKFILL_PAUSE_S", 300))
BOARD_PARAMS_VERSION = "b1"                                                          # bump when a BOARD_* default or formula changes
# group441 (WS-15): record today's 1-minute bars (from the bar store) to disk, one file per symbol per IST day, so intraday (15m / 5m) structure and the
# opening / VWAP / depth gates can be backtested later. RECORD ONLY: nothing in the entry path reads it. Needs a persistent volume at BAR_RECORD_DIR
# (docker-compose mounts `bar-records`); an unwritable directory switches the recorder off by itself and /board/status says why.
BAR_RECORD_ENABLED = _get_bool("BAR_RECORD_ENABLED", True)
BAR_RECORD_DIR = _get_str("BAR_RECORD_DIR", "/data/bars")
BAR_RECORD_EVERY_S = max(60, _get_int("BAR_RECORD_EVERY_S", 300))
BAR_RECORD_MAX_SYMBOLS = max(10, _get_int("BAR_RECORD_MAX_SYMBOLS", 250))
BAR_RECORD_MIN_BARS = max(1, _get_int("BAR_RECORD_MIN_BARS", 20))
BAR_RECORD_KEEP_DAYS = max(2, _get_int("BAR_RECORD_KEEP_DAYS", 45))

# ---- E232 (2026-10-27): why a mover was not picked -------------------------------------------------------------------------------
# The scan ranks every symbol that clears a rolling 5m / 60m move, but only the top SHORTLIST_MAX names per cycle reach the quality gate and
# the candidate log; the rest left no trace. SCAN_HITS records every scan hit (symbol, window, move, rank, whether it was quality-checked) in
# memory and to SCAN_HITS_DIR (JSON, one file per IST day), read by GET /scan/hits and GET /scan/why/{symbol}. RECORD ONLY: nothing reads it to trade.
SCAN_HITS_ENABLED = _get_bool("SCAN_HITS_ENABLED", True)
SCAN_HITS_DIR = _get_str("SCAN_HITS_DIR", "/data/bars/_scan_hits")
SCAN_HITS_DUMP_S = max(10, _get_int("SCAN_HITS_DUMP_S", 30))
SCAN_HITS_KEEP_DAYS = max(2, _get_int("SCAN_HITS_KEEP_DAYS", 30))
# DAY_MOVER: a stock that climbs (or slides) steadily all day, or gaps at the open, never shows +1% in 5 minutes or +2.5% in an hour, so the rolling
# windows never see it (2026-10-27: Zetwerk +12.5%, Mahindra Logistics +11.3%, Data Patterns +9.2%). With DAY_MOVER_ENABLED the scan also lists a stock whose
# move since the previous close is between DAY_MOVER_MIN_PCT and DAY_MOVER_MAX_PCT and that no rolling window listed, as window "day". It passes the same
# volume floor, spread cap, quality gate, range gate, stop-size floor and sizing as any candidate; nothing is relaxed. Score = min(move, 2 x MIN) x
# DAY_MOVER_SCORE_WEIGHT x liquidity x range x VWAP multipliers (a day mover ranks beside a rolling hit, not above it). DAY_MOVER_SHORT_ENABLED mirrors it
# for the short scan (shadow / live as SHORT_MODE says). DAY_MOVER_ENABLED=0 restores the old scan exactly.
DAY_MOVER_ENABLED = _get_bool("DAY_MOVER_ENABLED", True)
DAY_MOVER_SHORT_ENABLED = _get_bool("DAY_MOVER_SHORT_ENABLED", True)
DAY_MOVER_MIN_PCT = max(1.0, _get_float("DAY_MOVER_MIN_PCT", 4.0))
DAY_MOVER_MAX_PCT = max(DAY_MOVER_MIN_PCT, _get_float("DAY_MOVER_MAX_PCT", 20.0))
DAY_MOVER_SCORE_WEIGHT = max(0.05, _get_float("DAY_MOVER_SCORE_WEIGHT", 0.5))

# ---- group341 (log review 2026-10-13) -------------------------------------------------------------------------------------------
# Pool baseline: sync_from_broker() adds back this pool's committed capital to Dhan's FREE cash. Free cash only falls by the MARGIN an
# intraday order blocks (about 20% at 5x MIS - TECHM: cash -Rs596.61 for a Rs2,978.60 position), so adding back the full notional
# inflated the baseline by ~28% per open position (Rs9,446 -> Rs12,126). Only this fraction of capital_risked is added back.
# 1.0 = old behaviour (cash-funded / CNC positions).
SCALP_POOL_COMMITTED_MARGIN_FRAC = _get_float("SCALP_POOL_COMMITTED_MARGIN_FRAC", 0.20)
# Per-slot notional cap = (pool / MAX_CONCURRENT_SCALP_POSITIONS) x this. risk/stop alone gave a 0.8% stop a position of 50% of the
# pool (the 5-slot split assumed stops >= 2%), so two positions used the whole pool. 0 = no cap (old behaviour).
SCALP_SLOT_NOTIONAL_CAP_MULT = _get_float("SCALP_SLOT_NOTIONAL_CAP_MULT", 1.5)
# Quality gate: a fully-fetched result is reused for this many seconds (the 10 s cycle re-fetched 3 calls per shortlisted symbol each
# time, ~24 technical calls for one rejected symbol in 7 minutes, and the self-inflicted load caused the ReadTimeouts). 0 = off.
QUALITY_GATE_RESULT_TTL_S = _get_float("QUALITY_GATE_RESULT_TTL_S", 90.0)
# When the event call fails, the last earnings date / negative-news reason seen for the symbol within this many hours still applies
# (the EARNINGS_TODAY and NEWS_NEGATIVE blocks were silently skipped on every timeout). 0 = off.
QUALITY_GATE_EVENT_MEMORY_H = _get_float("QUALITY_GATE_EVENT_MEMORY_H", 24.0)
# Tick buffer: per-symbol cap (was a fixed 50,000 ticks x 2,600 subscribed symbols; the service grew 63 -> 364 MB of 400 in 20 minutes).
WS_MAX_TICKS_PER_SYMBOL = _get_int("WS_MAX_TICKS_PER_SYMBOL", 4000)


# group344 (13 Oct log: RAMRAT entered after "event fetch failed (ReadTimeout)", i.e. with no news / earnings check).
# When the event call fails and nothing is remembered for the symbol (group 341 memory), the gate retries once with a longer timeout; if that
# also fails, the candidate is rejected as EVENT_CHECK_UNAVAILABLE (it is looked at again next cycle) instead of passing unchecked.
# Safety valve: if the event service has failed continuously for QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S seconds the gate fails open again and says so.
# QUALITY_GATE_EVENT_FAIL_BLOCK=0 restores the old always-fail-open behaviour.
QUALITY_GATE_EVENT_FAIL_BLOCK = _get_bool("QUALITY_GATE_EVENT_FAIL_BLOCK", True)
QUALITY_GATE_EVENT_RETRY_TIMEOUT_S = _get_float("QUALITY_GATE_EVENT_RETRY_TIMEOUT_S", 8.0)  # group356: 6 -> 8 (extra wait on the SAME in-flight request)
QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S = _get_float("QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S", 900.0)
# group356 (14 Oct 10:38 IST restart log: event / technical / fundamental all hit ReadTimeout for 7 symbols together, V2RETAIL and EMBDL
# were skipped as EVENT_CHECK_UNAVAILABLE, and the event answers arrived a few seconds AFTER the gate gave up - the retry had just
# sent a SECOND request to a service that was already busy with the first, and the late answer was thrown away).
# With QUALITY_GATE_EVENT_SHARED_FETCH on (default) the event call runs as ONE in-flight request per symbol (hard limit
# QUALITY_GATE_EVENT_BG_TIMEOUT_S, default 45 s) that the gate WAITS on: QUALITY_GATE_TIMEOUT_S first, then up to
# QUALITY_GATE_EVENT_RETRY_TIMEOUT_S more on the same request (no second request). If it still has not answered, the request keeps
# running and its answer is kept for QUALITY_GATE_EVENT_LATE_TTL_S seconds, so the next cycle uses it instead of starting over.
# Only a symbol that is still unanswered is rejected as EVENT_CHECK_UNAVAILABLE, exactly as before. QUALITY_GATE_EVENT_SHARED_FETCH=0
# restores the group344 behaviour (a second request with its own timeout).
QUALITY_GATE_EVENT_SHARED_FETCH = _get_bool("QUALITY_GATE_EVENT_SHARED_FETCH", True)
QUALITY_GATE_EVENT_BG_TIMEOUT_S = max(5.0, _get_float("QUALITY_GATE_EVENT_BG_TIMEOUT_S", 45.0))
QUALITY_GATE_EVENT_LATE_TTL_S = max(0.0, _get_float("QUALITY_GATE_EVENT_LATE_TTL_S", 300.0))
QUALITY_GATE_EVENT_PREWARM_EXTRA = max(0, _get_int("QUALITY_GATE_EVENT_PREWARM_EXTRA", 3))   # E229: also start the event check of this many names ranked just below the quality-gate batch (0 = off)
# Price floor applied BEFORE the quality gate (HATHWAY at Rs10.6 used the gate's three calls although entry.py would reject it as PENNY_STOCK).
# Uses MIN_STOCK_PRICE and ENTRY_MIN_PRICE; PRICE_FLOOR_BEFORE_GATE=0 turns this pre-filter off (entry.py still enforces the floor).
PRICE_FLOOR_BEFORE_GATE = _get_bool("PRICE_FLOOR_BEFORE_GATE", True)
# Today's closed TARGET/STOP rows: the order-book exit lookup is done once per row per process once it has confirmed the fill.
EXIT_FILL_CONFIRM_ONCE = _get_bool("EXIT_FILL_CONFIRM_ONCE", True)


# ---- group 479 (2026-10-21): position-stocks parity with real-trade groups 470-472 -------------------------------------------------
# Whole-day stop-out guard (orders/stopout_guard.py): after STOPOUT_DAY_PAUSE_N LOSING stop-outs today (STOP_HIT / STOP_BREACH_EXIT) no new
# automatic entry until the next session; a symbol stopped out today is not re-bought today. Counted from scalp_positions (a restart does not reset
# it). The group 392 opening pause (2 stop-outs until 09:45) stays and fires first inside its window. STOPOUT_GUARD_ENABLED=false = rollback.
STOPOUT_GUARD_ENABLED = _get_bool("STOPOUT_GUARD_ENABLED", True)
STOPOUT_DAY_PAUSE_N = _get_int("STOPOUT_DAY_PAUSE_N", 3)            # 0 = no day pause
STOPOUT_SYMBOL_BLOCK = _get_bool("STOPOUT_SYMBOL_BLOCK", True)

# ── E251 (2026-10-28): ARVIND bought 518.15 right after a spike, 1.2 % stop hit one minute later, then the stock ran to the target. ───────────
# 1. Volatility-aware stop floor (orders/adaptive.py::apply_stop_floor): the stop is never smaller than ADAPTIVE_STOP_DAILY_ATR_FRAC x the stock's
#    own daily ATR % nor ADAPTIVE_STOP_M1_MULT x its average 1-minute range (last 5 min), capped at ADAPTIVE_STOP_FLOOR_MAX_PCT; the target moves with it (2:1).
ADAPTIVE_STOP_FLOOR_ENABLED = _get_bool("ADAPTIVE_STOP_FLOOR_ENABLED", True)
ADAPTIVE_STOP_DAILY_ATR_FRAC = _get_float("ADAPTIVE_STOP_DAILY_ATR_FRAC", 0.45)
ADAPTIVE_STOP_M1_MULT = _get_float("ADAPTIVE_STOP_M1_MULT", 2.0)
ADAPTIVE_STOP_FLOOR_MAX_PCT = _get_float("ADAPTIVE_STOP_FLOOR_MAX_PCT", 1.5)
# 2. Spike-chase guard (orders/entry_timing.py): no MARKET buy at the top of a day spike / mid-thrust; the symbol is armed for the pullback re-arm instead.
#    The day-spike and thrust limits scale with the stock's own daily ATR / 1-minute range (the fixed numbers are the fallback when those are unknown).
SPIKE_GUARD_MODE = _get_str("SPIKE_GUARD_MODE", "live")                  # live (refuse) | log | off
SPIKE_DAY_PCT = _get_float("SPIKE_DAY_PCT", 4.0)                         # fallback day-gain limit when the daily ATR is unknown; 0 = day-spike test off
SPIKE_DAY_ATR_MULT = _get_float("SPIKE_DAY_ATR_MULT", 1.2)               # adaptive: limit = this x daily ATR % ...
SPIKE_DAY_MIN_PCT = _get_float("SPIKE_DAY_MIN_PCT", 2.5)                 # ... clamped to this range
SPIKE_DAY_MAX_PCT = _get_float("SPIKE_DAY_MAX_PCT", 6.0)
SPIKE_RANGE_POS = _get_float("SPIKE_RANGE_POS", 0.85)                    # in the top 15 % of today's range
SPIKE_THRUST_PCT = _get_float("SPIKE_THRUST_PCT", 1.5)                   # fallback: rise inside the window; 0 = thrust test off
SPIKE_THRUST_M1_MULT = _get_float("SPIKE_THRUST_M1_MULT", 3.0)           # adaptive: limit = this x average 1-minute range ...
SPIKE_THRUST_MIN_PCT = _get_float("SPIKE_THRUST_MIN_PCT", 1.0)           # ... clamped to this range
SPIKE_THRUST_MAX_PCT = _get_float("SPIKE_THRUST_MAX_PCT", 2.5)
SPIKE_THRUST_WINDOW_S = _get_float("SPIKE_THRUST_WINDOW_S", 180.0)
SPIKE_NEAR_HIGH_PCT = _get_float("SPIKE_NEAR_HIGH_PCT", 0.25)            # within this of the window high = still at the top of the thrust
SPIKE_MIN_TICKS = _get_int("SPIKE_MIN_TICKS", 8)                         # fewer ticks in the window = no thrust opinion
# 3. Gain-room gate (orders/gain_room.py): the target has to be realistically reachable today (replayed odds + share of the normal day range left).
GAIN_ROOM_MODE = _get_str("GAIN_ROOM_MODE", "live")                      # live (refuse) | log | off
GAIN_ROOM_ODDS_MIN_RESOLVED = _get_int("GAIN_ROOM_ODDS_MIN_RESOLVED", 25)
GAIN_ROOM_ODDS_MARGIN_PTS = _get_float("GAIN_ROOM_ODDS_MARGIN_PTS", 8.0)  # refuse when the replayed target-first rate is this many points under break-even
GAIN_ROOM_RANGE_MULT = _get_float("GAIN_ROOM_RANGE_MULT", 2.0)           # expected day high = today's low x (1 + this x average daily range %)
GAIN_ROOM_MIN_TARGET_FRAC = _get_float("GAIN_ROOM_MIN_TARGET_FRAC", 0.5)  # room to that high must be at least this share of the target; 0 = range test off
# 4. One reclaim re-entry (orders/stopout_guard.py::reclaim_decision): a symbol whose only trade today was one losing stop-out may be bought once more
#    after it reclaims the old entry. Every other gate (and the day pause) still applies.
REENTRY_RECLAIM_ENABLED = _get_bool("REENTRY_RECLAIM_ENABLED", True)
REENTRY_RECLAIM_MAX_PER_SYMBOL = _get_int("REENTRY_RECLAIM_MAX_PER_SYMBOL", 1)   # 0 = off
REENTRY_RECLAIM_MIN_WAIT_S = _get_float("REENTRY_RECLAIM_MIN_WAIT_S", 120.0)
REENTRY_RECLAIM_MAX_AGE_MIN = _get_float("REENTRY_RECLAIM_MAX_AGE_MIN", 120.0)
REENTRY_RECLAIM_ABOVE_ENTRY_PCT = _get_float("REENTRY_RECLAIM_ABOVE_ENTRY_PCT", 0.1)   # price must be this far above the old entry ...
REENTRY_RECLAIM_MAX_TARGET_FRAC = _get_float("REENTRY_RECLAIM_MAX_TARGET_FRAC", 0.5)   # ... and not past this share of the way to the old target (that is chasing)
# E220: a manual BUY is refused while the market is closed and after today's one-shot EOD sweep has run (orders/manual_buy_window.py). false = the old
# behaviour (the manual route checked neither). A money-safety rule: not part of any report-driven auto-calibration.
MANUAL_BUY_WINDOW_GUARD = _get_bool("MANUAL_BUY_WINDOW_GUARD", True)
# Minimum whole shares per automatic entry, checked after every sizing step (VIX / calendar / opening half-size). Below it = SKIPPED (MIN_QTY).
# 1-2 share trades cannot scale out or trail and pay the same fixed costs. 0 = off. The forced 1-share first live order is never gated.
ENTRY_MIN_QTY = max(0, _get_int("ENTRY_MIN_QTY", 3))
# Trade-odds recorder (orders/trade_odds.py, E103): RECORD ONLY. flow_json["odds"] on every new long and short says how often this trade's own
# stop % / target % pair had the target come first when replayed on the stock's 1-minute bars so far today. Nothing is blocked, sized or ordered.
TRADE_ODDS_MODE = _get_str("TRADE_ODDS_MODE", "shadow").lower()      # shadow | off
if TRADE_ODDS_MODE not in ("shadow", "off"):
    TRADE_ODDS_MODE = "shadow"
# Time Machine (timemachine/, E111): EVIDENCE ONLY. Replays one stock's recent 5-minute sessions and thousands of made-up sessions with a given entry time,
# stop % and target %, and says how often the target comes first, after charges. It places, sizes and blocks nothing; off stops the routes answering.
TIME_MACHINE_ENABLED = _get_bool("TIME_MACHINE_ENABLED", True)
TIME_MACHINE_SIMS = min(50000, max(1000, _get_int("TIME_MACHINE_SIMS", 10000)))       # made-up sessions per run (a request may ask for 1,000 to 50,000)
TIME_MACHINE_SLIPPAGE_PCT = min(1.0, max(0.0, _get_float("TIME_MACHINE_SLIPPAGE_PCT", 0.05)))   # % per market leg (entry, stop exit, flat-by exit); the target leg is a limit
TIME_MACHINE_LOOKBACK = _get_str("TIME_MACHINE_LOOKBACK", "1mo").lower()              # 1mo | 2mo (2mo asks market-data for an exact ~58-day window, falls back to 1mo)
if TIME_MACHINE_LOOKBACK not in ("1mo", "2mo"):
    TIME_MACHINE_LOOKBACK = "1mo"
TIME_MACHINE_DEFAULT_VALUE_RS = max(1000.0, _get_float("TIME_MACHINE_DEFAULT_VALUE_RS", 20000.0))   # trade value used for the charges when the request gives none
TIME_MACHINE_FETCH_TIMEOUT_S = max(5.0, _get_float("TIME_MACHINE_FETCH_TIMEOUT_S", 25.0))

# E115: the Time Machine in LIVE stock picking (timemachine/gate_core.py, orders/tm_gate.py). RANK: a stock whose own recent 5-minute sessions and a made-up-session
# simulation both favour (or both oppose) the rule gets +/- TM_PICK_NUDGE_PTS in the shortlist. GATE: an entry whose own stop % / target % at this time of day
# shows AGAINST (simulation and real history both negative after charges and slippage) is refused. It can only say NO. Missing or slow data = no opinion.
# TM_GATE_MODE: live (a refusal blocks the entry) | log (compute and record, never block) | off. Rollback: TM_GATE_MODE=log (or off).
TM_GATE_MODE = _get_str("TM_GATE_MODE", "live").lower()
if TM_GATE_MODE not in ("live", "log", "off"):
    TM_GATE_MODE = "off"                                                     # a typo must not switch a gate on
TM_GATE_BLOCK = _get_str("TM_GATE_BLOCK", "against").lower()                  # against | against_or_near (also refuse a near-break-even verdict)
if TM_GATE_BLOCK not in ("against", "against_or_near"):
    TM_GATE_BLOCK = "against"
TM_GATE_SIMS = min(20000, max(1000, _get_int("TM_GATE_SIMS", 3000)))          # made-up sessions per gate verdict (3,000 takes about a quarter of a second)
TM_GATE_INLINE_BUDGET_S = min(15.0, max(0.0, _get_float("TM_GATE_INLINE_BUDGET_S", 2.5)))   # an uncached entry waits at most this long; 0 = cache only
TM_GATE_WINDOW_BUDGET_S = min(120.0, max(0.0, _get_float("TM_GATE_WINDOW_BUDGET_S", 8.0)))   # total seconds entries may wait inline in any one minute (0 = no cap); beyond it checks are queued, no opinion
TM_GATE_TTL_S = min(3600.0, max(30.0, _get_float("TM_GATE_TTL_S", 300.0)))   # how long a verdict is reused
TM_GATE_FAIL_TTL_S = min(1800.0, max(10.0, _get_float("TM_GATE_FAIL_TTL_S", 120.0)))   # a failed fetch is not retried for this long
TM_PICK_NUDGE_PTS = min(30.0, max(0.0, _get_float("TM_PICK_NUDGE_PTS", 6.0)))   # ranking points (0 = no ranking effect)
TM_AUTO_ENABLED = _get_bool("TM_AUTO_ENABLED", True)                          # the timer that keeps the verdicts of the current picks warm
TM_AUTO_PREFETCH_IST = _get_str("TM_AUTO_PREFETCH_IST", "09:05")              # once a day: load the 5-minute history of the current picks before the open
TM_AUTO_FIRST_IST = _get_str("TM_AUTO_FIRST_IST", "09:16")                    # first timed scan (the 09:15 bar is the first entry bar)
TM_AUTO_LAST_IST = _get_str("TM_AUTO_LAST_IST", "14:30")                      # last timed scan
TM_AUTO_INTERVAL_MIN = min(120, max(5, _get_int("TM_AUTO_INTERVAL_MIN", 15)))  # minutes between timed scans
TM_AUTO_MAX_SYMBOLS = min(40, max(1, _get_int("TM_AUTO_MAX_SYMBOLS", 12)))    # picks re-checked per scan
TM_AUTO_ACTIVE_MIN = min(600.0, max(5.0, _get_float("TM_AUTO_ACTIVE_MIN", 60.0)))   # a pick stays on the timer this long after it was last seen
# Opening-range-breakout recorder (orders/orb_shadow.py): RECORD ONLY. flow_json["orb"] on every new position says what a 5-minute opening-range
# entry (price above the 09:15-09:20 range high and above VWAP, tight spread, limit just above the high, stop = range low) would have done.
ORB_MODE = _get_str("ORB_MODE", "shadow").lower()                   # shadow | off
ORB_RANGE_END_IST = _get_str("ORB_RANGE_END_IST", "09:20")
ORB_CAPTURE_UNTIL_IST = _get_str("ORB_CAPTURE_UNTIL_IST", "09:26")
ORB_LATEST_ENTRY_IST = _get_str("ORB_LATEST_ENTRY_IST", "10:30")
ORB_LIMIT_BUFFER_PCT = max(0.0, _get_float("ORB_LIMIT_BUFFER_PCT", 0.10))
ORB_MAX_SPREAD_PCT = max(0.0, _get_float("ORB_MAX_SPREAD_PCT", 0.30))
ORB_MIN_RANGE_PCT = max(0.0, _get_float("ORB_MIN_RANGE_PCT", 0.30))
ORB_MAX_RANGE_PCT = max(0.0, _get_float("ORB_MAX_RANGE_PCT", 3.0))

# 2026-10-22 (reward:risk 1:2): hard floor on the stop / target distances of an AUTOMATIC entry, judged on the FINAL levels (after the target cap and
# the stop floors, which could leave 1.5:1). Below it the candidate is SKIPPED (RR_FLOOR). Same name and default as real-trade-service (2.0); 0 = off.
ENTRY_MIN_REWARD_RISK = max(0.0, _get_float("ENTRY_MIN_REWARD_RISK", 2.0))

# 2026-10-22: at order time move the stop / target legs to the freshest live tick (same stop % / target %) instead of the scan price, so a price move
# between the signal and the MARKET fill is not a hidden tight (or wide) stop. IONEXCHANG: signal 354.60, fill 352.30, stop 0.15 % under the fill, out in
# 21 s. Fails open (no fresh tick -> unchanged). 0 = old behaviour. orders/live_anchor.py.
ENTRY_ANCHOR_TO_LIVE = _get_bool("ENTRY_ANCHOR_TO_LIVE", True)
