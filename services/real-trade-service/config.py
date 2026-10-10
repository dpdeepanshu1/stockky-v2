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
PORT = int(((os.getenv("PORT") or "").strip() or "8005"))

# ── Upstream Stockky services (recommendations only — this service never
#    writes back into api-gateway's data) ───────────────────────────────────
API_GATEWAY_URL = (os.getenv("API_GATEWAY_URL") or "").strip().rstrip("/") or "https://stockky-api-gateway.onrender.com"

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


# Short-Term Trading Upgrade (2026-09-02): analysis-intelligence-service's
# event sub-service, used only by watchlist_engine/sources.py's Tier 2
# fallback (raw catalyst feed, pre-scoring — see that module's docstring).
# Independent from API_GATEWAY_URL so Tier 2 keeps working via its own
# circuit breaker even when api-gateway (Tier 1) is unhealthy.
EVENT_URL = _env_url("EVENT_URL", "https://stockky-event-tracker.onrender.com")

# 2026-09-11 fix — needed for the volume-shock quality gate (see
# candidate_engine/candidates.py's _quality_gate_fund_tech). Mirrors the
# exact same ANALYSIS_INTELLIGENCE_URL / TECHNICAL_URL / FUNDAMENTAL_URL
# pattern decision-prediction-service and api-gateway already use — this
# service just never defined them because nothing here called into
# analysis-intelligence-service before now. docker-compose.yml sets
# TECHNICAL_URL/FUNDAMENTAL_URL explicitly for the container network; the
# onrender.com defaults below match the Render-hosted deployment already
# used by ANALYSIS_INTELLIGENCE_URL elsewhere in this codebase.
_ANALYSIS_INTELLIGENCE_URL = _env_url("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com")
TECHNICAL_URL = _env_url("TECHNICAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/technical")
FUNDAMENTAL_URL = _env_url("FUNDAMENTAL_URL", f"{_ANALYSIS_INTELLIGENCE_URL}/fundamental")

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
_ADMIN_HASH_B64 = (os.getenv("ADMIN_PASSWORD_HASH_B64") or "").strip()
if _ADMIN_HASH_B64 and not (os.getenv("ADMIN_PASSWORD_HASH") or "").strip():
    try:
        import base64 as _b64
        ADMIN_PASSWORD_HASH = _b64.b64decode(_ADMIN_HASH_B64).decode("utf-8").strip()
    except Exception:
        ADMIN_PASSWORD_HASH = ""
else:
    ADMIN_PASSWORD_HASH = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

# Session token signing secret + lifetime. Idle timeout is intentionally
# short (real-money surface) — every mutating call re-validates the session,
# not just page load (see auth/admin_auth.py).
SESSION_SECRET = (os.getenv("SESSION_SECRET") or "").strip()
SESSION_IDLE_TIMEOUT_MINUTES = int(((os.getenv("SESSION_IDLE_TIMEOUT_MINUTES") or "").strip() or "30"))

# ── Dhan credential encryption (Layer 2) ─────────────────────────────────────
# Fernet key encrypting the stored Dhan client-id/access-token at rest.
# Generate once with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DHAN_CREDENTIAL_ENC_KEY = (os.getenv("DHAN_CREDENTIAL_ENC_KEY") or "").strip()

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
DHAN_TOTP_SECRET = (os.getenv("DHAN_TOTP_SECRET") or "").strip()  # only read when the above is true

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
# validation runs are done. NOTE: nothing imports DHAN_BASE_URL yet (the order path goes through
# the Dhan SDK and execution/dhan_client.py's eDIS calls hardcode https://api.dhan.co/v2), so this
# value is only a resolved, validated setting for now.
_DHAN_DEFAULT_BASE = "https://api.dhan.co/v2"


def _dhan_base_url(name: str) -> str:
    """Dhan REST base from the environment, never blank and never a non-https host.

    os.getenv(name, default) only falls back when the variable is UNSET, so a blank
    DHAN_LIVE_URL (`DHAN_LIVE_URL=` in an env_file) became "" and every order call went to
    "/orders". Blank / whitespace-only / slash-only values use the official base. A value
    that is not https:// is refused (the access token travels on this URL), so it also falls
    back to the official base instead of sending the token over plain http or to a typo host.
    """
    raw = (os.getenv(name) or "").strip().rstrip("/")
    if raw.lower().startswith("https://") and len(raw) > len("https://"):
        return raw
    return _DHAN_DEFAULT_BASE


# A blank DHAN_ENV used to raise KeyError at import; blank now means the safe default (sandbox).
# A non-blank value that is neither "sandbox" nor "live" still fails fast, with a clear message.
DHAN_ENV = (os.getenv("DHAN_ENV") or "").strip().lower() or "sandbox"  # "sandbox" | "live"
if DHAN_ENV not in ("sandbox", "live"):
    raise ValueError(f"DHAN_ENV must be 'sandbox' or 'live', got {DHAN_ENV!r}")
DHAN_BASE_URL = {
    "sandbox": _dhan_base_url("DHAN_SANDBOX_URL"),  # Dhan uses one base URL for both;
    "live": _dhan_base_url("DHAN_LIVE_URL"),        # sandbox = separate app/token
}[DHAN_ENV]

# ── Decision 1: entry style — bounded limit order, time-boxed ──────────────
ENTRY_ORDER_TYPE = "LIMIT"
ENTRY_ZONE_UPPER_PCT = float(((os.getenv("ENTRY_ZONE_UPPER_PCT") or "").strip() or "0.5"))   # limit at most +0.5% above signal price
ENTRY_VALIDITY_MINUTES = int(((os.getenv("ENTRY_VALIDITY_MINUTES") or "").strip() or "15"))  # one candle; cancel-and-reassess if unfilled
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
HOLDINGS_SYNC_GUARD_MINUTES = int(((os.getenv("HOLDINGS_SYNC_GUARD_MINUTES") or "").strip() or "30"))

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
ENTRY_CYCLE_QUALITY_FILTER_ENABLED = ((os.getenv("ENTRY_CYCLE_QUALITY_FILTER_ENABLED") or "").strip() or "true").lower() == "true"
ENTRY_MAX_NEW_PER_CYCLE = int(((os.getenv("ENTRY_MAX_NEW_PER_CYCLE") or "").strip() or "3"))
ENTRY_MIN_COMPOSITE_SCORE = float(((os.getenv("ENTRY_MIN_COMPOSITE_SCORE") or "").strip() or "50.0"))
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
ENTRY_COMPOSITE_WEIGHT_CONVICTION = float(((os.getenv("ENTRY_COMPOSITE_WEIGHT_CONVICTION") or "").strip() or "0.65"))
ENTRY_COMPOSITE_WEIGHT_RR = float(((os.getenv("ENTRY_COMPOSITE_WEIGHT_RR") or "").strip() or "0.10"))
ENTRY_COMPOSITE_WEIGHT_DRIFT = float(((os.getenv("ENTRY_COMPOSITE_WEIGHT_DRIFT") or "").strip() or "0.25"))
# R:R at or above this is scored as "excellent" (100/100 on that sub-score) —
# not a hard ceiling on trades, only on how much extra composite credit an
# already-generous R:R keeps earning past this point.
ENTRY_COMPOSITE_RR_CEILING = float(((os.getenv("ENTRY_COMPOSITE_RR_CEILING") or "").strip() or "4.0"))

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
ENTRY_GATE6_REQUEUE_MINUTES = int(((os.getenv("ENTRY_GATE6_REQUEUE_MINUTES") or "").strip() or "15"))

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
ENTRY_OVERNIGHT_PRIORITY_BONUS = float(((os.getenv("ENTRY_OVERNIGHT_PRIORITY_BONUS") or "").strip() or "12.0"))

