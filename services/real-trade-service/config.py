"""
config.py — Real Automatic Trade service configuration.

Encodes the four decisions confirmed 2026-08-25:

  1. Entry style   : limit order inside a bounded entry zone, time-boxed
                      validity — never a chasing market order. See
                      ENTRY_* below; enforced in entry_engine (Phase 2).
  2. Risk defaults : conservative — 1% account risk per trade, 3% max
                      daily loss, 3 concurrent positions. These are
                      SEEDED into trade_risk_config on first boot and are
                      then admin-editable via the UI (only while
                      disarmed) — this module only supplies the seed.
  3. Database      : SAME Oracle Autonomous DB the rest of Stockky uses.
                      No separate DB — new tables, same instance. See
                      oracle_compat.py / db.py, which reuse the exact
                      ORACLE_* env contract every other service already
                      has on Render.
  4. Dhan token    : manual daily paste by default (DHAN_TOTP_ENABLED
                      defaults False). TOTP auto-refresh is wired as an
                      opt-in path in auth/dhan_credentials.py so flipping
                      it on later needs no code changes, only an env var
                      + the TOTP secret.

Nothing in this file talks to the network or the DB — it's pure
environment/constant resolution so every other module can import it
without side effects.
"""
from __future__ import annotations

import os

# ── Service identity ────────────────────────────────────────────────────────
SERVICE_NAME = "real-trade-service"
PORT = int(os.getenv("PORT", "8005"))

# ── Upstream Stockky services (recommendations only — this service never
#    writes back into api-gateway's data) ───────────────────────────────────
API_GATEWAY_URL = os.getenv("API_GATEWAY_URL", "https://stockky-api-gateway.onrender.com").rstrip("/")

# Short-Term Trading Upgrade (2026-09-02): analysis-intelligence-service's
# event sub-service, used only by watchlist_engine/sources.py's Tier 2
# fallback (raw catalyst feed, pre-scoring — see that module's docstring).
# Independent from API_GATEWAY_URL so Tier 2 keeps working via its own
# circuit breaker even when api-gateway (Tier 1) is unhealthy.
EVENT_URL = os.getenv("EVENT_URL", "https://stockky-event-tracker.onrender.com").rstrip("/")

# 2026-09-11 fix — needed for the volume-shock quality gate (see
# candidate_engine/candidates.py's _quality_gate_fund_tech). Mirrors the
# exact same ANALYSIS_INTELLIGENCE_URL / TECHNICAL_URL / FUNDAMENTAL_URL
# pattern decision-prediction-service and api-gateway already use — this
# service just never defined them because nothing here called into
# analysis-intelligence-service before now. docker-compose.yml sets
# TECHNICAL_URL/FUNDAMENTAL_URL explicitly for the container network; the
# onrender.com defaults below match the Render-hosted deployment already
# used by ANALYSIS_INTELLIGENCE_URL elsewhere in this codebase.
_ANALYSIS_INTELLIGENCE_URL = os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com").rstrip("/")
TECHNICAL_URL = os.getenv("TECHNICAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/technical").rstrip("/")
FUNDAMENTAL_URL = os.getenv("FUNDAMENTAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/fundamental").rstrip("/")

# ── Admin auth (Layer 1) ─────────────────────────────────────────────────────
# Argon2id hash of the admin password — generate once with:
#   python -c "from argon2 import PasswordHasher; print(PasswordHasher().hash('yourpassword'))"
# and paste the hash (never the plaintext) into Render's env. If unset, the
# service refuses to boot into a usable state (see main.py startup check) —
# there is no default password.
#
# IMPORTANT — the hash looks like $argon2id$v=19$m=65536,t=3,p=4$....
# The leading `$argon2id`, `$v=19`, `$m=...` segments are NOT dollar-sign
# escapes for you to strip; they're part of the hash. BUT if you put that
# raw string in a local .env file that docker-compose loads (not Render's
# own env UI), docker-compose's own variable interpolation will try to
# expand $argon2id / $v / $m / ... as if THEY were variables — which is
# exactly the "variable is not set. Defaulting to a blank string" warnings
# and the resulting auth failures. Two ways to avoid that entirely:
#   1. In .env (docker-compose only — never needed on Render), double every
#      literal $ as $$:  ADMIN_PASSWORD_HASH=$$argon2id$$v=19$$m=65536,...
#   2. Or set ADMIN_PASSWORD_HASH_B64 instead (base64 of the raw hash) —
#      no $ characters, so no interpolation problem, on either platform:
#        python -c "import base64; print(base64.b64encode(b'<hash>').decode())"
# Either input is accepted; ADMIN_PASSWORD_HASH takes priority if both are set.
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

