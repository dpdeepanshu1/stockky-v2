"""config.py (fno-options-service) - every setting, read blank-safe from the environment.

`(os.getenv(X) or "").strip() or default`: an empty `X=` line in an env_file can never crash an import or turn a number into "".
The switches that appear on the Real Money Calibration page (control/catalog.py) are the FNO_* names below; they are module attributes, and every
consumer reads them as `config.NAME` at call time, so a change on the page is effective on the next call (scripts/check_control_catalog.py proves it).

SAFETY DEFAULTS: the engine is OFF, the mode is DEMO (virtual money), REAL orders are OFF. Nothing in this service places a real order unless
FNO_ENGINE_ENABLED, FNO_MODE=REAL, FNO_REAL_ENABLED and the per-session ARM are ALL on (see execution.py).
"""
from __future__ import annotations

import os

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _s(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _f(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        v = float(raw) if raw else default
    except (TypeError, ValueError):
        v = default
    return default if v != v else v


def _i(name: str, default: int) -> int:
    return int(_f(name, float(default)))


def _b(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return default


# ---- service plumbing -----------------------------------------------------------------------------------------------
PORT = _i("PORT", 8007)
SESSION_SECRET = _s("SESSION_SECRET")                       # legacy shared secret; the per-service keys are read by session_keys.py
MARKET_DATA_URL = _s("MARKET_DATA_URL", "http://market-data-service:8001").rstrip("/")
MARKET_DATA_TIMEOUT_S = _f("FNO_MARKET_DATA_TIMEOUT_S", 12.0)
REAL_TRADE_URL = _s("REAL_TRADE_URL", "http://real-trade-service:8005").rstrip("/")   # only used to borrow the Dhan token for REAL orders
DATA_DIR = _s("FNO_DATA_DIR", "/data/fno")                   # sqlite db, chain snapshots, backtest outputs (a docker volume)

# The official 2026 NSE trading-holiday list: the EIGHTH copy of the list the other services keep (see scripts/check_holiday_lists_sync.py, which compares this one too).
_NSE_HOLIDAYS_2026 = {
    "2026-01-15",  # Maharashtra Municipal Corporation elections
    "2026-01-26",  # Republic Day
    "2026-03-03",  # Holi
    "2026-03-26",  # Ram Navami
    "2026-03-31",  # Mahavir Jayanti
    "2026-04-03",  # Good Friday
    "2026-04-14",  # Dr. Ambedkar Jayanti
    "2026-05-01",  # Maharashtra Day
    "2026-05-28",  # Bakri Eid
    "2026-06-26",  # Muharram
    "2026-09-14",  # Ganesh Chaturthi
    "2026-10-02",  # Gandhi Jayanti
    "2026-10-20",  # Dussehra
    "2026-11-10",  # Diwali Balipratipada
    "2026-11-24",  # Guru Nanak Jayanti
    "2026-12-25",  # Christmas
    # 2027 PENDING: NSE publishes it in December 2026. Not guessed; add here AND in the other copies, then run scripts/check_holiday_lists_sync.py.
}


def _dates(name: str, default: tuple) -> tuple:
    """Comma / space separated YYYY-MM-DD list, blank-safe; a malformed entry is dropped, never a crash. Blank or absent = `default`."""
    import re
    from datetime import date
    raw = _s(name, "")
    if not raw:
        return tuple(sorted(default))
    out = []
    for tok in re.split(r"[,\s;]+", raw):
        try:
            out.append(date.fromisoformat(tok).isoformat())
        except ValueError:
            continue
    return tuple(sorted(set(out)))


# Weekday exchange holidays (IST dates) the engine skips. Round 272: the engine used to be built with NO holidays. Round 274: default = the full official list above;
# FNO_HOLIDAYS (if set) REPLACES it, e.g. for a 2027 list before this file is updated.
HOLIDAYS = _dates("FNO_HOLIDAYS", tuple(_NSE_HOLIDAYS_2026))
POLL_INTERVAL_S = _f("FNO_POLL_INTERVAL_S", 5.0)             # engine tick; Dhan's chain limit (1 request / 3 s) is enforced upstream
CHAIN_INTERVAL_S = _f("FNO_CHAIN_INTERVAL_S", 6.0)           # per underlying
SNAPSHOT_INTERVAL_S = _f("FNO_SNAPSHOT_INTERVAL_S", 60.0)    # how often a chain snapshot is recorded for the replay backtest
INDEX_BARS_REFRESH_S = _f("FNO_INDEX_BARS_REFRESH_S", 60.0)
STATE_BACKEND = _s("FNO_STATE_BACKEND", "sqlite")
DAILY_EXCLUDE_TODAY = _b("FNO_DAILY_EXCLUDE_TODAY", True)        # round 287: the live direction logic reads the daily candles up to YESTERDAY, like every backtest (the session still running is a repainting candle). false = the old behaviour (env only)

# ---- exchange facts used only as FALLBACKS (the live value is the lot size in Dhan's scrip master) -----------------------
LOT_FALLBACK = {"NIFTY": _i("FNO_LOT_NIFTY", 65), "SENSEX": _i("FNO_LOT_SENSEX", 20)}   # Nov 2026; check with /dhan/options/contracts

# ---- switches (control/catalog.py) -----------------------------------------------------------------------------------
FNO_ENGINE_ENABLED = _b("FNO_ENGINE_ENABLED", False)         # master: run the signal + DEMO engine loop
FNO_MODE = _s("FNO_MODE", "DEMO").upper()                    # DEMO | REAL
FNO_REAL_ENABLED = _b("FNO_REAL_ENABLED", False)             # second lock: without it a REAL-mode engine only logs what it would send
FNO_REAL_REQUIRES_READINESS = _b("FNO_REAL_REQUIRES_READINESS", True)   # round 262: REAL orders wait until every hard item of GET /fno/readiness is green (fno/readiness.py); OFF = the list only advises
FNO_READINESS_BLOCKERS = None                                # set by readiness.publish every 30 s: [] = all hard items green, a list = what is open, None = not computed yet (REAL stays closed)
FNO_UNDERLYINGS = _s("FNO_UNDERLYINGS", "BOTH").upper()      # NIFTY | SENSEX | BOTH
FNO_SIGNAL_MIN_SCORE = _f("FNO_SIGNAL_MIN_SCORE", 45.0)
# strategies (fno/strategies.py): CORE is the original signal and stays ON; the named ones ship OFF until their DEMO / SIM evidence is read
FNO_STRAT_CORE = _b("FNO_STRAT_CORE", True)
FNO_STRAT_TREND_RIDER = _b("FNO_STRAT_TREND_RIDER", False)
FNO_STRAT_MOMENTUM_SURGE = _b("FNO_STRAT_MOMENTUM_SURGE", False)
FNO_STRAT_BEAR_SWEEP = _b("FNO_STRAT_BEAR_SWEEP", False)
FNO_STRAT_BEAR_PIVOT = _b("FNO_STRAT_BEAR_PIVOT", False)
FNO_STRAT_DRIFT_EDGE = _b("FNO_STRAT_DRIFT_EDGE", False)
FNO_STRAT_WEEKLY_OPENER = _b("FNO_STRAT_WEEKLY_OPENER", False)
FNO_STRAT_EXPIRY_MOMENTUM = _b("FNO_STRAT_EXPIRY_MOMENTUM", False)   # round 236: expiry-day strategies. OFF; DEMO-only unless FNO_EXPIRY_REAL (env only)
FNO_STRAT_EXPIRY_ALPHA = _b("FNO_STRAT_EXPIRY_ALPHA", False)
FNO_STRATEGY_MAX_PER_DAY = _i("FNO_STRATEGY_MAX_PER_DAY", 1)  # entries per day for each non-core strategy
FNO_SL_PCT = _f("FNO_SL_PCT", 25.0)                          # stop loss as % of the entry premium
FNO_TARGET_PCT = _f("FNO_TARGET_PCT", 50.0)
FNO_TRAIL_ARM_PCT = _f("FNO_TRAIL_ARM_PCT", 30.0)            # profit % at which the trailing stop switches on
FNO_TRAIL_PCT = _f("FNO_TRAIL_PCT", 20.0)                    # trail distance, % below the best premium seen
FNO_RISK_PER_TRADE_PCT = _f("FNO_RISK_PER_TRADE_PCT", 1.0)   # % of capital lost if the stop is hit
FNO_MAX_LOTS = _i("FNO_MAX_LOTS", 2)
FNO_MAX_OPEN_POSITIONS = _i("FNO_MAX_OPEN_POSITIONS", 2)
FNO_MAX_TRADES_PER_DAY = _i("FNO_MAX_TRADES_PER_DAY", 3)
FNO_MAX_DAILY_LOSS_PCT = _f("FNO_MAX_DAILY_LOSS_PCT", 3.0)
FNO_ENTRY_START = _s("FNO_ENTRY_START", "09:30")
FNO_ENTRY_END = _s("FNO_ENTRY_END", "14:30")
FNO_SQUAREOFF_AT = _s("FNO_SQUAREOFF_AT", "15:15")
FNO_VIX_MAX = _f("FNO_VIX_MAX", 25.0)
FNO_MIN_DAYS_TO_EXPIRY = _i("FNO_MIN_DAYS_TO_EXPIRY", 1)
FNO_BLOCK_EXPIRY_DAY = _b("FNO_BLOCK_EXPIRY_DAY", True)
FNO_MAX_SPREAD_PCT = _f("FNO_MAX_SPREAD_PCT", 3.0)
FNO_MIN_PREMIUM = _f("FNO_MIN_PREMIUM", 20.0)
FNO_MAX_DATA_AGE_S = _i("FNO_MAX_DATA_AGE_S", 25)
FNO_RECORD_SNAPSHOTS = _b("FNO_RECORD_SNAPSHOTS", True)
FNO_DEMO_CAPITAL = _f("FNO_DEMO_CAPITAL", 500000.0)
# Round 242: the three-way split of the one Dhan account (capital_board.py). Real Trade decides the board (it owns the Dhan balance); this service publishes its figures and, in REAL mode only,
# SIZES FROM ITS BOARD AMOUNT instead of FNO_DEMO_CAPITAL (a virtual 5,00,000 must never size a real order). No fresh board = no REAL entry (fail closed). off | shadow (default) | live.
FNO_REAL_FIXED_CAPITAL = max(0.0, _f("FNO_REAL_FIXED_CAPITAL", 0.0))   # round 387 (E403): a fixed rupee amount REAL sizing may use, instead of the capital board (an idle F&O gets 0 from the board, so it could never start). 0 = off = the board decides. Open REAL premium counts against it.
CAPITAL_ALLOC_MODE = _s("CAPITAL_ALLOC_MODE", "shadow").lower()
if CAPITAL_ALLOC_MODE not in ("off", "shadow", "live"):
    CAPITAL_ALLOC_MODE = "off"                                   # a typo must not switch it on
CAPITAL_ALLOC_DIR = _s("CAPITAL_ALLOC_DIR", "/data/shared")      # the shared-runtime volume (also mounted in Real Trade and Position Stocks)
CAPITAL_ALLOC_WINDOW_DAYS = max(5, min(180, _i("CAPITAL_ALLOC_WINDOW_DAYS", 30)))
CAPITAL_ALLOC_MAX_AGE_S = max(300.0, _f("CAPITAL_ALLOC_MAX_AGE_S", 21600.0))   # a board older than this is not trusted
FNO_SLIPPAGE_PCT = _f("FNO_SLIPPAGE_PCT", 0.5)               # paper fills: buy at ask-or-ltp + this %, sell at bid-or-ltp - this %

# ---- expiry-day rules (round 236). Not on the page on purpose: these are safety limits, and FNO_EXPIRY_REAL in particular must never be one click away -------
# named-strategy thresholds (round 237). Env only; defaults are the values the strategies always used. Change them only after a calibration run on real data.
FNO_STRAT_TREND_D = _f("FNO_STRAT_TREND_D", 40.0)
FNO_STRAT_TREND_I = _f("FNO_STRAT_TREND_I", 15.0)
FNO_STRAT_CHAIN_VETO = _f("FNO_STRAT_CHAIN_VETO", 15.0)
FNO_STRAT_SWEEP_MAX_D = _f("FNO_STRAT_SWEEP_MAX_D", 50.0)
FNO_CORE_MIN_INTRADAY_BARS = max(0, _i("FNO_CORE_MIN_INTRADAY_BARS", 6))     # round 388 (E404): switch, 0 = off, else on: CORE needs a scoreable intraday part (6 bars of today)
FNO_STRAT_PIVOT_MAX_D = _f("FNO_STRAT_PIVOT_MAX_D", 15.0)
FNO_STRAT_OPENER_D = _f("FNO_STRAT_OPENER_D", 15.0)
FNO_STRAT_SURGE_BODY = _f("FNO_STRAT_SURGE_BODY", 0.6)
FNO_STRAT_SURGE_CLOSE = _f("FNO_STRAT_SURGE_CLOSE", 0.7)
FNO_STRAT_SURGE_VOL_MULT = _f("FNO_STRAT_SURGE_VOL_MULT", 1.3)
FNO_STRAT_EXPIRY_BODY = _f("FNO_STRAT_EXPIRY_BODY", 0.5)
FNO_STRAT_ALPHA_MOVE = _f("FNO_STRAT_ALPHA_MOVE", 0.5)
FNO_STRAT_ALPHA_PAUSE = _f("FNO_STRAT_ALPHA_PAUSE", 0.4)
FNO_STRAT_DRIFT_TOUCH_PCT = _f("FNO_STRAT_DRIFT_TOUCH_PCT", 0.08)
FNO_STRAT_SURGE_RANGE_MULT = _f("FNO_STRAT_SURGE_RANGE_MULT", 1.5)
FNO_STRAT_EXPIRY_RANGE_MULT = _f("FNO_STRAT_EXPIRY_RANGE_MULT", 1.3)
FNO_STRAT_DRIFT_CLOSES = _i("FNO_STRAT_DRIFT_CLOSES", 10)
FNO_STRAT_DRIFT_SLOPE_PCT = _f("FNO_STRAT_DRIFT_SLOPE_PCT", 0.04)
FNO_STRAT_PIVOT_BREAK_PCT = _f("FNO_STRAT_PIVOT_BREAK_PCT", 0.03)
FNO_EXPIRY_ENTRY_END = _s("FNO_EXPIRY_ENTRY_END", "13:30")        # last entry on the contract's expiry day
FNO_EXPIRY_SQUAREOFF_AT = _s("FNO_EXPIRY_SQUAREOFF_AT", "14:45")  # out before 15:00: from 7 Dec 2026 an expiring index contract settles on the last-30-minute VWAP of its constituents
FNO_EXPIRY_RISK_MULT = _f("FNO_EXPIRY_RISK_MULT", 0.5)            # risk per trade on expiry day = FNO_RISK_PER_TRADE_PCT x this
FNO_EXPIRY_REAL = _b("FNO_EXPIRY_REAL", False)                    # expiry-day entries in REAL mode: OFF until 30+ DEMO expiry days have been read

# ---- selection (not on the page: these shape the contract picker) ------------------------------------------------------------
TARGET_DELTA = _f("FNO_TARGET_DELTA", 0.52)
DELTA_MIN = _f("FNO_DELTA_MIN", 0.35)
DELTA_MAX = _f("FNO_DELTA_MAX", 0.65)
STRIKES_EACH_SIDE = _i("FNO_STRIKES_EACH_SIDE", 3)           # how many strikes around ATM are considered
MIN_OI = _f("FNO_MIN_OI", 50000.0)
MIN_VOLUME = _f("FNO_MIN_VOLUME", 1000.0)
RISK_FREE_RATE = _f("FNO_RISK_FREE_RATE", 0.065)

# ---- costs (round trip, option BUY then SELL). Correct them against a real Dhan contract note; no code change needed -------
BROKERAGE_PER_ORDER = _f("FNO_BROKERAGE_PER_ORDER", 20.0)
BROKERAGE_PCT_CAP = _f("FNO_BROKERAGE_PCT_CAP", 100.0)      # F&O: flat Rs 20 per executed order (VERIFY on the contract note). A cap below 100 turns it into min(Rs 20, cap % of turnover)
STT_SELL_PCT = _f("FNO_STT_SELL_PCT", 0.15)                  # option premium, sell side: 0.15 % from 1 Apr 2026 (NSE circular FATAX/73524; was 0.1 %). Confirmed round 236
EXCH_TXN_PCT_NSE = _f("FNO_EXCH_TXN_PCT_NSE", 0.03553)       # NSE circular NSE/FA/73061: 0.03553 % of premium (IPFT included) from 1 Mar 2026 (it was 0.03503 %). Still check one contract note
EXCH_TXN_PCT_BSE = _f("FNO_EXCH_TXN_PCT_BSE", 0.0325)        # BSE: Rs 3,250 per crore of premium for Sensex / Bankex options from 1 Oct 2024; no later change found. Check one contract note
SEBI_PCT = _f("FNO_SEBI_PCT", 0.0001)
STAMP_BUY_PCT = _f("FNO_STAMP_BUY_PCT", 0.003)
GST_PCT = _f("FNO_GST_PCT", 18.0)

# ---- REAL execution (only read by fno/execution.py; the Dhan token is NOT borrowed from real-trade-service yet - see the remaining-items list) ----------------------
DHAN_BASE_URL = _s("FNO_DHAN_BASE_URL", "https://api.dhan.co/v2").rstrip("/")
DHAN_CLIENT_ID = _s("FNO_DHAN_CLIENT_ID")
DHAN_ACCESS_TOKEN = _s("FNO_DHAN_ACCESS_TOKEN")              # a Dhan access token lasts about a day; renew it before the session
ORDER_TIMEOUT_S = _f("FNO_ORDER_TIMEOUT_S", 10.0)
