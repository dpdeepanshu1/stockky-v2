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