# Session token signing secret + lifetime. Idle timeout is intentionally
# short (real-money surface) — every mutating call re-validates the session,
# not just page load (see auth/admin_auth.py).
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
SESSION_IDLE_TIMEOUT_MINUTES = int(os.getenv("SESSION_IDLE_TIMEOUT_MINUTES", "30"))

# ── Dhan credential encryption (Layer 2) ─────────────────────────────────────
# Fernet key encrypting the stored Dhan client-id/access-token at rest.
# Generate once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DHAN_CREDENTIAL_ENC_KEY = os.getenv("DHAN_CREDENTIAL_ENC_KEY", "")

# Decision 4: manual token paste by default. Dhan access tokens are
# generated from the Dhan developer console (web.dhan.co → DhanHQ Trading
# APIs → generate access token) and are valid for DHAN_TOKEN_LIFETIME_DAYS.
# Confirmed directly against this account's own Dhan dashboard: manual
# (non-TOTP) tokens here expire in ~24h — default set to 1 day accordingly.
# Override via env if a different plan/account issues longer-lived tokens.
# With DHAN_TOTP_ENABLED=False, the service surfaces "🔴 Token expired —
# reauthenticate" and auto-disarms rather than silently failing orders
# (see auth/dhan_credentials.py:is_token_valid / gate state machine in
# main.py).
# Flip DHAN_TOTP_ENABLED to true + set DHAN_TOTP_SECRET once TOTP is
# enabled on the Dhan account, and the same background refresh job
# self-heals the token instead.
DHAN_TOKEN_LIFETIME_DAYS = float(os.getenv("DHAN_TOKEN_LIFETIME_DAYS", "1") or 1)
DHAN_TOTP_ENABLED = os.getenv("DHAN_TOTP_ENABLED", "false").lower() == "true"
DHAN_TOTP_SECRET = os.getenv("DHAN_TOTP_SECRET", "")  # only read when the above is true

# §2 proactive TOTP refresh loop (execution/auto_pilot.py:_totp_refresh_loop,
# auth/dhan_credentials.py:token_needs_refresh). Added alongside the
# 2026-09-01 TOTP proactive-refresh fix but missed from config.py at the
# time — caused AttributeError on startup (auto_pilot.start() logs both
# values unconditionally, even with DHAN_TOTP_ENABLED=false). Fixed here.
# How often the background loop wakes to check whether the current Dhan
# token is inside the refresh margin. No-op wake (cheap) while
# DHAN_TOTP_ENABLED=false.
DHAN_TOTP_REFRESH_CHECK_INTERVAL_SECONDS = max(
    60, int(os.getenv("DHAN_TOTP_REFRESH_CHECK_INTERVAL_SECONDS", "900") or 900)
)
# How close to the token's effective expiry (DHAN_HARD_CAP_HOURS-clamped,
# see auth/dhan_credentials.py:_effective_expiry) the loop proactively
# refreshes, instead of waiting for it to actually expire.
DHAN_TOTP_REFRESH_MARGIN_HOURS = float(
    os.getenv("DHAN_TOTP_REFRESH_MARGIN_HOURS", "2") or 2
)

# Dhan sandbox vs live base URL — sandbox is the default until Phase 2's
# validation runs are done; execution/dhan_client.py reads this, never
# hardcodes a host.
DHAN_ENV = os.getenv("DHAN_ENV", "sandbox")  # "sandbox" | "live"
DHAN_BASE_URL = {
    "sandbox": os.getenv("DHAN_SANDBOX_URL", "https://api.dhan.co/v2"),  # Dhan uses one
    "live": os.getenv("DHAN_LIVE_URL", "https://api.dhan.co/v2"),        # base URL for both;
}[DHAN_ENV]                                                              # sandbox = separate app/token

# ── Decision 1: entry style — bounded limit order, time-boxed ──────────────
ENTRY_ORDER_TYPE = "LIMIT"
ENTRY_ZONE_UPPER_PCT = float(os.getenv("ENTRY_ZONE_UPPER_PCT", "0.5"))   # limit at most +0.5% above signal price
ENTRY_VALIDITY_MINUTES = int(os.getenv("ENTRY_VALIDITY_MINUTES", "15"))  # one candle; cancel-and-reassess if unfilled
ENTRY_NO_CHASE = True  # never re-price an unfilled entry upward; re-evaluate next cycle instead