# ── Decision 2: conservative risk defaults (seed values only — admin can
#    edit via UI while disarmed; risk_engine always reads the live DB row,
#    never these constants directly, once trade_risk_config exists) ────────
# 2026-09-21 fix (session79 cold-start audit): was 1.0. At this account's
# equity (~₹10,962 after the two other services' capital split), 1% capped
# max_trade_risk at ₹109.62 — below the per-share risk of almost every
# NSE stock above ~₹100, so gate 5 (per_trade_risk_cap) rejected "even 1
# share" on nearly every candidate. Raised to 5.0 (max_trade_risk ≈ ₹548)
# so a normal ATR-based stop distance can actually size a trade. Existing
# DB rows already seeded at the old 1.0 default are corrected on startup —
# see main.py's _migrate_risk_defaults().
DEFAULT_RISK_PER_TRADE_PCT = float(((os.getenv("DEFAULT_RISK_PER_TRADE_PCT") or "").strip() or "5.0"))
DEFAULT_MAX_DAILY_LOSS_PCT = float(((os.getenv("DEFAULT_MAX_DAILY_LOSS_PCT") or "").strip() or "3.0"))
DEFAULT_MAX_CONCURRENT_POSITIONS = int(((os.getenv("DEFAULT_MAX_CONCURRENT_POSITIONS") or "").strip() or "3"))
DEFAULT_MAX_PORTFOLIO_RISK_PCT = float(((os.getenv("DEFAULT_MAX_PORTFOLIO_RISK_PCT") or "").strip() or "5.0"))
DEFAULT_STALE_DATA_SECONDS = int(((os.getenv("DEFAULT_STALE_DATA_SECONDS") or "").strip() or "30"))
DEFAULT_MAX_TICK_VOLATILITY_MULT = float(((os.getenv("DEFAULT_MAX_TICK_VOLATILITY_MULT") or "").strip() or "2.0"))

# ── Decision 3: same Oracle DB, new schema — see oracle_compat.py / db.py.
#    No separate DATABASE_URL default here on purpose: this service must be
#    pointed at the SAME instance as the rest of Stockky via the same
#    ORACLE_DSN / ORACLE_WALLET_DIR / ORACLE_WALLET_PASSWORD env vars, or
#    (on Render/Neon for local dev) the same DATABASE_URL. ─────────────────
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

# ── Paper-mode default capital (DEMO account seed, admin-editable) ─────────
DEFAULT_DEMO_CAPITAL = float(((os.getenv("DEFAULT_DEMO_CAPITAL") or "").strip() or "100000"))

# ── Auto-Pilot (2026-08-27) — runs /cycle/run/{mode} on a server-side timer
#    so armed trading keeps working with the dashboard closed. Off by
#    default per mode (see models.TradeGateState.auto_pilot_enabled) —
#    this only controls HOW OFTEN it ticks once an admin turns it on for
#    a given mode; it never arms anything by itself. ──────────────────────
AUTO_PILOT_INTERVAL_SECONDS = max(30, int(((os.getenv("AUTO_PILOT_INTERVAL_SECONDS") or "").strip() or "180")))
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

# ── Opening entry guard (2026-10-07, group 220) ───────────────────────────────
# Review item 6. In the September trade file the five entries opened between 09:19 and 09:24 IST (3 trading days) all
# lost (-Rs 274 together) while the other 17 made +Rs 301, nearly all of it one trade. Tiny and old, so a hypothesis, but
# the first minutes after the 09:15 open are conventionally the most volatile (auction-driven prices, wide spreads) and
# nothing in this service stopped an automatic entry then: two of those five fired at 09:19, BEFORE ENTER_AT_OPEN's
# 09:20, so the regular auto-pilot cycle enters at the open too. entry_engine/opening_guard.py makes evaluate_mode()
# leave candidates queued (not consumed, not rejected) until OPENING_ENTRY_NOT_BEFORE_IST while the market is open; they
# are evaluated normally, with live ticks, once the guard lifts. The ENTER_AT_OPEN schedule moves to the guard time when
# the guard is later than ENTER_AT_OPEN_TIME_IST (otherwise its once-a-day run would fire into the guard and be wasted).
#   OPENING_ENTRY_GUARD_ENABLED   false turns the guard off
#   OPENING_ENTRY_NOT_BEFORE_IST  'HH:MM' IST, default 09:15 since group 268 (was 09:30; blank/bad value falls back to 09:15)
#   OPENING_ENTRY_GUARD_MODES     comma list of modes it applies to, default REAL,DEMO (set REAL to keep DEMO as a control)
OPENING_ENTRY_GUARD_ENABLED = ((os.getenv("OPENING_ENTRY_GUARD_ENABLED") or "").strip() or "true").lower() == "true"
OPENING_ENTRY_NOT_BEFORE_IST = (os.getenv("OPENING_ENTRY_NOT_BEFORE_IST") or "").strip() or "09:15"
OPENING_ENTRY_GUARD_MODES = tuple(
    m.strip().upper()
    for m in ((os.getenv("OPENING_ENTRY_GUARD_MODES") or "").strip() or "REAL,DEMO").split(",")
    if m.strip()
)

# ── Opening-quality gate (group 268, 2026-10-09) ─────────────────────────────
# The entry window now opens at 09:15 (OPENING_ENTRY_NOT_BEFORE_IST above). The scalp service's 14-day breakdown showed
# entries before 10:30 won 1 of 10, so from the open until OPENING_GATE_SETTLE_IST every automatic entry must pass
# entry_engine/opening_gate.py: at least OPENING_GATE_MIN_MINUTES_AFTER_OPEN minutes after the open, price vs the previous
# close inside [OPENING_GATE_MIN_CHANGE_PCT, OPENING_GATE_MAX_CHANGE_PCT], and not in the top of the exchange day range
# (OPENING_GATE_MAX_RANGE_POS). The tick carries no day-open price, so "change vs previous close" is the gap proxy here.
# Candidates that fail stay QUEUED (not consumed, not rejected) and are re-checked next cycle; after the settle time the
# normal rules apply unchanged. Missing previous close / day range before the settle time FAILS CLOSED (stays queued).
# Applies to the modes in OPENING_ENTRY_GUARD_MODES. OPENING_GATE_ENABLED=false turns it off.
def _og_float(name, default):
    try:
        return float(((os.getenv(name) or "").strip() or str(default)))
    except (TypeError, ValueError):
        return default