# 2026-09-08 fix (holdings-sync ghost-position reconciliation — see
# portfolio.holdings_sync_reconcile): a REAL position this service booked
# OPEN from an order Dhan's orderbook reported as TRADED/COMPLETE, but that
# never actually shows up in Dhan's own get_positions()/get_holdings()
# snapshot on a later cycle, is treated as a ghost and force-closed rather
# than left open forever pointing at shares this account doesn't actually
# hold (root cause of Stockky showing 19 OPEN positions against only 4 real
# Dhan holdings). This guard window exists purely to avoid a false positive
# on a position that filled only seconds/minutes ago — Dhan's own
# positions/holdings feed isn't guaranteed to reflect a same-cycle fill
# instantly, so a position younger than this is left alone and re-checked
# next cycle instead of being force-closed on its very first pass.
HOLDINGS_SYNC_GUARD_MINUTES = int(os.getenv("HOLDINGS_SYNC_GUARD_MINUTES", "30"))

# ── 2026-09-08 — cycle-level entry quality filter (user-requested calibration
# improvement, SyncContext STOCKKY decision #30) ────────────────────────────
# Gates 1-5 in entry_engine/entry.py (actionable label / live price / regime /
# drift / R:R floor) plus risk_engine's per-trade and portfolio caps are all
# INDEPENDENT per-candidate checks — a mediocre setup that just barely clears
# every individual floor gets entered exactly like a genuinely strong one.
# Watching the live book (2026-09-08: 19 open REAL positions, a large
# majority small losers) showed this concretely: too many marginal setups
# were being let through, diluting capital across weak candidates instead of
# concentrating it in the best few the pipeline found *that cycle*.
#
# This adds one more gate AFTER a candidate has already individually cleared
# gates 1-5 and risk_engine: rank every risk-approved candidate THIS CYCLE
# against each other by a composite quality score (see
# entry_engine.entry._composite_quality_score) and only actually place orders
# for the top ENTRY_MAX_NEW_PER_CYCLE of them, and only if that composite
# score also clears ENTRY_MIN_COMPOSITE_SCORE. Everything that was
# risk-approved but didn't make the cut is left WAIT (not REJECTED — a
# genuinely good setup, just not the best of this cycle's batch) so it's
# still visible next cycle instead of being silently discarded.
ENTRY_CYCLE_QUALITY_FILTER_ENABLED = os.getenv("ENTRY_CYCLE_QUALITY_FILTER_ENABLED", "true").lower() == "true"
ENTRY_MAX_NEW_PER_CYCLE = int(os.getenv("ENTRY_MAX_NEW_PER_CYCLE", "3"))
ENTRY_MIN_COMPOSITE_SCORE = float(os.getenv("ENTRY_MIN_COMPOSITE_SCORE", "50.0"))
# Weights must sum to 1.0 — conviction (the pipeline's own scoring across
# analysis-intelligence/decision-prediction), reward:risk (how much upside
# per unit of downside this specific setup offers), and drift safety (how
# close price still is to the original signal — a candidate that's already
# run far from its signal is chasing, even if it still numerically clears
# the drift gate).
# 2026-09-09 reweight (session20 — see CHANGES_2026-09-09_GATE6_RR_WEIGHT_
# STRUCTURAL_FIX.md): rr_norm was carrying 35% of the composite on the
# assumption R:R varies meaningfully between setups (up toward the 4.0
# ceiling for a great one). It doesn't — _atr_stop_target_pct() derives
# target_pct as stop_pct * (ATR_TARGET_MULTIPLIER/ATR_STOP_MULTIPLIER), a
# FIXED 2.0:1 ratio (3.0/1.5) for every ATR-based candidate and 2.03:1 for
# the flat fallback (6.5/3.2), always — never higher, regardless of setup
# quality. Since MIN_REWARD_RISK_RATIO is also 2.0, rr_norm is ~0 for
# virtually every candidate that ever reaches Gate 6, every cycle, by
# construction — not because the setups are weak. That made the 50-point
# floor arithmetically unreachable for anything below HIGH_CONVICTION even
# after the same-day conviction-score bump above (base tier tops out at
# conviction(60)*0.50 + ~0 + drift(15) = 45, never 50, at ANY drift value —
# confirmed against live session20 data: 30+ risk-approved candidates all
# stuck at composite 40-47.5, zero entries all day). Rebalanced weight off
# the non-discriminating RR term and onto drift-safety, which does vary
# per-candidate and is exactly what Gate 6's own docstring says it should
# reward. RR keeps a small (10%) weight rather than 0 in case a future fix
# to _atr_stop_target_pct (e.g. technical-level-based targets) makes it
# genuinely variable again — no need to touch this file when that happens.
ENTRY_COMPOSITE_WEIGHT_CONVICTION = float(os.getenv("ENTRY_COMPOSITE_WEIGHT_CONVICTION", "0.65"))
ENTRY_COMPOSITE_WEIGHT_RR = float(os.getenv("ENTRY_COMPOSITE_WEIGHT_RR", "0.10"))
ENTRY_COMPOSITE_WEIGHT_DRIFT = float(os.getenv("ENTRY_COMPOSITE_WEIGHT_DRIFT", "0.25"))
# R:R at or above this is scored as "excellent" (100/100 on that sub-score) —
# not a hard ceiling on trades, only on how much extra composite credit an
# already-generous R:R keeps earning past this point.
ENTRY_COMPOSITE_RR_CEILING = float(os.getenv("ENTRY_COMPOSITE_RR_CEILING", "4.0"))

# 2026-09-09 fix (session20 — see candidate_engine/candidates.py's
# _recently_candidated_symbols docstring for the full incident): a candidate
# WAIT'd ONLY by Gate 6's cycle-level concentration filter (risk-approved,
# just not in this cycle's top ENTRY_MAX_NEW_PER_CYCLE, or below the
# composite floor) is marked consumed immediately, same as an ENTER or a
# real gate-1-5/risk_engine WAIT. Without this, that symbol was silently
# excluded from re-candidacy for the full CANDIDATE_DEDUPE_COOLDOWN_HOURS
# (6h) / volume_shock's 2h — contradicting Gate 6's own WAIT message, which
# tells the dashboard it's "re-evaluated fresh next cycle". This is how
# soon (in minutes) such a symbol becomes eligible again — set close to
# AUTO_PILOT_INTERVAL_SECONDS (180s default) with headroom, not to the
# multi-hour dedupe window. Gate 1-5/risk_engine WAITs and real ENTERs are
# unaffected — they keep the full cooldown.
ENTRY_GATE6_REQUEUE_MINUTES = int(os.getenv("ENTRY_GATE6_REQUEUE_MINUTES", "15"))

# 2026-09-10 (session22): candidates re-queued overnight by the EOD signal
# scan (see auto_pilot._eod_signal_scan / _prepick and config's
# EOD_SIGNAL_SCAN_* block below) get a flat bonus added to their raw
# composite score before Gate 6 ranks the cycle. Rationale: these candidates
# already survived a full day's price action (today's move didn't fade into
# the close) AND a second, end-of-day re-scan of the source feeds (catching
# results/board/bulk-block catalysts that landed AFTER the candidate was
# first seen this morning) — that's strictly more confirmation than an
# intraday candidate gets, so it earns a modest ranking boost, not a floor
# bypass like UPPER_CIRCUIT's. Still has to clear every individual gate
# (extension/drift caps, risk_engine, cash) fresh at tomorrow's open — the
# bonus only affects Gate 6's cross-candidate ranking, never gates 1-5.
ENTRY_OVERNIGHT_PRIORITY_BONUS = float(os.getenv("ENTRY_OVERNIGHT_PRIORITY_BONUS", "12.0"))

# ── Decision 2: conservative risk defaults (seed values only — admin can
#    edit via UI while disarmed; risk_engine always reads the live DB row,
#    never these constants directly, once trade_risk_config exists) ────────
DEFAULT_RISK_PER_TRADE_PCT = float(os.getenv("DEFAULT_RISK_PER_TRADE_PCT", "1.0"))
DEFAULT_MAX_DAILY_LOSS_PCT = float(os.getenv("DEFAULT_MAX_DAILY_LOSS_PCT", "3.0"))
DEFAULT_MAX_CONCURRENT_POSITIONS = int(os.getenv("DEFAULT_MAX_CONCURRENT_POSITIONS", "3"))
DEFAULT_MAX_PORTFOLIO_RISK_PCT = float(os.getenv("DEFAULT_MAX_PORTFOLIO_RISK_PCT", "5.0"))
DEFAULT_STALE_DATA_SECONDS = int(os.getenv("DEFAULT_STALE_DATA_SECONDS", "30"))
DEFAULT_MAX_TICK_VOLATILITY_MULT = float(os.getenv("DEFAULT_MAX_TICK_VOLATILITY_MULT", "2.0"))

# ── Decision 3: same Oracle DB, new schema — see oracle_compat.py / db.py.
#    No separate DATABASE_URL default here on purpose: this service must be
#    pointed at the SAME instance as the rest of Stockky via the same
#    ORACLE_DSN / ORACLE_WALLET_DIR / ORACLE_WALLET_PASSWORD env vars, or
#    (on Render/Neon for local dev) the same DATABASE_URL. ─────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Paper-mode default capital (DEMO account seed, admin-editable) ─────────
DEFAULT_DEMO_CAPITAL = float(os.getenv("DEFAULT_DEMO_CAPITAL", "100000"))