OPENING_GATE_ENABLED = ((os.getenv("OPENING_GATE_ENABLED") or "").strip() or "true").lower() == "true"
OPENING_GATE_SETTLE_IST = (os.getenv("OPENING_GATE_SETTLE_IST") or "").strip() or "10:00"
OPENING_GATE_MIN_MINUTES_AFTER_OPEN = _og_float("OPENING_GATE_MIN_MINUTES_AFTER_OPEN", 5.0)
OPENING_GATE_MIN_CHANGE_PCT = _og_float("OPENING_GATE_MIN_CHANGE_PCT", -0.5)   # price vs previous close, lower bound
OPENING_GATE_MAX_CHANGE_PCT = _og_float("OPENING_GATE_MAX_CHANGE_PCT", 3.0)    # upper bound; chasing a bigger move at the open is skipped
OPENING_GATE_MAX_RANGE_POS = _og_float("OPENING_GATE_MAX_RANGE_POS", 0.88)     # 0 disables
# group 269: shadow mode. true -> inside the gate window no candidate is entered; one that passes the gate is logged once per
# symbol per day as "OPENING_SHADOW would enter" and stays queued. Default false = the gate is live (group 268).
OPENING_GATE_SHADOW = ((os.getenv("OPENING_GATE_SHADOW") or "").strip() or "false").lower() == "true"
# group 270: previous-day candle checks (entry_engine/prev_day.py: background fetch of market-data /history, never on the
# entry path; a cache miss holds the candidate back for that cycle). Previous day must have closed in the upper half of its
# range; the stop must be at least FRAC x the daily ATR % (0 disables either check).
OPENING_GATE_PREVDAY_TIMEOUT_S = _og_float("OPENING_GATE_PREVDAY_TIMEOUT_S", 8.0)
OPENING_GATE_MIN_PREVDAY_CLOSE_POS = _og_float("OPENING_GATE_MIN_PREVDAY_CLOSE_POS", 0.5)
OPENING_GATE_MIN_STOP_ATR_FRAC = _og_float("OPENING_GATE_MIN_STOP_ATR_FRAC", 0.3)
OPENING_GATE_PREVDAY_MAX_AGE_DAYS = int(_og_float("OPENING_GATE_PREVDAY_MAX_AGE_DAYS", 6.0))   # group 273: a last daily candle older than this is treated as no data (stale history); 6 covers a long weekend + holiday
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
# group292: once per IST trading day, from this time on, store today's per-trade expectancy report (trade_records.py) as a
# snapshot and push a short summary to Telegram. After the EOD square-off and the 15:30 close; sells that fill later still
# have their charges estimated until the ledger books them. DAILY_REPORT_ENABLED=0 turns the step off.
DAILY_REPORT_ENABLED = (os.getenv("DAILY_REPORT_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")
DAILY_REPORT_TIME_IST = os.getenv("DAILY_REPORT_TIME_IST", "15:45")

# How often the time-trigger loop wakes to check the clock (seconds).
SCHEDULE_CHECK_INTERVAL_SECONDS = max(20, int(((os.getenv("SCHEDULE_CHECK_INTERVAL_SECONDS") or "").strip() or "60")))

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
EOD_SIGNAL_SCAN_MAX_CANDIDATES = int(((os.getenv("EOD_SIGNAL_SCAN_MAX_CANDIDATES") or "").strip() or "5"))
# Below this conviction score, a late-day candidate isn't strong enough to
# carry as an overnight priority pick — matches the same conviction scale
# (0-100) the rest of entry_engine/candidate_engine already use.
EOD_SIGNAL_SCAN_MIN_CONVICTION = float(((os.getenv("EOD_SIGNAL_SCAN_MIN_CONVICTION") or "").strip() or "60.0"))

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
EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION = float(((os.getenv("EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION") or "").strip() or "75.0"))
EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES = int(((os.getenv("EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES") or "").strip() or "2"))

# ── After-hours news scan (2026-09-17, session56 — user request) ────────────
# After market close, poll Moneycontrol/LiveMint/ET RSS feeds hourly, classify
# headlines by catalyst type, score and deduplicate by symbol, and persist into
# NextDayWatchlistEntry for the next trading day's _prepick to consume as
# high-priority candidates. All features controlled by the single DB toggle
# gate.afterhours_news_scan_enabled (DEFAULT OFF — same pattern as every other
# scheduled feature). No order placement — only pre-seeds the candidate queue.
#
# Active window: AFTERHOURS_SCAN_START_IST (default 15:45) to
#   AFTERHOURS_SCAN_END_IST (default 08:45 next day). The loop fires every 6 h off-market
#   and every 30 min 08:00-09:00 IST (see the 2026-10-04 block below). A final
#   "finalize" pass runs at AFTERHOURS_FINALIZE_TIME_IST (default 08:45) to
#   trim to the top-N shortlist before the open.
AFTERHOURS_SCAN_START_IST     = os.getenv("AFTERHOURS_SCAN_START_IST", "15:45")
AFTERHOURS_SCAN_END_IST       = os.getenv("AFTERHOURS_SCAN_END_IST", "08:45")
AFTERHOURS_FINALIZE_TIME_IST  = os.getenv("AFTERHOURS_FINALIZE_TIME_IST", "08:45")
AFTERHOURS_SCAN_INTERVAL_SECONDS = max(
    300, int(((os.getenv("AFTERHOURS_SCAN_INTERVAL_SECONDS") or "").strip() or "3600"))
)  # floor 5 min — RSS feeds are NOT rate-limited like quote APIs, but no point scanning faster than 5 min
# 2026-10-04 (user request): TIME-OF-DAY scan cadence instead of one fixed interval.
#   * off-market hours (15:45 -> 08:00 IST): one scan every AFTERHOURS_SCAN_OFFHOURS_INTERVAL_SECONDS
#     (default 21600 = 6 h) - RSS news barely moves overnight, no point polling every 30 min
#   * pre-open ramp (08:00 -> 09:00 IST): one scan every AFTERHOURS_SCAN_RAMP_INTERVAL_SECONDS
#     (default 1800 = 30 min) so the shortlist is fresh when the 08:45 finalize pass runs
# AFTERHOURS_SCAN_INTERVAL_SECONDS is kept (status endpoint / old env files) but the loop no longer sleeps on it.
AFTERHOURS_SCAN_RAMP_START_IST = os.getenv("AFTERHOURS_SCAN_RAMP_START_IST", "08:00")
AFTERHOURS_SCAN_RAMP_END_IST   = os.getenv("AFTERHOURS_SCAN_RAMP_END_IST", "09:00")
AFTERHOURS_SCAN_RAMP_INTERVAL_SECONDS = max(
    300, int(((os.getenv("AFTERHOURS_SCAN_RAMP_INTERVAL_SECONDS") or "").strip() or "1800"))
)
# 2026-10-08 (group 238): an RSS feed whose every URL answered with a bot-gate status (HTTP 403/429/...)
# is skipped for this long (floor 60 s) instead of being retried on every scan / 15-min intraday tick.
AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS = max(
    60, int(((os.getenv("AFTERHOURS_RSS_BLOCKED_COOLDOWN_SECONDS") or "").strip() or "1800"))
)
AFTERHOURS_SCAN_OFFHOURS_INTERVAL_SECONDS = max(
    300, int(((os.getenv("AFTERHOURS_SCAN_OFFHOURS_INTERVAL_SECONDS") or "").strip() or "21600"))
)
# 2026-10-04 (user request): LIGHTWEIGHT MARKET-HOURS NEWS CHECK. The after-hours loop sleeps through
# 09:00-15:45; news still breaks during the session, so a second, much lighter loop polls the same RSS
# feeds every INTRADAY_NEWS_INTERVAL_SECONDS (default 900 = 15 min) inside INTRADAY_NEWS_START_IST ..
# INTRADAY_NEWS_END_IST (default 09:00-15:45 IST, trading days only). It only looks at NEW headlines
# published in the last INTRADAY_NEWS_MAX_AGE_MINUTES, never calls the quote APIs, never places or
# injects anything - it stores a small CAPPED score nudge per symbol that Gate 6 ranking adds to a
# candidate that is ALREADY in the queue (+ for fresh positive news, - for fresh negative news).
# Runs only while gate.afterhours_news_scan_enabled is on for at least one mode (same single toggle).
INTRADAY_NEWS_START_IST = os.getenv("INTRADAY_NEWS_START_IST", "09:00")
INTRADAY_NEWS_END_IST   = os.getenv("INTRADAY_NEWS_END_IST", "15:45")
INTRADAY_NEWS_INTERVAL_SECONDS = max(
    300, int(((os.getenv("INTRADAY_NEWS_INTERVAL_SECONDS") or "").strip() or "900"))
)  # floor 5 min - RSS is cheap, but there is no value polling faster than feeds publish
INTRADAY_NEWS_MAX_AGE_MINUTES = max(
    15, int(((os.getenv("INTRADAY_NEWS_MAX_AGE_MINUTES") or "").strip() or "180"))
)  # a headline older than this is ignored and an old nudge expires after it
INTRADAY_NEWS_MIN_SCORE = float(
    ((os.getenv("INTRADAY_NEWS_MIN_SCORE") or "").strip() or "20.0")
)  # positive headlines scoring below this (see afterhours_scan._score_headline) give no nudge
INTRADAY_NEWS_BONUS_CAP = float(
    ((os.getenv("INTRADAY_NEWS_BONUS_CAP") or "").strip() or "8.0")
)  # max points ADDED to a candidate's Gate 6 ranking score for fresh positive news
INTRADAY_NEWS_PENALTY_CAP = float(
    ((os.getenv("INTRADAY_NEWS_PENALTY_CAP") or "").strip() or "10.0")
)  # points SUBTRACTED for fresh negative news (probe, downgrade, fraud...) - can push a marginal candidate under the Gate 6 floor
INTRADAY_NEWS_ALERT_MIN_SCORE = float(
    ((os.getenv("INTRADAY_NEWS_ALERT_MIN_SCORE") or "").strip() or "45.0")
)  # Telegram alert for strong positive hits at/above this score (set 101 to turn positive-only alerts off); any hit on a queued candidate always alerts

AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES = int(
    ((os.getenv("AFTERHOURS_SCAN_MAX_NEXTDAY_CANDIDATES") or "").strip() or "8")
)  # top-N kept after finalize pass; rest are marked consumed (discarded)
# Minimum priority_score a NextDayWatchlistEntry must have to be injected as
# a TradeCandidate by _prepick. Set conservatively — the score formula tops
# at 100; 30 means at least a weak-news hit on a decent source, 50 means a
# real catalyst (results/bulk/insider) on a trusted feed.
AFTERHOURS_SCAN_MIN_INJECT_SCORE = float(
    ((os.getenv("AFTERHOURS_SCAN_MIN_INJECT_SCORE") or "").strip() or "30.0")
)
# 2026-09-17 fix (session58, user request): both the RSS headlines and the
# bulk/block-deal hits pulled into the after-hours scan need to actually be
# RECENT news — an RSS feed occasionally re-serves an older item near the
# top (feed re-publish, cache hiccup), and api-gateway's /stockky-hot bulk-
# deal bucket carries whatever the most recent underlying deal/insider
# filing was, which isn't always today's. Without an age check, a stale
# item could get pre-seeded into tomorrow's watchlist as if it were fresh
# after-hours news. Bounded to 3–7 days per user instruction; default 5
# (mid-range) balances "not so tight it drops a Friday-evening story
# scanned before a long weekend" against "not so loose it lets week-old
# news back in".
AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS = min(7, max(3, int(
    ((os.getenv("AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS") or "").strip() or "5")
)))

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
US_SECTOR_BONUS_CAP = float(((os.getenv("US_SECTOR_BONUS_CAP") or "").strip() or "6.0"))
US_SECTOR_BONUS_FULL_SCALE_PCT = float(((os.getenv("US_SECTOR_BONUS_FULL_SCALE_PCT") or "").strip() or "1.5"))

# ── Telegram — direct bot notifications for fills/exits/auto-pilot ticks.
#    Separate from notification-scheduler-service's own Telegram config on
#    purpose: that service notifies about SCAN opportunities (candidates
#    found), this one notifies about actual REAL-money order/position
#    events, so a token/chat can be shared or split independently.
#    Create a bot via @BotFather, then message it once and open
#    https://api.telegram.org/bot<token>/getUpdates to read your chat_id.
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()


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
RISK_MAX_POSITION_CONCENTRATION_PCT = float(((os.getenv("RISK_MAX_POSITION_CONCENTRATION_PCT") or "").strip() or "25.0"))
RISK_MIN_STOCK_PRICE                = float(((os.getenv("RISK_MIN_STOCK_PRICE") or "").strip() or "20.0"))
CANDIDATE_MIN_STOCK_PRICE           = float(((os.getenv("CANDIDATE_MIN_STOCK_PRICE") or "").strip() or "20.0"))
# How long a symbol that already has a candidate row (this mode, any source
# track) is skipped from being re-fetched/re-inserted. Without this, a
# symbol that keeps qualifying every cycle (e.g. still sitting in the
# volume-shock universe) got a brand-new TradeCandidate row every cycle,
# which is what produced repeated duplicate cards on the Watchlist tab.
CANDIDATE_DEDUPE_COOLDOWN_HOURS     = float(((os.getenv("CANDIDATE_DEDUPE_COOLDOWN_HOURS") or "").strip() or "6.0"))
CANDIDATE_MAX_ATR_PCT               = float(((os.getenv("CANDIDATE_MAX_ATR_PCT") or "").strip() or "7.0"))
CANDIDATE_VOLUME_HEALTH_RATIO       = float(((os.getenv("CANDIDATE_VOLUME_HEALTH_RATIO") or "").strip() or "0.80"))
CANDIDATE_BULLISH_THRESHOLD_PCT     = float(((os.getenv("CANDIDATE_BULLISH_THRESHOLD_PCT") or "").strip() or "0.5"))
ENTRY_MAX_DRIFT_ATR                 = float(((os.getenv("ENTRY_MAX_DRIFT_ATR") or "").strip() or "0.75"))
ENTRY_CONVICTION_MIDPOINT           = float(((os.getenv("ENTRY_CONVICTION_MIDPOINT") or "").strip() or "65.0"))
ENTRY_CONVICTION_MAX_SCALE          = float(((os.getenv("ENTRY_CONVICTION_MAX_SCALE") or "").strip() or "0.25"))
EXIT_BREAKEVEN_ATR_TRIGGER          = float(((os.getenv("EXIT_BREAKEVEN_ATR_TRIGGER") or "").strip() or "1.0"))
EXIT_EMERGENCY_LOSS_MULT            = float(((os.getenv("EXIT_EMERGENCY_LOSS_MULT") or "").strip() or "1.5"))
EXIT_PARTIAL_FRACTION               = float(((os.getenv("EXIT_PARTIAL_FRACTION") or "").strip() or "0.60"))
EXIT_MAX_HOLD_DAYS                  = int(((os.getenv("EXIT_MAX_HOLD_DAYS") or "").strip() or "10"))
EXIT_EARLY_WARN_DAYS                = int(((os.getenv("EXIT_EARLY_WARN_DAYS") or "").strip() or "6"))

# 2026-09-15 fix (session40 — DATAMATICS position 81, 89 consecutive
# REJECTED zero-fill exit-SELL attempts over ~4.5h, see
# models.py TradePosition.consecutive_exit_failures docstring for the full
# incident). A SELL that dies with zero fill at the broker used to be
# retried again on the very next cycle, forever, with no backoff and no
# alert — indistinguishable in the logs from a healthy retry loop even
# after dozens of identical rejections. These three settings back that
# off: cooldown doubles (capped at the max) with each consecutive
# rejection for the SAME position, and an operator alert fires once the
# streak crosses the threshold (and again every additional multiple of it,
# so a very long stuck streak doesn't go silent after the first alert).
EXIT_RETRY_BASE_COOLDOWN_SECONDS    = int(((os.getenv("EXIT_RETRY_BASE_COOLDOWN_SECONDS") or "").strip() or "60"))
EXIT_RETRY_MAX_COOLDOWN_SECONDS     = int(((os.getenv("EXIT_RETRY_MAX_COOLDOWN_SECONDS") or "").strip() or "900"))
EXIT_RETRY_ALERT_THRESHOLD          = int(((os.getenv("EXIT_RETRY_ALERT_THRESHOLD") or "").strip() or "5"))

#
# REGIME-DEPENDENT (review monthly or on material market-regime shift):
#   These are tactical thresholds tied to current market conditions. They were
#   set on 2026-08-28 based on Nifty correction (−7% in 6m), FII net-short.
#   adaptive_thresholds.py auto-adjusts ENTRY_REGIME_MIN_SCORE from DB history;
#   the others below are static-with-staleness-warning until the next review.
#
#   LAST REVIEWED: 2026-08-28 (Nifty 24,090, FII net-short, Midcap outperforming)
#   RE-REVIEWED 2026-10-04: owner kept all six values unchanged (dates below moved to match).
#   Next review: trigger on Nifty crossing 25,500 OR monthly on the 1st.
#
ENTRY_REGIME_MIN_SCORE              = int(((os.getenv("ENTRY_REGIME_MIN_SCORE") or "").strip() or "25"))   # ADAPTIVE: auto-computed from history. 2026-09-03: lowered 38→25. Rationale: Nifty flat (+0.05%) but individual stocks making 7-17% moves (Hikal, Raymond, GOCL etc). Broad market regime score should not block individual volume-shock movers. REGIME_OVERRIDE still lets top-1 high-conviction candidate through even below this gate.
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
ENTRY_REGIME_OVERRIDE_TOP_N         = int(((os.getenv("ENTRY_REGIME_OVERRIDE_TOP_N") or "").strip() or "1"))
ENTRY_REGIME_OVERRIDE_RISK_SCALE    = float(((os.getenv("ENTRY_REGIME_OVERRIDE_RISK_SCALE") or "").strip() or "0.5"))
ENTRY_MIN_REWARD_RISK               = float(((os.getenv("ENTRY_MIN_REWARD_RISK") or "").strip() or "2.0"))  # LAST_REVIEWED: 2026-10-04
CANDIDATE_MIN_CONVICTION            = float(((os.getenv("CANDIDATE_MIN_CONVICTION") or "").strip() or "55")) # LAST_REVIEWED: 2026-10-04
CANDIDATE_MIN_BULLISH_TF            = int(((os.getenv("CANDIDATE_MIN_BULLISH_TF") or "").strip() or "4"))   # LAST_REVIEWED: 2026-10-04
CANDIDATE_DOWNTREND_6M_PCT          = float(((os.getenv("CANDIDATE_DOWNTREND_6M_PCT") or "").strip() or "-10.0")) # LAST_REVIEWED: 2026-10-04
CANDIDATE_OVEREXTENDED_52W_TOP_PCT  = float(((os.getenv("CANDIDATE_OVEREXTENDED_52W_TOP_PCT") or "").strip() or "12.0")) # LAST_REVIEWED: 2026-10-04

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
VOLUME_SHOCK_QUALITY_GATE_ENABLED   = ((os.getenv("VOLUME_SHOCK_QUALITY_GATE_ENABLED") or "").strip() or "true").lower() == "true"
VOLUME_SHOCK_FUND_ABS_FLOOR         = float(((os.getenv("VOLUME_SHOCK_FUND_ABS_FLOOR") or "").strip() or "35"))
VOLUME_SHOCK_TECH_ABS_FLOOR         = float(((os.getenv("VOLUME_SHOCK_TECH_ABS_FLOOR") or "").strip() or "35"))
VOLUME_SHOCK_SECTOR_MIN_PEERS       = int(((os.getenv("VOLUME_SHOCK_SECTOR_MIN_PEERS") or "").strip() or "3"))
VOLUME_SHOCK_SECTOR_PCTL_FLOOR      = float(((os.getenv("VOLUME_SHOCK_SECTOR_PCTL_FLOOR") or "").strip() or "30"))
# 2026-09-18 audit fix #3: the sector-relative check above compares a
# candidate only against THIS CYCLE's other same-sector candidates — often
# just 1-3 names — so the same stock can pass or fail purely because of
# which other names happened to be candidates that specific cycle, not
# because anything about the stock itself changed. This does NOT replace
# the cycle-scoped comparison (a historical-peer version was deliberately
# rejected before as too strict for this "not bad" gate — see
# _quality_gate_fund_tech's docstring); it widens the sample by also
# remembering recent cycles' peer quality scores per sector (in-memory,
# process-local — see candidate_engine/candidates.py's
# _sector_peer_history) and merging them in alongside this cycle's peers
# before the percentile check, so the comparison window is less sensitive
# to which handful of names happened to show up this one cycle.
VOLUME_SHOCK_SECTOR_HISTORY_MAX_AGE_MINUTES = int(((os.getenv("VOLUME_SHOCK_SECTOR_HISTORY_MAX_AGE_MINUTES") or "").strip() or "180"))
VOLUME_SHOCK_SECTOR_HISTORY_MAX_SAMPLES     = int(((os.getenv("VOLUME_SHOCK_SECTOR_HISTORY_MAX_SAMPLES") or "").strip() or "40"))
# Bound on how many symbols get fund/technical HTTP lookups per cycle —
# analysis-intelligence-service calls are the most expensive step in this
# gate; cap so a huge volume-shock universe day can't turn one cycle into
# hundreds of extra outbound calls. Candidates beyond this cap are still
# subject to the underlying price/volume checks, just not the quality gate
# — same "don't silently guess" spirit as the rest of this module, applied
# to cost control rather than a trading decision.
# group 214 (A6): VOLUME_SHOCK_QUALITY_FAIL_CLOSED (read in candidate_engine/candidates.py, default on; 0/false/no/off = old
# behaviour). A candidate inside the scored batch whose fundamental AND technical lookups both failed is skipped this
# cycle instead of passing unchecked. Candidates beyond the cap above stay ungated, as documented.
VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS = int(((os.getenv("VOLUME_SHOCK_QUALITY_GATE_MAX_SYMBOLS") or "").strip() or "40"))

# ── Market-cap filter (2026-09-11 addition) ────────────────────────────────
# User request: "on volume stock pick... add only those which is fundamental
# and technically ok" was missing a market-cap floor entirely — a stock
# could clear the fund/tech quality gate above on pure score despite being
# a micro/nano-cap with essentially no institutional participation, wide
# spreads, and easy price manipulation on light volume.
#
# Tier bands below are approximate INR-crore buckets in common retail/
# analyst use, NOT SEBI's exact rank-based percentile cutoffs (SEBI defines
# large-cap as NSE/BSE rank 1-100 by full market cap, mid-cap 101-250,
# small-cap 251+ — cumulative full-universe ranking, re-set every 6 months,
# which this service doesn't compute). Good enough for a practical filter;
# override via env if your own scan universe skews differently.
MARKET_CAP_LARGE_CR = float(((os.getenv("MARKET_CAP_LARGE_CR") or "").strip() or "20000"))
MARKET_CAP_MID_CR   = float(((os.getenv("MARKET_CAP_MID_CR") or "").strip() or "5000"))
MARKET_CAP_SMALL_CR = float(((os.getenv("MARKET_CAP_SMALL_CR") or "").strip() or "500"))
# Absolute floor — enforced regardless of regime/adaptive tilt (see
# adaptive_market_params.adaptive_min_market_cap_cr). This is a structural
# liquidity/manipulation-risk floor, same category as RISK_MIN_STOCK_PRICE
# in risk_engine/engine.py, not a tactical call — sub-₹500cr names have the
# thinnest institutional coverage and are most exposed to pump-and-dump/
# operator activity on NSE.
MIN_MARKET_CAP_CR_ABSOLUTE_FLOOR = float(((os.getenv("CANDIDATE_MIN_MARKET_CAP_CR_ABSOLUTE_FLOOR") or "").strip() or "500"))
# Static fallback (used until adaptive_market_params has 30 distinct days
# of self-recorded history, or if it errors) and base value the regime tilt
# is applied around.
#
# 2026-09-11 CALIBRATION NOTE: this sandbox has no access to NSE/Dhan
# historical data APIs to run an actual 6-month backtest (network egress
# here is restricted to pypi/npm/github, confirmed while building this) —
# so this starting value is grounded in verified CURRENT published market
# data, not a from-scratch backtest:
#   - Nifty 50 ~23,200-23,300 (11-Sep-2026), a 3-month low, -13.67% YTD
#     price return through 9-Sep-2026 (independent tracker estimate) —
#     WORSE than the -7% 6m figure this file's own Aug-28 header used, i.e.
#     the correction that was already being defended against has deepened.
#   - India VIX ~11.5-11.9 (11-Sep-2026) vs its own 52-week range of
#     8.72-28.91 — LOW/calm, far from its own high. Options-market fear is
#     NOT elevated despite the index being weak: a grinding decline, not a
#     panic/capitulation event.
#   - NSE 500 breadth: 274/500 stocks above their 200-day SMA (early-Sep
#     reading), down from 301 the prior month — deteriorating but still a
#     majority-positive breadth, not a collapse.
# Net read: a real but orderly correction, not a volatility spike. That
# combination argues for a MODERATE tilt toward higher-quality/larger names
# (raise the floor somewhat from the absolute ₹500cr minimum) rather than
# either ignoring the correction or applying full panic-regime defensiveness
# a VIX spike would call for. ₹1,500cr as the static base reflects that
# middle ground — comfortably past micro-cap risk, still well below
# large-cap, leaving room for the midcap/smallcap outperformance this
# file's own Aug-28 header already documented (Midcap100 +12.88% 1Y vs
# Nifty -1.08% at the time) once the adaptive tilt below loosens it back
# down in a stronger reading.
MIN_MARKET_CAP_CR_STATIC = float(((os.getenv("CANDIDATE_MIN_MARKET_CAP_CR") or "").strip() or "1500"))

# ── Adaptive threshold engine configuration ───────────────────────────────────
# Controls how adaptive_thresholds.py computes the live regime gate.
ADAPTIVE_HISTORY_DAYS      = int(((os.getenv("ADAPTIVE_HISTORY_DAYS") or "").strip() or "90"))
ADAPTIVE_MIN_HISTORY_DAYS  = int(((os.getenv("ADAPTIVE_MIN_HISTORY_DAYS") or "").strip() or "30"))
ADAPTIVE_PERCENTILE        = float(((os.getenv("ADAPTIVE_PERCENTILE") or "").strip() or "20.0"))
ADAPTIVE_STALE_THRESHOLD_DAYS = int(((os.getenv("ADAPTIVE_STALE_THRESHOLD_DAYS") or "").strip() or "30"))

# ── Shared Dhan account-wide order-rate budget (session 7 audit finding —
# shared with position-stocks-service via the same Dhan account and the
# same physical DB; see execution/shared_order_budget.py) ────────────────────
SHARED_DAILY_ORDER_BUDGET = int(((os.getenv("SHARED_DAILY_ORDER_BUDGET") or "").strip() or "5000"))

# ── Cross-service capital split (session52 fix) ───────────────────────────────
# This service and position-stocks-service share ONE real Dhan account.
# position-stocks-service has always enforced its own half in software
# (capital/ledger.py: SCALP_POOL_CAPITAL_SHARE_PCT, default 50.0) — but until
# now nothing on THIS side ever reserved the matching half, so equity_sync.py
# sized every REAL trade off the account's FULL, uncapped Dhan balance.
# Confirmed live (2026-09-16): this service held ~93.5% of total shared
# account equity across 9 open positions, leaving position-stocks-service's
# ledger starved down to a ₹52 allocation (it only ever gets 50% of whatever
# cash THIS service hasn't already grabbed).
#
# CAPITAL_SHARE_PCT is this service's own half of the shared account. It is
# a separate env var from position-stocks-service's SCALP_POOL_CAPITAL_
# SHARE_PCT (the two services don't share Python config) — if you change
# one, change the other to match so they keep summing to (at most) 100.
# See execution/equity_sync.py (caps cash_available/current_equity) and
# risk_engine/engine.py's new "capital_share_cap" check (caps this
# service's TOTAL exposure — cash + open positions — not just new cash).
CAPITAL_SHARE_PCT = float(((os.getenv("REAL_TRADE_CAPITAL_SHARE_PCT") or "").strip() or "50.0"))

# ── Transaction-cost model (2026-09-18 — user audit finding) ─────────────────
# Codebase audit found ZERO awareness of brokerage/STT/exchange charges/GST/
# stamp duty anywhere in entry_engine or risk_engine — a candidate's
# theoretical edge (target_pct * position_value) was never compared against
# what it actually costs to round-trip the position. Combined with 1%-of-
# equity risk sizing on ATR stops, this routinely produced 1-2 share
# positions on ₹800-1500 stocks, where fixed/percentage transaction costs are
# a much larger fraction of trade value than on a bigger position — very
# plausibly the real reason for "breakeven or small loss" outcomes despite a
# backtested edge. See cost_model.py for the actual estimate function.
#
# Rates below are standard NSE-equity levies as of 2026 for a discount
# broker (Dhan): STT/exchange-txn/SEBI/GST rates are fixed by law/exchange,
# not broker-specific, so these are accurate regardless of plan; BROKERAGE_*
# defaults assume Dhan's ₹0 discount-brokerage plans (both CNC delivery and
# intraday) — correct this env var if that ever changes. All estimates are
# deliberately conservative/approximate — verify against a real Dhan
# contract note periodically and adjust via env vars, no code change needed.
COST_MODEL_ENABLED = ((os.getenv("COST_MODEL_ENABLED") or "").strip() or "true").lower() == "true"

# Brokerage: flat ₹ per executed leg (BUY and SELL each count as one leg).
# Dhan's discount plans are ₹0 for both CNC and intraday as of 2026 — set
# BROKERAGE_PER_ORDER if that ever changes for this account.
BROKERAGE_PER_ORDER = float(((os.getenv("BROKERAGE_PER_ORDER") or "").strip() or "0.0"))

# STT (Securities Transaction Tax) — statutory, not broker-specific.
# Delivery (CNC): charged on BOTH legs. Intraday: SELL leg only.
STT_DELIVERY_PCT_PER_LEG   = float(((os.getenv("STT_DELIVERY_PCT_PER_LEG") or "").strip() or "0.10"))
STT_INTRADAY_SELL_PCT      = float(((os.getenv("STT_INTRADAY_SELL_PCT") or "").strip() or "0.025"))

# Exchange transaction charges + IPFT levy + SEBI turnover fee — both legs, both product types.
# 2026-10-08 (group 262): Dhan's published rate card (dhan.co/pricing) bills NSE equity at 0.0030699 % = the 0.00297 %
# exchange transaction charge (since 1 Oct 2024) + 0.0001 % IPFT. This file used 0.00325 and the dashboards 0.00345.
EXCHANGE_TXN_PCT   = float(((os.getenv("EXCHANGE_TXN_PCT") or "").strip() or "0.00297"))
IPFT_PCT           = float(((os.getenv("IPFT_PCT") or "").strip() or "0.0001"))
SEBI_TURNOVER_PCT  = float(((os.getenv("SEBI_TURNOVER_PCT") or "").strip() or "0.0001"))

# GST — 18 % on (brokerage + exchange txn + IPFT + SEBI fee), both legs. NOT on STT, stamp duty or the DP charge's base.
GST_PCT = float(((os.getenv("GST_PCT") or "").strip() or "18.0"))

# Stamp duty — BUY leg only. Delivery rate is higher than intraday.
STAMP_DUTY_BUY_PCT_DELIVERY = float(((os.getenv("STAMP_DUTY_BUY_PCT_DELIVERY") or "").strip() or "0.015"))
STAMP_DUTY_BUY_PCT_INTRADAY = float(((os.getenv("STAMP_DUTY_BUY_PCT_INTRADAY") or "").strip() or "0.003"))

# DP (Depository Participant) charge — flat ₹ + GST, per scrip per day, ONLY
# when actual T+1-settled demat holdings are sold (a genuine multi-day CNC
# hold, not a same-day round trip — see exit_engine's same-day INTRADAY
# product-type logic, which never touches real holdings). Applied by
# cost_model.py only when the caller explicitly says the sell is a real
# delivery sell of a previously-settled holding.
# group 262: Dhan's published DP charge is Rs 12.50 per instruction per ISIN + GST (= Rs 14.75). cost_model used 15.0 and
# the dashboards a hard-coded 13.5 with no GST; this is now the single source for the cost gate AND the charges report.
DP_CHARGE_FLAT = float(((os.getenv("DP_CHARGE_FLAT") or "").strip() or "12.5"))

# ── Cumulative brokerage report (2026-10-08, group 258) ─────────────────────
# charges_ledger.py sums brokerage over every filled order in trade_orders/trade_fills (never purged) using the
# same rate card as the dashboard Charges tab: INTRADAY/MIS = lower of Rs 20 and 0.03% per executed order,
# CNC delivery = Rs 0. MEASUREMENT ONLY: it does not feed any gate (BROKERAGE_PER_ORDER above, default 0, still
# drives cost_model.py). Set both CHARGES_* to 0 if your Dhan contract note shows no intraday brokerage.
CHARGES_BROKERAGE_PCT = float(((os.getenv("CHARGES_BROKERAGE_PCT") or "").strip() or "0.03"))
CHARGES_BROKERAGE_CAP_RS = float(((os.getenv("CHARGES_BROKERAGE_CAP_RS") or "").strip() or "20.0"))
CHARGES_DELIVERY_BROKERAGE_RS = float(((os.getenv("CHARGES_DELIVERY_BROKERAGE_RS") or "").strip() or "0.0"))

# Entry-time cost gate (see entry_engine/entry.py's Gate 5.6):
#   1. Position value must clear MIN_TRADE_VALUE — below this, fixed/
#      percentage costs dominate any realistic edge.
#   2. Expected edge in ₹ (qty * entry_price * target_pct) must clear the
#      estimated round-trip cost by at least MIN_EDGE_TO_COST_RATIO — a
#      trade whose entire theoretical profit is 1.2x its own transaction
#      cost is not a real edge once execution slippage is added.
# 2026-09-21 fix (session79): was 3000.0. On a ~₹11K account that floor
# forced every position to be 27%+ of equity, which then collided head-on
# with the 25% position-concentration cap (risk_engine/engine.py) — the
# two gates rejected almost everything between them. User explicitly asked
# for "no min trade value... if needed set one like 10 or 20 rs", so this
# is now just a sanity floor against near-zero, cost-dominated orders, not
# a sizing constraint.
MIN_TRADE_VALUE          = float(((os.getenv("MIN_TRADE_VALUE") or "").strip() or "20.0"))
MIN_EDGE_TO_COST_RATIO   = float(((os.getenv("MIN_EDGE_TO_COST_RATIO") or "").strip() or "3.0"))

# 2026-09-21 NEW (session79): flat rupee ceiling on a single trade's
# position value (entry_price × final_qty), separate from and in addition
# to risk_engine's existing MAX_POSITION_CONCENTRATION_PCT (a %-of-equity
# cap). User explicitly asked for a flat ₹3,000 max per trade. Admin-
# editable per-mode via TradeRiskConfig.max_trade_value (POST /risk-config,
# field "max_trade_value") — None on that row falls back to this default,
# same NULL-means-"use config default" pattern as MIN_TRADE_VALUE above.
# Enforced in risk_engine/engine.py (§5c-ii), which downsizes qty to fit
# under the cap the same way the concentration cap does, rather than
# rejecting outright.
# group 264 (2026-10-09): entry product routing + same-day re-entry guard (see entry_product.py). ENTRY_PRODUCT_MODE=cnc
# restores the old always-CNC behaviour without a code change.
# DEFAULT IS "cnc" (group 266): the Dhan contract note of 07-Oct-2026 showed a same-day CNC round trip is charged at
# intraday rates anyway, so routing to MIS saves nothing on STT / brokerage / stamp; it only helps when a CNC exit would
# otherwise be carried overnight (DP + delivery STT). Set "auto" to route same-day-exit entries to MIS.
ENTRY_PRODUCT_MODE       = ((os.getenv("ENTRY_PRODUCT_MODE") or "").strip() or "cnc").lower()
ENTRY_MIS_LAST_TIME_IST  = ((os.getenv("ENTRY_MIS_LAST_TIME_IST") or "").strip() or "14:45")
# After a position in a symbol was fully closed today, a fresh BUY of the same symbol the same day is blocked
# (churn pays a second round of STT/stamp + exchange charges). Stop-loss exits are unaffected. false = old behaviour.
# Exit-side DP guard: a profit-TARGET partial sale of a CARRIED CNC holding (bought on an earlier day, so the sale pays the
# flat DP fee) is skipped while its gross gain is under EXIT_DP_MIN_GAIN_RATIO x the sell-leg cost (STT + DP + ...).
# The stop is raised to breakeven instead so the winner is still protected. Stops / emergency / time exits are never held back.
EXIT_DP_GUARD_ENABLED    = ((os.getenv("EXIT_DP_GUARD_ENABLED") or "").strip() or "true").lower() == "true"
EXIT_DP_MIN_GAIN_RATIO   = float(((os.getenv("EXIT_DP_MIN_GAIN_RATIO") or "").strip() or "3.0"))
REENTRY_SAME_DAY_BLOCK   = ((os.getenv("REENTRY_SAME_DAY_BLOCK") or "").strip() or "true").lower() == "true"
MAX_TRADE_VALUE          = float(((os.getenv("MAX_TRADE_VALUE") or "").strip() or "3000.0"))

# ── Selective overnight hold (2026-09-18 — user audit finding) ───────────────
# _eod_squareoff (execution/auto_pilot.py) used to flatten EVERY open
# position at EOD_SQUAREOFF_TIME_IST, unconditionally — including positions
# candidate_engine had already tagged high_conviction/upper_circuit with a
# backtested Day+1 continuation edge (55.7%/69.7% win rate) and time_stop_
# hint="EOD+1". That field was computed every cycle and never read anywhere
# — the position got force-sold same-day regardless, before the edge it was
# scored on ever had a chance to play out. This lets a NARROW, capped subset
# of positions skip the square-off instead of the previous all-or-nothing
# behavior. Everything else (base VOLUME_SHOCK tier, anything not currently
# profitable, anything already extended near the day's high) still squares
# off exactly as before — this does not change behavior for the bulk of
# positions, only for the specific cases the pipeline already has strong
# evidence for.
OVERNIGHT_HOLD_ENABLED = ((os.getenv("OVERNIGHT_HOLD_ENABLED") or "").strip() or "true").lower() == "true"

# Only these entry decision labels are eligible — deliberately excludes the
# base "VOLUME_SHOCK" tier (48.1% backtested win rate, +0.66% mean — too
# thin an edge to justify overnight gap risk) and plain "BUY NOW"/"PREPARE
# TO BUY" (no Day+1 continuation backtest attached at all).
OVERNIGHT_HOLD_ELIGIBLE_LABELS = {
    s.strip().upper() for s in os.getenv(
        "OVERNIGHT_HOLD_ELIGIBLE_LABELS",
        "VOLUME_SHOCK_UPPER_CIRCUIT,VOLUME_SHOCK_HIGH_CONVICTION",
    ).split(",") if s.strip()
}

# Must be at/above breakeven right now — never hold a currently-losing
# position overnight on the strength of a tier-level backtest statistic.
OVERNIGHT_HOLD_REQUIRE_PROFITABLE = ((os.getenv("OVERNIGHT_HOLD_REQUIRE_PROFITABLE") or "").strip() or "true").lower() == "true"

# Skip the hold if price is already this far into today's range (near the
# high) — mirrors entry_engine's own _range_adjusted_stop_target near_high
# logic (rpos >= 0.80): a position that's already run to the top of its
# day's range is more likely exhausted than mid-breakout.
OVERNIGHT_HOLD_MAX_RANGE_POS = float(((os.getenv("OVERNIGHT_HOLD_MAX_RANGE_POS") or "").strip() or "0.80"))

# Total value held overnight across ALL kept positions cannot exceed this %
# of equity, regardless of how many individually qualify — caps aggregate
# gap-risk exposure. When more positions qualify than the cap allows, the
# highest-conviction ones are kept first and the rest are squared off as
# usual (see execution/auto_pilot.py's _select_overnight_holds).
OVERNIGHT_HOLD_MAX_EXPOSURE_PCT = float(((os.getenv("OVERNIGHT_HOLD_MAX_EXPOSURE_PCT") or "").strip() or "40.0"))

# 2026-09-19 (audit finding, fixed): the exposure cap above bounds TOTAL
# overnight value but does nothing to stop that value concentrating into
# a handful of correlated names — positions are ranked purely by
# conviction, so 3-4 momentum names from the same sector could all be
# held overnight together, which is exactly when gap risk is correlated
# (a sector-wide open, not an idiosyncratic one, hits all of them at
# once). No sector data is reliably available on TradePosition without
# an extra live fundamental-service call at the worst possible time
# (15:00 IST, latency-sensitive), so this uses two cheaper, robust
# proxies instead: a hard cap on how many distinct symbols can be held
# overnight, and a cap on how much of equity any ONE symbol can occupy
# overnight — both enforced in execution/auto_pilot.py's
# _select_overnight_holds alongside the aggregate % cap above.
OVERNIGHT_HOLD_MAX_POSITIONS = int(((os.getenv("OVERNIGHT_HOLD_MAX_POSITIONS") or "").strip() or "3"))
OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT = float(((os.getenv("OVERNIGHT_HOLD_MAX_SINGLE_SYMBOL_PCT") or "").strip() or "15.0"))
# 2026-09-20 (audit fix — sector/correlation diversification): session72's
# MAX_POSITIONS/MAX_SINGLE_SYMBOL_PCT caps bound how MANY names and how much
# of ANY ONE name can be held overnight, but say nothing about whether those
# names are actually diversified — three of session72's own 3-position cap
# could still all be, say, IT names, concentrating gap risk in one overnight
# catalyst (a US tech selloff, a sector-wide regulatory headline) instead of
# spreading it. This caps how many overnight holds may share the same KNOWN
# NSE sector (market_context/sector_signal.py's NSE_SECTOR_MAP — the same
# map already used for the overnight US-sector signal, deliberately partial
# per that module's own documented caveat). A symbol with NO recognized
# sector is never compared against another for this specific check (there's
# nothing to compare — see _select_overnight_holds for how this degrades
# honestly rather than either blocking everything on missing data or
# silently skipping the check).
OVERNIGHT_HOLD_MAX_PER_SECTOR = int(((os.getenv("OVERNIGHT_HOLD_MAX_PER_SECTOR") or "").strip() or "1"))

# 2026-09-19 (audit finding, fixed): OVERNIGHT_HOLD_REQUIRE_PROFITABLE
# above only checked raw LTP >= avg_entry_price — a position sitting at
# exact breakeven on price is still a NET LOSER once brokerage/STT/GST/
# stamp duty are included, which is inconsistent with cost_model.py's
# entry-time edge-vs-cost gate (entry_engine/entry.py Gate 5.6) elsewhere
# in this same pipeline. When true, the overnight-hold profitability
# check requires unrealized gross P&L to exceed the estimated round-trip
# cost of exiting now, not just to be >= 0. Kept togglable in case the
# stricter bar ever needs to be relaxed for testing.
OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS = ((os.getenv("OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS") or "").strip() or "true").lower() == "true"

# ── Overnight-hold × CDSL eDIS morning check (2026-09-19, audit finding) ────
# Every same-day exit in this service sells product_type=INTRADAY, which
# never touches CDSL. A position carried overnight by OVERNIGHT_HOLD_ENABLED
# becomes a real T+1 CNC holding — see execution/dhan_client.py's
# eDIS/TPIN documentation — and selling it the next day (including a
# stop-loss or emergency-gap-down exit) needs the account holder to
# manually verify holdings in the Dhan app first, or the SELL is
# rejected. dhan_client.edis_verification_summary() already existed for
# a manual dashboard check but was never called proactively — so a
# forgotten morning verification could silently leave an overnight
# position's stop-loss unable to fire during exactly the highest-risk
# window (the open gap). When enabled, a scheduled pre-market check
# (execution/auto_pilot.py) calls it once per day and sends a loud
# reminder notification if any held-overnight symbol isn't yet verified.
EDIS_MORNING_CHECK_ENABLED = ((os.getenv("EDIS_MORNING_CHECK_ENABLED") or "").strip() or "true").lower() == "true"
EDIS_MORNING_CHECK_TIME_IST = os.getenv("EDIS_MORNING_CHECK_TIME_IST", "09:00")

# ── Limit orders for profit-target exits (2026-09-18 — user audit finding) ───
# Every automatic exit (stop/emergency/time_stop/eod_squareoff/target) was
# sent as a MARKET order. Correct for anything protecting capital, but a
# needless slippage cost on a profit-target hit, where there's no urgency.
# Only affects the target_hit_partial path in exit_engine/exit.py — stop_hit,
# emergency_gap_down, time_stop, and eod_squareoff are all unchanged and
# always MARKET, on purpose (must fill regardless of price).
EXIT_TARGET_USE_LIMIT = ((os.getenv("EXIT_TARGET_USE_LIMIT") or "").strip() or "true").lower() == "true"
EXIT_TARGET_LIMIT_BUFFER_PCT = float(((os.getenv("EXIT_TARGET_LIMIT_BUFFER_PCT") or "").strip() or "0.1"))

# ── Exit-side LIMIT sell expiry (2026-09-18 audit fix #1) ────────────────────
# EXIT_TARGET_USE_LIMIT above fixed slippage on winners but opened a real gap:
# exit.py's _has_pending_real_sell() blocks EVERY exit check (stop-hit,
# target, time-stop, trail) for a position while any SELL for that symbol is
# still PLACED/PARTIAL at Dhan — a guard written back when all exits were
# MARKET orders that filled in under a second. A partial-target LIMIT sell
# that doesn't fill quickly (illiquid name, circuit-limit halt, a fast gap
# away from the 0.1%-buffered limit price) can now sit PLACED all day with
# nothing to cancel/replace it — and for as long as it sits there, the
# stop-loss for the remaining position is never checked. That's exactly the
# scenario (a violent, fast move) where the stop matters most.
# The entry side already has exactly this pattern (ENTRY_VALIDITY_MINUTES +
# expire_stale_orders()) but valid_until was only ever set on BUY orders.
# EXIT_LIMIT_VALIDITY_MINUTES gives exit-side LIMIT SELL orders their own
# short window; exit_engine/exit.py's expire_stale_exit_orders() cancels an
# unfilled one at that point and immediately resends the remaining qty as
# MARKET, so the position is never left with a dark stop for longer than
# this window.
EXIT_LIMIT_VALIDITY_MINUTES = int(((os.getenv("EXIT_LIMIT_VALIDITY_MINUTES") or "").strip() or "3"))