# ── Auto-Pilot (2026-08-27) — runs /cycle/run/{mode} on a server-side timer
#    so armed trading keeps working with the dashboard closed. Off by
#    default per mode (see models.TradeGateState.auto_pilot_enabled) —
#    this only controls HOW OFTEN it ticks once an admin turns it on for
#    a given mode; it never arms anything by itself. ──────────────────────
AUTO_PILOT_INTERVAL_SECONDS = max(30, int(os.getenv("AUTO_PILOT_INTERVAL_SECONDS", "180")))
# If true, sends a Telegram message on every tick even when nothing
# happened (useful to confirm the loop is alive); default is quiet —
# only notify when a cycle actually entered/filled/exited something.
AUTO_PILOT_NOTIFY_HEARTBEAT = os.getenv("AUTO_PILOT_NOTIFY_HEARTBEAT", "false").lower() == "true"

# ── Scheduled automation (2026-08-31) — three OPTIONAL, time-of-day features
#    layered on top of auto-pilot. ALL DEFAULT OFF. These are the process-level
#    kill-switches: a feature runs only when BOTH its env flag here is true AND
#    the per-mode UI toggle (models.TradeGateState.*_enabled) is on AND the mode
#    is armed. Leaving these false means the new loop is inert no matter what the
#    dashboard shows — the intended posture until the whole flow is proven in
#    DEMO. Times are IST 'HH:MM'.
#
#      PREPICK        ~09:00 pre-open: refresh candidates + pre-rank so the queue
#                     is warm the moment the market opens (no order placed;
#                     market is shut).
#      ENTER_AT_OPEN  ~09:20 just after open: run one full entry cycle so the
#                     pre-picked names get entered at the early/optimum price.
#      EOD_SQUAREOFF  ~15:00 before close: close all open positions for the mode
#                     so nothing is carried overnight (intraday square-off).
#      EOD_SIGNAL_SCAN ~15:05, right after square-off: a SEPARATE, later
#                     re-scan of the source feeds (catches results/board/
#                     bulk-block catalysts and volume-shock momentum that
#                     only showed up late in the day) — does NOT place any
#                     order today (never worth entering minutes before
#                     close), it queues the strongest names as an
#                     "overnight priority" list for tomorrow's PREPICK/
#                     ENTER_AT_OPEN so they're bought first thing at the
#                     next open instead of waiting to be rediscovered.
# 2026-09-01: the three *_ENABLED env kill-switches (PREPICK_ENABLED,
# ENTER_AT_OPEN_ENABLED, EOD_SQUAREOFF_ENABLED) were removed at the admin's
# request — the per-mode dashboard toggle (TradeGateState.prepick_enabled
# etc.) is now the SOLE on/off authority for these features. Only the
# time-of-day vars remain here.
PREPICK_TIME_IST = os.getenv("PREPICK_TIME_IST", "09:00")
ENTER_AT_OPEN_TIME_IST = os.getenv("ENTER_AT_OPEN_TIME_IST", "09:20")
# 2026-09-10 (session22, user request): moved from 15:15 to 15:00. Two
# reasons: (1) decision #33 (session21e, live Dhan order-book evidence)
# found that SELLs placed close to Dhan's intraday cutoff generated a large
# retry-storm of failed orders (205 failed vs 12 success) — firing
# square-off 15 minutes earlier gives materially more buffer before that
# cutoff for slow fills/partial retries to complete cleanly instead of
# racing the clock. (2) it leaves a clean ~15-25 minute window before the
# 15:30 close for EOD_SIGNAL_SCAN below to run against still-live prices
# without any risk of re-entering something square-off just flattened.
EOD_SQUAREOFF_TIME_IST = os.getenv("EOD_SQUAREOFF_TIME_IST", "15:00")
EOD_SIGNAL_SCAN_TIME_IST = os.getenv("EOD_SIGNAL_SCAN_TIME_IST", "15:05")

# How often the time-trigger loop wakes to check the clock (seconds).
SCHEDULE_CHECK_INTERVAL_SECONDS = max(20, int(os.getenv("SCHEDULE_CHECK_INTERVAL_SECONDS", "60")))

# ── EOD signal scan (2026-09-10, session22 — user request) ─────────────────
# "When market closes for the day, pick some stocks based on end-of-day
# signals (positive news/results/events, momentum into the close) so they
# can be bought first thing next morning instead of waiting to be
# rediscovered" — this is the config for that feature. It is entirely
# additive: it runs candidate_engine.candidates.refresh_candidates() one
# more time near the close (a normal, already-existing scan — nothing new
# is fetched here that today's earlier cycles didn't already know how to
# fetch), then keeps only the top few by conviction as an "overnight
# priority" list consumed by tomorrow's pre-pick. See auto_pilot.py's
# _eod_signal_scan / _prepick and models.py's TradeCandidate.overnight_priority.
EOD_SIGNAL_SCAN_MAX_CANDIDATES = int(os.getenv("EOD_SIGNAL_SCAN_MAX_CANDIDATES", "5"))
# Below this conviction score, a late-day candidate isn't strong enough to
# carry as an overnight priority pick — matches the same conviction scale
# (0-100) the rest of entry_engine/candidate_engine already use.
EOD_SIGNAL_SCAN_MIN_CONVICTION = float(os.getenv("EOD_SIGNAL_SCAN_MIN_CONVICTION", "60.0"))

# 2026-09-11 (session23, user request): "if the system stock looks good it
# can place an order that day too — better than next day's open, which is
# more volatile and we might miss the move." Adds a SECOND, stricter tier
# on top of the queue-only behaviour above: among the picks that already
# cleared EOD_SIGNAL_SCAN_MIN_CONVICTION, the ones at/above this bar are
# handed to the normal entry pipeline (entry_engine.entry.evaluate_mode)
# for evaluation RIGHT NOW at ~15:05, instead of only being snapshotted for
# tomorrow. This does NOT bypass any gate — extension/drift caps,
# risk_engine, regime gate, and cash checks all still apply exactly as they
# do to any other candidate; it only decides which candidates get a same-day
# shot at all. Deliberately set well above EOD_SIGNAL_SCAN_MIN_CONVICTION
# (75 vs 60) because a same-day fill has ~20-25 minutes of live trading left
# and becomes an overnight position once taken (square-off for the day has
# already run) — both real risks that a next-morning entry doesn't carry.
# Anything picked for this tier that doesn't actually fill today (gate
# rejection, disarmed mode, insufficient cash) automatically falls back
# into the normal overnight-priority queue for tomorrow rather than being
# lost — see auto_pilot._eod_signal_scan.
EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION = float(os.getenv("EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION", "75.0"))
EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES = int(os.getenv("EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES", "2"))

# ── US sector overnight signal (2026-09-11, session23 — user request) ──────
# "We can take some idea from the US stock market sector-wise, or on some
# point based on that, predict something early" — used ONLY at PREPICK
# (~09:00 IST), which is safely after the US session has closed for the
# night (NYSE/NASDAQ close ~02:00-02:30 IST during EDT, ~03:00-03:30 IST
# during EST). Fetches a handful of US sector ETFs (see
# market_context/sector_signal.py) via the yfinance dependency this service
# already uses elsewhere (surprise_premarket.py), maps each candidate's
# NSE sector to the closest US sector ETF via a static table (only covers
# the major NSE sectoral-index constituents — an unmapped symbol simply
# gets no bonus, never a penalty for being unmapped), and adds a small,
# CAPPED bonus/penalty to that candidate's ranking score at Gate 6 — same
# "ranking nudge, not a gate bypass" posture as ENTRY_OVERNIGHT_PRIORITY_BONUS
# above. Off by default until proven in DEMO.
US_SECTOR_SIGNAL_ENABLED = os.getenv("US_SECTOR_SIGNAL_ENABLED", "false").lower() == "true"
# Max points added/subtracted at the extreme (a sector ETF at
# +/-US_SECTOR_BONUS_FULL_SCALE_PCT% or beyond gets the full +/-cap;
# scaled linearly in between, capped both ends).
US_SECTOR_BONUS_CAP = float(os.getenv("US_SECTOR_BONUS_CAP", "6.0"))
US_SECTOR_BONUS_FULL_SCALE_PCT = float(os.getenv("US_SECTOR_BONUS_FULL_SCALE_PCT", "1.5"))

# ── Telegram — direct bot notifications for fills/exits/auto-pilot ticks.
#    Separate from notification-scheduler-service's own Telegram config on
#    purpose: that service notifies about SCAN opportunities (candidates
#    found), this one notifies about actual REAL-money order/position
#    events, so a token/chat can be shared or split independently.
#    Create a bot via @BotFather, then message it once and open
#    https://api.telegram.org/bot<token>/getUpdates to read your chat_id.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


def startup_config_errors() -> list[str]:
    """Real, blocking config problems — checked once at boot (main.py) so a
    misconfigured deploy fails loudly instead of booting into a state that
    LOOKS armed-capable but can't actually protect anything."""
    errors = []
    if not ADMIN_PASSWORD_HASH:
        errors.append("ADMIN_PASSWORD_HASH is not set — service cannot authenticate an admin.")
    if not SESSION_SECRET:
        errors.append("SESSION_SECRET is not set — admin sessions cannot be signed.")
    if not DHAN_CREDENTIAL_ENC_KEY:
        errors.append("DHAN_CREDENTIAL_ENC_KEY is not set — Dhan credentials cannot be stored safely.")
    if DHAN_TOTP_ENABLED and not DHAN_TOTP_SECRET:
        errors.append("DHAN_TOTP_ENABLED=true but DHAN_TOTP_SECRET is not set.")
    return errors

# ── Trading thresholds — two categories (STRUCTURAL and REGIME-DEPENDENT) ────
#
# STRUCTURAL (permanent hygiene — never change based on market conditions):
#   These are mathematical safety caps, not market-regime judgments. They
#   should stay constant regardless of bull/bear market.
#
RISK_MAX_POSITION_CONCENTRATION_PCT = float(os.getenv("RISK_MAX_POSITION_CONCENTRATION_PCT", "25.0"))
RISK_MIN_STOCK_PRICE                = float(os.getenv("RISK_MIN_STOCK_PRICE", "20.0"))
CANDIDATE_MIN_STOCK_PRICE           = float(os.getenv("CANDIDATE_MIN_STOCK_PRICE", "20.0"))
# How long a symbol that already has a candidate row (this mode, any source
# track) is skipped from being re-fetched/re-inserted. Without this, a
# symbol that keeps qualifying every cycle (e.g. still sitting in the
# volume-shock universe) got a brand-new TradeCandidate row every cycle,
# which is what produced repeated duplicate cards on the Watchlist tab.
CANDIDATE_DEDUPE_COOLDOWN_HOURS     = float(os.getenv("CANDIDATE_DEDUPE_COOLDOWN_HOURS", "6.0"))
CANDIDATE_MAX_ATR_PCT               = float(os.getenv("CANDIDATE_MAX_ATR_PCT", "7.0"))
CANDIDATE_VOLUME_HEALTH_RATIO       = float(os.getenv("CANDIDATE_VOLUME_HEALTH_RATIO", "0.80"))
CANDIDATE_BULLISH_THRESHOLD_PCT     = float(os.getenv("CANDIDATE_BULLISH_THRESHOLD_PCT", "0.5"))
ENTRY_MAX_DRIFT_ATR                 = float(os.getenv("ENTRY_MAX_DRIFT_ATR", "0.75"))
ENTRY_CONVICTION_MIDPOINT           = float(os.getenv("ENTRY_CONVICTION_MIDPOINT", "65.0"))
ENTRY_CONVICTION_MAX_SCALE          = float(os.getenv("ENTRY_CONVICTION_MAX_SCALE", "0.25"))
EXIT_BREAKEVEN_ATR_TRIGGER          = float(os.getenv("EXIT_BREAKEVEN_ATR_TRIGGER", "1.0"))
EXIT_EMERGENCY_LOSS_MULT            = float(os.getenv("EXIT_EMERGENCY_LOSS_MULT", "1.5"))
EXIT_PARTIAL_FRACTION               = float(os.getenv("EXIT_PARTIAL_FRACTION", "0.60"))
EXIT_MAX_HOLD_DAYS                  = int(os.getenv("EXIT_MAX_HOLD_DAYS", "10"))
EXIT_EARLY_WARN_DAYS                = int(os.getenv("EXIT_EARLY_WARN_DAYS", "6"))

#
# REGIME-DEPENDENT (review monthly or on material market-regime shift):
#   These are tactical thresholds tied to current market conditions. They were
#   set on 2026-08-28 based on Nifty correction (−7% in 6m), FII net-short.
#   adaptive_thresholds.py auto-adjusts ENTRY_REGIME_MIN_SCORE from DB history;
#   the others below are static-with-staleness-warning until the next review.
#
#   LAST REVIEWED: 2026-08-28 (Nifty 24,090, FII net-short, Midcap outperforming)
#   Next review: trigger on Nifty crossing 25,500 OR monthly on the 1st.
#
ENTRY_REGIME_MIN_SCORE              = int(os.getenv("ENTRY_REGIME_MIN_SCORE", "25"))   # ADAPTIVE: auto-computed from history. 2026-09-03: lowered 38→25. Rationale: Nifty flat (+0.05%) but individual stocks making 7-17% moves (Hikal, Raymond, GOCL etc). Broad market regime score should not block individual volume-shock movers. REGIME_OVERRIDE still lets top-1 high-conviction candidate through even below this gate.
# 2026-08-31: the adaptive regime gate (see adaptive_thresholds.py) is a
# TRAILING 90-day p20 — after a sharp, recent regime break it can sit far
# above today's actual score for weeks (e.g. gate=65 vs today's score=19),
# during which the fully-strict gate blocks every single REAL entry with no
# way for even the strongest setup to get through. REGIME_OVERRIDE_TOP_N lets
# the N highest-conviction candidates per cycle bypass ONLY this gate — they
# still have to clear every other gate (drift, R:R floor, risk sizing, risk
# engine) unchanged, and get sized at REGIME_OVERRIDE_RISK_SCALE of normal
# risk as an extra margin for trading against a still-weak market read. Set
# to 0 to fully restore the old "regime weak = nothing enters" behavior.
ENTRY_REGIME_OVERRIDE_TOP_N         = int(os.getenv("ENTRY_REGIME_OVERRIDE_TOP_N", "1"))
ENTRY_REGIME_OVERRIDE_RISK_SCALE    = float(os.getenv("ENTRY_REGIME_OVERRIDE_RISK_SCALE", "0.5"))
ENTRY_MIN_REWARD_RISK               = float(os.getenv("ENTRY_MIN_REWARD_RISK", "2.0"))  # LAST_REVIEWED: 2026-09-03
CANDIDATE_MIN_CONVICTION            = float(os.getenv("CANDIDATE_MIN_CONVICTION", "55")) # LAST_REVIEWED: 2026-09-03
CANDIDATE_MIN_BULLISH_TF            = int(os.getenv("CANDIDATE_MIN_BULLISH_TF", "4"))   # LAST_REVIEWED: 2026-09-03
CANDIDATE_DOWNTREND_6M_PCT          = float(os.getenv("CANDIDATE_DOWNTREND_6M_PCT", "-10.0")) # LAST_REVIEWED: 2026-09-03
CANDIDATE_OVEREXTENDED_52W_TOP_PCT  = float(os.getenv("CANDIDATE_OVEREXTENDED_52W_TOP_PCT", "12.0")) # LAST_REVIEWED: 2026-09-03

# ── Volume-shock quality gate (2026-09-11 fix) ─────────────────────────────
# User-reported bug: the volume-shock track (candidate_engine._refresh_
# volume_shock_candidates) added EVERY symbol that cleared the pure price/
# volume breakout check (_volume_shock_analysis) straight onto the
# watchlist — no fundamental or technical quality check at all, so a stock
# with terrible fundamentals or a broken technical picture got the same
# watchlist slot as a genuinely tradeable one, and the watchlist ended up
# both noisy and full of low-quality names.
#
# See candidate_engine/candidates.py's _quality_gate_fund_tech() for the
# actual gate logic. Two parts:
#   1. Absolute "not bad" floor on fundamental_score/technical_score (each
#      0-100, from analysis-intelligence-service) — deliberately low, this
#      is a "not bad" bar, not a "great stock" bar.
#   2. Sector-relative floor: a stock must not sit at the bottom of ITS
#      OWN sector's candidates this cycle (not the whole market) — see
#      VOLUME_SHOCK_SECTOR_MIN_PEERS/PCTL_FLOOR below. Skipped when a
#      sector doesn't have enough same-cycle peers to make "adaptive"
#      meaningful; the absolute floors still apply either way.
VOLUME_SHOCK_QUALITY_GATE_ENABLED   = os.getenv("VOLUME_SHOCK_QUALITY_GATE_ENABLED", "true").lower() == "true"
VOLUME_SHOCK_FUND_ABS_FLOOR         = float(os.getenv("VOLUME_SHOCK_FUND_ABS_FLOOR", "35"))
VOLUME_SHOCK_TECH_ABS_FLOOR         = float(os.getenv("VOLUME_SHOCK_TECH_ABS_FLOOR", "35"))
VOLUME_SHOCK_SECTOR_MIN_PEERS       = int(os.getenv("VOLUME_SHOCK_SECTOR_MIN_PEERS", "3"))
VOLUME_SHOCK_SECTOR_PCTL_FLOOR      = float(os.getenv("VOLUME_SHOCK_SECTOR_PCTL_FLOOR", "30"))
# Bound on how many symbols get fund/technical HTTP lookups per cycle —
# analysis-intelligence-service calls are the most expensive step in this
# gate; cap so a huge volume-shock universe day can't turn one cycle into
# hundreds of extra outbound calls. Candidates beyond this cap are still
# subject to the underlying price/volume checks, just not the quality gate
# — same "don't silently guess" spirit as the rest of this module, applied
# to cost control rather than a trading decision.
VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS = int(os.getenv("VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS", "40"))

# ── Adaptive threshold engine configuration ───────────────────────────────────
# Controls how adaptive_thresholds.py computes the live regime gate.
ADAPTIVE_HISTORY_DAYS      = int(os.getenv("ADAPTIVE_HISTORY_DAYS", "90"))
ADAPTIVE_MIN_HISTORY_DAYS  = int(os.getenv("ADAPTIVE_MIN_HISTORY_DAYS", "30"))
ADAPTIVE_PERCENTILE        = float(os.getenv("ADAPTIVE_PERCENTILE", "20.0"))
ADAPTIVE_STALE_THRESHOLD_DAYS = int(os.getenv("ADAPTIVE_STALE_THRESHOLD_DAYS", "30"))
