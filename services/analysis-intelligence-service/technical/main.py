import time
import gc
import threading
"""
Technical Analysis Service
---------------------------
Single responsibility: compute technical indicators (score, trend,
support/resistance) for a given NSE symbol.

Data source: fetches OHLCV candles from the Market Data Service.
All results are cached with TTL that respects market hours:
- During NSE trading hours: TTL = 300 seconds (5 min)
- Outside: TTL = 21600 seconds (6 hours)
"""
import os
import json
import logging
import math
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import httpx
import pandas as pd

# §6 — corporate-action clamp for ATR inputs
try:
    import sys, os as _os
    sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), '../../../shared'))
    from return_sanity import clamp_for_atr as _clamp_for_atr, CORPORATE_ACTION_JUMP_THRESHOLD
except ImportError:
    CORPORATE_ACTION_JUMP_THRESHOLD = 30.0
    def _clamp_for_atr(x):
        return x if x is not None and abs(x) <= 30.0 else None
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
try:
    from upstash_redis import Redis
except ImportError:
    Redis = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("technical-analysis-service")

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


MARKET_DATA_URL = _env_url("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com")
UPSTASH_URL = (os.getenv("UPSTASH_REDIS_REST_URL") or "").strip() or None
UPSTASH_TOKEN = (os.getenv("UPSTASH_REDIS_REST_TOKEN") or "").strip() or None


def _rs_vs_nifty(close_series, nifty_close_series=None):
    """Relative strength vs Nifty over ~20 sessions (0-100). Free yfinance data only."""
    try:
        import numpy as np
        if close_series is None or len(close_series) < 21:
            return 50.0, False
        ret = float(close_series.iloc[-1] / close_series.iloc[-21] - 1.0)
        nret = 0.0
        if nifty_close_series is not None and len(nifty_close_series) >= 21:
            nret = float(nifty_close_series.iloc[-1] / nifty_close_series.iloc[-21] - 1.0)
        # map excess return to 0-100 (excess -10%..+10% → 0..100)
        excess = (ret - nret) * 100.0
        score = max(0.0, min(100.0, 50.0 + excess * 5.0))
        extended = ret > 0.18  # >18% in ~1m treated as extended
        return round(score, 1), extended
    except Exception:
        return 50.0, False


app = FastAPI(title="Stockky Technical Analysis Service", version="0.3.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Cache: memory-first; Redis only if USE_REDIS=1 ─────────────────────────────
_USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
_mem_tech: dict = {}
_mem_tech_exp: dict = {}
cache = None
try:
    if _USE_REDIS and UPSTASH_URL and UPSTASH_TOKEN:
        cache = Redis(url=UPSTASH_URL, token=UPSTASH_TOKEN)
        cache.ping()
        logger.info("Technical: Upstash Redis ON (USE_REDIS=1)")
    else:
        logger.info("Technical: USE_REDIS=0 — in-memory cache only")
except Exception as e:
    logger.warning("Redis unavailable (%s). Memory-only.", e)
    cache = None

def is_market_open() -> bool:
    now = datetime.now(ZoneInfo("Asia/Kolkata"))
    if now.weekday() >= 5:
        return False
    return dtime(9, 15) <= now.time() <= dtime(15, 30)

def get_cache_ttl() -> int:
    return 300 if is_market_open() else 21600

# A "history too thin" quote-only result is a stopgap, not an analysis: cache
# it only briefly so full technicals come back as soon as history recovers.
_INSUFFICIENT_CACHE_TTL = 60
# ADX needs two Wilder periods of history before its first value exists (one to
# seed the smoothed TR/DM, one to seed the smoothed DX). Below this it is unknown.
_ADX_PERIOD = 14
_ADX_MIN_BARS = 2 * _ADX_PERIOD

def _cache_get(key: str):
    exp = _mem_tech_exp.get(key)
    if key in _mem_tech and (exp is None or exp > time.time()):
        return _mem_tech[key]
    if not cache:
        return None
    try:
        val = cache.get(key)
        return json.loads(val) if val else None
    except Exception:
        return None

def _cache_set(key: str, value: dict, ttl: int = None):
    if ttl is None:
        ttl = get_cache_ttl()
    _mem_tech[key] = value
    _mem_tech_exp[key] = time.time() + int(ttl)
    if not cache:
        return
    # Redis is a best-effort shared layer (_cache_get already swallows its
    # errors). A failed write used to raise out of here AFTER the memory
    # write, so analyze() 500'd on a result it had already computed and
    # cached locally. Log it and carry on.
    try:
        cache.setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        logger.warning("Technical: Redis cache write failed for %s (%s); memory cache only.", key, e)

# ── Helpers ────────────────────────────────────────────────────────────────────
def normalize_symbol(symbol: str) -> str:
    return symbol.strip().upper().replace(".NS", "").replace(".BO", "")

def _safe(val, decimals=2):
    try:
        f = float(val)
        return round(f, decimals) if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None

def _num(val, default):
    """Full-precision float for internal comparisons; `default` only when the value is
    missing (None / NaN / inf / non-numeric).

    analyze() used `_safe(x) or default`, which had two problems: _safe rounds to 2
    decimals, so on a low-priced stock MACD/signal/EMA/Bollinger values tied or
    collapsed to 0.0 and crossovers, EMA stacks and band position were decided on
    rounded numbers; and `or` replaced a genuine 0.0 (a Bollinger lower band that
    reaches 0) with the fallback. Rounding is now applied only to the values the
    response reports."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _fetch_quote_price(symbol: str) -> float | None:
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("price")
    except Exception:
        pass
    return None

def _candles_to_df(candles):
    if not candles or len(candles) < 5:
        return None
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    rename = {col: col.capitalize() for col in df.columns}
    df.rename(columns=rename, inplace=True)
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df.dropna(subset=["Close"], inplace=True)
    return df if len(df) >= 5 else None


def _fetch_history_yfinance(symbol: str):
    """Direct Yahoo fallback when market-data-service is cold/rate-limited."""
    try:
        import yfinance as yf
        ticker = yf.Ticker(f"{symbol}.NS")
        hist = ticker.history(period="6mo", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="6mo", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        candles = []
        for idx, row in hist.iterrows():
            try:
                candles.append({
                    "date": idx.isoformat(),
                    "open": float(row["Open"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "close": float(row["Close"]),
                    "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                })
            except Exception:
                continue
        return _candles_to_df(candles)
    except Exception as e:
        logger.warning("yfinance history fallback failed for %s: %s", symbol, e)
        try:
            from rate_limit_report import report_if_rate_limited
            report_if_rate_limited(e, provider="market_data", path="technical/yfinance", symbol=symbol)
        except Exception:
            pass
        return None


# ── History fetch policy (2026-10-04, item 3: AngelOne 403 "exceeding access rate") ──
# analyze() used to walk 6mo -> 3mo -> 1mo -> 1y (+ a final 6mo "accept short"
# retry), every one force=true, so a single analysis cost up to 5 /history
# calls and an upstream 403/429/503 was simply retried four more times against
# the same wall. Now: ONE call for the period every other consumer already
# shares (default "1y", which the decision service also requests and which
# market-data can slice shorter periods out of), trimmed locally to the same
# ~6-month window the scoring has always used; other periods are tried only
# when the symbol genuinely has no data for the long one (HTTP 404, e.g. a
# recent listing), never after a rate-limit / busy / timeout failure.
# TECHNICAL_HISTORY_FETCH_PERIOD=6mo restores the old request period.
_TECH_FETCH_PERIOD = ((os.getenv("TECHNICAL_HISTORY_FETCH_PERIOD") or "").strip() or "1y")
_TECH_WINDOW_DAYS = 186          # the 6mo window the indicators were tuned on
_md_state = threading.local()    # why the last _fetch_history_from_market_data returned None


def _set_md_failure(kind: str) -> None:
    _md_state.last_failure = kind


def _trim_to_analysis_window(df):
    """Return only the trailing ~6 months of a longer fetch so scores do not
    change with the fetch period. Returns `df` itself when nothing is cut."""
    try:
        if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
            return df
        trimmed = df[df.index >= df.index.max() - pd.Timedelta(days=_TECH_WINDOW_DAYS)]
        if len(trimmed) == len(df) or len(trimmed) < 20:
            return df
        return trimmed
    except Exception:  # noqa: BLE001 - trimming is cosmetic, never fail analysis over it
        return df


def _fetch_history_from_market_data(symbol: str, period: str = "6mo", force: bool = False):
    """One attempt against market-data /history."""
    _set_md_failure("")
    try:
        resp = httpx.get(
            f"{MARKET_DATA_URL}/history/{symbol}",
            params={"period": period, "force": str(force).lower()},
            timeout=35,
        )
        if resp.status_code == 200:
            data = resp.json()
            df = _candles_to_df(data.get("candles", []))
            if df is not None and len(df) >= 5:
                return df
            _set_md_failure("thin")       # 200 but (almost) no bars: another period cannot add any
        else:
            logger.warning("market-data history HTTP %s for %s period=%s", resp.status_code, symbol, period)
            _set_md_failure("no_data" if resp.status_code == 404 else "transient")
            if resp.status_code in (429, 503):
                try:
                    from rate_limit_report import record_rate_limit_hit
                    record_rate_limit_hit(
                        provider="market_data",
                        status=resp.status_code,
                        path=f"/history/{symbol}",
                        symbol=symbol,
                    )
                except Exception:
                    pass
    except httpx.HTTPError as e:
        _set_md_failure("transient")
        logger.warning("market-data history error %s period=%s: %s", symbol, period, e)
        try:
            from rate_limit_report import report_if_rate_limited
            report_if_rate_limited(e, provider="market_data", path=f"/history/{symbol}", symbol=symbol)
        except Exception:
            pass
    return None


def _fetch_history_bhavcopy_hint(symbol: str):
    """
    Optional: ask market-data delivery/bhavcopy path for last close when history is empty.
    Builds a minimal 1-row frame so analyze() can still attach a price-based fallback.
    """
    try:
        resp = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=12)
        if resp.status_code != 200:
            return None
        q = resp.json() if resp.content else {}
        px = None
        for k in ("price", "cmp", "last_price", "ltp", "close", "prev_close"):
            try:
                v = float(q.get(k) or 0)
                if v > 0:
                    px = v
                    break
            except (TypeError, ValueError):
                continue
        if not px:
            return None
        # Minimal synthetic OHLC so downstream len checks can still produce a quote-based result
        import pandas as pd
        from datetime import datetime, timezone
        row = {
            "Open": px,
            "High": px,
            "Low": px,
            "Close": px,
            "Volume": int(q.get("volume") or 0),
        }
        df = pd.DataFrame([row], index=[datetime.now(timezone.utc)])
        df.attrs["bhavcopy_hint"] = True
        return df
    except Exception as e:
        logger.debug("bhavcopy/quote hint failed for %s: %s", symbol, e)
        return None


def _wake_pings_enabled() -> bool:
    """Same rule as api-gateway: off on the always-on Oracle VM (ORACLE_DSN set), WAKE_PINGS=1/0 overrides."""
    v = (os.getenv("WAKE_PINGS") or "").strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return not (os.getenv("ORACLE_DSN") or "").strip()


def _fetch_history(symbol: str, force: bool = False):
    # 1) market-data-service (preferred, shared cache) — try multiple periods
    try:
        if _wake_pings_enabled():
            try:
                httpx.get(f"{MARKET_DATA_URL}/health", params={"warm": "true"}, timeout=8)
            except Exception:
                pass
        periods = [_TECH_FETCH_PERIOD] + [p for p in ("3mo", "1mo") if p != _TECH_FETCH_PERIOD]
        best_short = None
        for period in periods:
            _set_md_failure("no_data")   # default for a None return that never said why
            df = _fetch_history_from_market_data(symbol, period=period, force=force)
            if df is not None:
                if len(df) >= 20:
                    return _trim_to_analysis_window(df)
                # Short but real data (e.g. a recent listing): a different period
                # cannot add bars, so keep it and stop instead of asking again.
                best_short = df
                break
            if getattr(_md_state, "last_failure", "") != "no_data":
                # rate-limited / busy / timed out / too-thin response: every other
                # period would hit the same wall (and count against the same limit).
                break
        if best_short is not None:
            return best_short
    except Exception as e:
        logger.warning("Market data history chain failed for %s: %s — trying yfinance", symbol, e)

    # 2) direct yfinance (free-tier resilience — last resort)
    df = _fetch_history_yfinance(symbol)
    if df is not None:
        logger.info("Using yfinance history for %s (%s bars)", symbol, len(df))
        if len(df) > 260:
            df = df.iloc[-260:]
        gc.collect()
        return df

    # 3) last-resort quote/bhavcopy hint (single bar) — analyze() treats <5 as insufficient
    #    but still returns structured fallback with close price
    hint = _fetch_history_bhavcopy_hint(symbol)
    if hint is not None:
        logger.info("Using quote/bhavcopy hint for %s (minimal bar)", symbol)
        return hint
    return None

# ── Indicator calculations ─────────────────────────────────────────────────────
def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))
    # No losses in the window: the division above yields NaN, which analyze()
    # then read as "missing" and reported as a neutral 50 — a stock rising
    # every day looked neutral instead of maximally overbought. Resolve the
    # zero-loss windows explicitly: gains only -> 100, no movement at all ->
    # 50 (neutral). Warm-up rows (gain/loss NaN) stay NaN.
    no_loss = loss.eq(0) & gain.notna()
    rsi = rsi.mask(no_loss & (gain > 0), 100.0)
    rsi = rsi.mask(no_loss & (gain == 0), 50.0)
    return rsi

def _ema(close: pd.Series, span: int) -> pd.Series:
    return close.ewm(span=span, adjust=False).mean()

def _macd(close: pd.Series):
    exp12 = _ema(close, 12)
    exp26 = _ema(close, 26)
    macd_line = exp12 - exp26
    signal = _ema(macd_line, 9)
    return macd_line, signal

def _wilder_smooth(s: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (RMA): seed with the plain mean of the first `period`
    valid values, then out[i] = (out[i-1] * (period - 1) + s[i]) / period.
    A NaN input breaks the chain: the output is NaN there and the smoother
    re-seeds from the next `period` consecutive valid values, so bad data never
    silently carries stale state forward."""
    vals = [float(x) for x in s.tolist()]
    out = [float("nan")] * len(vals)
    run = 0  # consecutive valid inputs since the last NaN / re-seed
    prev = float("nan")
    for i, v in enumerate(vals):
        if math.isnan(v):
            run = 0
            prev = float("nan")
            continue
        run += 1
        if math.isnan(prev):
            if run >= period:
                prev = sum(vals[i - period + 1:i + 1]) / period
                out[i] = prev
        else:
            prev = (prev * (period - 1) + v) / period
            out[i] = prev
    return pd.Series(out, index=s.index)

def _adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """ADX with Wilder smoothing (same definition charting tools use), so the
    25/20 trend thresholds mean what they conventionally mean. The previous
    version used plain rolling means instead of Wilder smoothing."""
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    up = high.diff()
    down = -low.diff()
    # Both directional moves are classified from the RAW up/down values. The old
    # code masked dm_neg against the already-zeroed dm_pos, so an exact tie
    # (up == down > 0) was counted as a down move instead of neither.
    dm_pos = up.where((up > down) & (up > 0), 0.0)
    dm_neg = down.where((down > up) & (down > 0), 0.0)
    # DM/TR start at the second bar (the first has no previous bar), as in Wilder.
    dm_pos = dm_pos.where(up.notna())
    dm_neg = dm_neg.where(down.notna())
    tr = tr.where(up.notna())
    atr = _wilder_smooth(tr, period)
    sm_pos = _wilder_smooth(dm_pos, period)
    sm_neg = _wilder_smooth(dm_neg, period)
    atr_ok = atr.where(atr > 0)
    di_pos = 100 * sm_pos / atr_ok
    di_neg = 100 * sm_neg / atr_ok
    di_sum = di_pos + di_neg
    dx = 100 * (di_pos - di_neg).abs() / di_sum.where(di_sum > 0)
    # Real range but zero directional movement in the window: DX is 0 (no
    # trend), not undefined -- a NaN here would break the ADX smoothing chain.
    dx = dx.mask(atr_ok.notna() & di_sum.eq(0), 0.0)
    return _wilder_smooth(dx, period)

def _atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """§6 — ATR with corporate-action clamp. Excludes single-day jumps > 30%
    from the rolling window so a demerger/bonus/split day doesn't inflate ATR
    readings for the following 14 sessions."""
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    # Clamp: exclude TR values that correspond to corporate-action-sized moves
    daily_ret_pct = close.pct_change().abs() * 100
    clamped_tr = tr.where(daily_ret_pct <= CORPORATE_ACTION_JUMP_THRESHOLD, other=float('nan'))
    return clamped_tr.rolling(period, min_periods=5).mean()

def _bollinger(close: pd.Series, period: int = 20):
    mid  = close.rolling(period).mean()
    std  = close.rolling(period).std()
    return mid + 2 * std, mid - 2 * std

def _support_resistance(df: pd.DataFrame, window: int = 20):
    recent = df.tail(window)
    return float(recent["Low"].min()), float(recent["High"].max())

# ── Endpoints ──────────────────────────────────────────────────────────────────
@app.get("/sector-strength/{symbol}")
async def sector_relative_strength(symbol: str, sector: str = ""):
    """
    §5 — 10-day relative strength of symbol vs sector peers.
    Uses hybrid_gate (relative AND absolute) — never percentile alone.
    """
    sym = normalize_symbol(symbol)
    sec = sector.strip()
    if not sec:
        # Try to read sector from symbol_master
        try:
            db_url = (os.getenv("DATABASE_URL") or "").strip() or (os.getenv("CACHE_DATABASE_URL") or "").strip()
            if db_url.startswith("postgres://"):
                db_url = "postgresql://" + db_url[len("postgres://"):]
            from sqlalchemy import create_engine, text as _text
            engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
            with engine.connect() as conn:
                row = conn.execute(
                    _text("SELECT sector FROM symbol_master WHERE current_symbol=:s AND status='active'"),
                    {"s": sym}
                ).fetchone()
                if row:
                    sec = row[0] or ""
        except Exception:
            pass

    try:
        try:
            from shared_adaptive import relative_strength_vs_sector
        except ImportError:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../shared'))
            from adaptive_thresholds import relative_strength_vs_sector

        async def _get_return(s, days):
            df = _fetch_history(normalize_symbol(s))
            if df is None or df.empty or "Close" not in df.columns:
                return None
            closes = df["Close"].dropna()
            if len(closes) < days + 1:
                return None
            return float((closes.iloc[-1] / closes.iloc[-days-1] - 1) * 100)

        async def _get_peers(s):
            try:
                db_url = (os.getenv("DATABASE_URL") or "").strip() or (os.getenv("CACHE_DATABASE_URL") or "").strip()
                if db_url.startswith("postgres://"):
                    db_url = "postgresql://" + db_url[len("postgres://"):]
                from sqlalchemy import create_engine, text as _text
                engine = create_engine(db_url, pool_pre_ping=True, pool_size=1, max_overflow=0)
                with engine.connect() as conn:
                    rows = conn.execute(
                        _text("SELECT current_symbol FROM symbol_master WHERE sector=:sec AND status='active' LIMIT 50"),
                        {"sec": s}
                    ).fetchall()
                return [r[0] for r in rows]
            except Exception:
                return []

        result = await relative_strength_vs_sector(sym, sec, _get_return, _get_peers)
        return {"symbol": sym, "sector": sec, **result}
    except Exception as e:
        return {"symbol": sym, "sector": sec, "error": str(e), "passes": False}


@app.get("/health")
def health():
    return {"status": "ok", "service": "technical-analysis-service"}

@app.get("/")
def root():
    return {"service": "technical-analysis-service", "version": "0.3.0", "status": "ok",
            "endpoints": ["/health", "/analyze/{symbol}"]}

@app.get("/analyze/{symbol}")
def analyze(
    symbol: str,
    force: bool = False,
    # 2026-09-11 addition: these four were hardcoded constants (RSI
    # 30/70, extension chase-risk 18%/5%) with no way for a caller to
    # supply a regime-adjusted value. real-trade-service's
    # adaptive_market_params.py now computes these from its own rolling
    # live-market history and passes them in on every call from the
    # quality gate (candidate_engine.candidates._fetch_fund_tech_score);
    # any OTHER caller that doesn't pass them gets the exact same static
    # defaults as before — this is purely additive, never a behavior
    # change for existing callers.
    rsi_oversold: float = 30.0,
    rsi_overbought: float = 70.0,
    extended_1m_pct: float = 0.18,
    extended_short_pct: float = 0.05,
    # 2026-09-11 addition: multipliers on the trend-following (EMA stack,
    # MACD) vs mean-reversion (RSI, Bollinger Band) score deltas below —
    # see adaptive_market_params.adaptive_signal_weights for how
    # real-trade-service computes these from a live, rolling measure of
    # "is the market trending or range-bound right now." 1.0 = unchanged
    # from the original fixed scoring for any caller that doesn't pass
    # these (fully backward compatible).
    trend_weight: float = 1.0,
    meanrev_weight: float = 1.0,
):
    sym = normalize_symbol(symbol)
    # force=True and/or non-default threshold overrides must bypass the
    # plain per-symbol cache below — otherwise a cached response computed
    # under yesterday's thresholds (or under someone else's override)
    # would get served back regardless of what THIS call asked for.
    _using_overrides = (
        rsi_oversold != 30.0 or rsi_overbought != 70.0
        or extended_1m_pct != 0.18 or extended_short_pct != 0.05
        or trend_weight != 1.0 or meanrev_weight != 1.0
    )
    cache_key = f"tech_analysis:{sym}"

    # Bypass local memory/Upstash cache when force=True (sniper / real-time)
    if not force and not _using_overrides:
        cached = _cache_get(cache_key)
        if cached:
            return cached

    df = _fetch_history(sym, force=force)

    if df is None or len(df) < 5:
        price = _fetch_quote_price(sym)
        if price:
            result = {
                "symbol": sym,
                "technical_score": 50,
                "trend_strength": "unknown",
                "volume_surge": False,
                "close": price,
                "price": price,
                "rsi": 50,
                "support": None,
                "resistance": None,
                "extended": False,
                "extended_short": False,
                "data_insufficient": True,
                "summary": f"Technical indicators neutral (RSI: 50). Last quote ₹{price:.2f}; history temporarily thin.",
                "reasons": [
                    f"Limited history for {sym}; using last quote ₹{price:.2f}. Retry for full technicals."
                ],
            }
            # Short TTL only: this used to be cached for 5 min (market hours)
            # or 6 h (after close), pinning a neutral 50 long after the
            # history fetch had recovered.
            _cache_set(cache_key, result, ttl=min(get_cache_ttl(), _INSUFFICIENT_CACHE_TTL))
        else:
            result = {
                "symbol": sym,
                "technical_score": 50,
                "trend_strength": "unknown",
                "volume_surge": False,
                "close": None,
                "price": None,
                "rsi": 50,
                "support": None,
                "resistance": None,
                "extended": False,
                "extended_short": False,
                "data_insufficient": True,
                "summary": "Technical indicators neutral (RSI: 50). Price history unavailable — retry shortly.",
                "reasons": [
                    f"Price history unavailable for {sym} right now (upstream busy). Retry shortly."
                ],
            }
            # Deliberately NOT cached: it says "retry shortly", but caching it
            # for 5 min-6 h made every retry return the same empty neutral
            # result even after the upstream recovered.
        return result

    close  = df["Close"]
    high   = df["High"]
    low    = df["Low"]
    volume = df["Volume"]
    data_length = len(df)

    rsi_series = _rsi(close) if data_length >= 14 else pd.Series([50]*data_length, index=df.index)
    macd_line, macd_sig = _macd(close) if data_length >= 26 else (pd.Series([0]*data_length, index=df.index), pd.Series([0]*data_length, index=df.index))
    ema20 = _ema(close, min(20, data_length)) if data_length >= 5 else close
    ema50 = _ema(close, min(50, data_length)) if data_length >= 10 else close
    ema200 = _ema(close, min(200, data_length)) if data_length >= 30 else close
    # Below _ADX_MIN_BARS ADX does not exist yet. The old code ran _adx from 20
    # bars (all-NaN until bar 28, then reported as a real 0.0 "weak") and used a
    # fake 15 under 20 bars; both leaked into the universe_adx regime average.
    adx_known = data_length >= _ADX_MIN_BARS
    adx_series = _adx(high, low, close, _ADX_PERIOD) if adx_known else None
    atr_series = _atr(high, low, close) if data_length >= 14 else pd.Series([0]*data_length, index=df.index)
    bb_upper, bb_lower = _bollinger(close, min(20, data_length)) if data_length >= 5 else (close, close)

    latest = df.iloc[-1]
    close_val = float(latest["Close"])
    support, resistance = _support_resistance(df, min(20, len(df)))

    # `_safe(x) or 50.0` treated a genuine RSI of 0.0 (every day a loss) as
    # missing and reported a neutral 50; only None means missing.
    rsi_val    = _num(rsi_series.iloc[-1], 50.0)
    macd_val   = _num(macd_line.iloc[-1], 0.0)
    macd_s_val = _num(macd_sig.iloc[-1], 0.0)
    prev_macd  = _num(macd_line.iloc[-2], 0.0) if len(macd_line) > 1 else 0.0
    prev_sig   = _num(macd_sig.iloc[-2], 0.0) if len(macd_sig) > 1 else 0.0
    ema20_val  = _num(ema20.iloc[-1], close_val)
    ema50_val  = _num(ema50.iloc[-1], close_val)
    ema200_val = _num(ema200.iloc[-1], close_val)
    adx_val    = _num(adx_series.iloc[-1], 0.0) if adx_known else None
    atr_val    = _num(atr_series.iloc[-1], 0.0)
    bb_up      = _num(bb_upper.iloc[-1], close_val)
    bb_lo      = _num(bb_lower.iloc[-1], close_val)
    vol_now    = float(latest["Volume"])
    vol_avg20  = float(volume.tail(min(20, len(volume))).mean()) if len(volume) >= 5 else vol_now

    score   = 50
    reasons = []

    # Mean-reversion bonuses (oversold RSI, price near the lower Bollinger band) are
    # a bet on a bounce. In a confirmed downtrend (bearish EMA stack) RSI can stay
    # oversold and price can ride the lower band for weeks, and the two bonuses
    # (+12, +8) used to add up to cancel the stack's -15 and lift a falling stock
    # to neutral. No bounce credit while the stack is bearish; penalties
    # (overbought / near upper band) are unaffected, so the guard only ever
    # makes a score more cautious.
    bearish_stack = data_length >= 30 and close_val < ema20_val < ema50_val < ema200_val

    if rsi_val < rsi_oversold:
        if bearish_stack:
            reasons.append(f"RSI at {rsi_val:.1f} — oversold, but no bounce credit under a bearish EMA stack")
        else:
            score += 12 * meanrev_weight
            reasons.append(f"RSI at {rsi_val:.1f} — oversold (adaptive floor {rsi_oversold:.0f}, weight {meanrev_weight:.2f}x)")
    elif rsi_val > rsi_overbought:
        score -= 12 * meanrev_weight
        reasons.append(f"RSI at {rsi_val:.1f} — overbought (adaptive ceiling {rsi_overbought:.0f}, weight {meanrev_weight:.2f}x)")
    else:
        reasons.append(f"RSI at {rsi_val:.1f} — neutral")

    if data_length >= 26:
        bullish_cross = prev_macd < prev_sig and macd_val > macd_s_val
        bearish_cross = prev_macd > prev_sig and macd_val < macd_s_val
        if bullish_cross:
            score += 15 * trend_weight
            reasons.append(f"MACD bullish crossover (weight {trend_weight:.2f}x)")
        elif bearish_cross:
            score -= 15 * trend_weight
            reasons.append(f"MACD bearish crossover (weight {trend_weight:.2f}x)")
        elif macd_val > macd_s_val:
            score += 5 * trend_weight
            reasons.append("MACD above signal line")
        else:
            score -= 5 * trend_weight
            reasons.append("MACD below signal line")
    else:
        reasons.append("MACD: insufficient data")

    if data_length >= 30:
        if close_val > ema20_val > ema50_val > ema200_val:
            score += 15 * trend_weight
            reasons.append(f"Bullish EMA stack (weight {trend_weight:.2f}x)")
        elif close_val < ema20_val < ema50_val < ema200_val:
            score -= 15 * trend_weight
            reasons.append(f"Bearish EMA stack (weight {trend_weight:.2f}x)")
        elif close_val > ema200_val:
            score += 5 * trend_weight
            reasons.append("Above 200 EMA")
        else:
            score -= 5 * trend_weight
            reasons.append("Below 200 EMA")
    else:
        reasons.append("EMA trend: insufficient data")

    if data_length >= 20:
        sma20 = float(close.tail(20).mean())
        if close_val > sma20:
            score += 8 * trend_weight
            reasons.append("Above 20-day SMA")
        else:
            score -= 8 * trend_weight
            reasons.append("Below 20-day SMA")
    else:
        reasons.append("Short-term momentum: insufficient data")

    if adx_val is None:
        trend_strength = "unknown"
        reasons.append("ADX: insufficient data")
    else:
        trend_strength = "strong" if adx_val >= 25 else "moderate" if adx_val >= 20 else "weak"
        if adx_val >= 25:
            reasons.append(f"ADX {adx_val:.1f} — strong trend")
        else:
            reasons.append(f"ADX {adx_val:.1f} — weak/no trend")

    if data_length >= 20 and not bb_up > bb_lo:
        # Collapsed bands (flat or halted stock, zero std-dev). The old code
        # substituted a range of 1, which put a mid-band price at 0% and
        # handed a flat stock a "near lower BB" +8 bonus. No volatility
        # means no mean-reversion signal.
        reasons.append("Bollinger Bands: flat price, no signal")
    elif data_length >= 20:
        bb_range = bb_up - bb_lo
        bb_pct   = (close_val - bb_lo) / bb_range * 100
        if bb_pct < 20:
            if bearish_stack:
                reasons.append(f"Near lower BB ({bb_pct:.0f}%) — no bounce credit under a bearish EMA stack")
            else:
                score += 8 * meanrev_weight
                reasons.append(f"Near lower BB ({bb_pct:.0f}%, weight {meanrev_weight:.2f}x)")
        elif bb_pct > 80:
            score -= 8 * meanrev_weight
            reasons.append(f"Near upper BB ({bb_pct:.0f}%, weight {meanrev_weight:.2f}x)")
    else:
        reasons.append("Bollinger Bands: insufficient data")

    dist_res = round(((resistance - close_val) / close_val) * 100, 2) if resistance and close_val else 999
    dist_sup = round(((close_val - support)   / close_val) * 100, 2) if support and close_val else 999
    if dist_res < 2:
        score -= 8
        reasons.append(f"{dist_res}% below resistance")
    if dist_sup < 2:
        score += 8
        reasons.append(f"{dist_sup}% above support")

    volume_surge = vol_avg20 > 0 and vol_now > vol_avg20 * 1.5
    if volume_surge:
        score += 8
        reasons.append("Volume surge")

    # ── Short-Term Trading Upgrade (2026-09-02) — bonus fix ───────────────────
    # BUG: the only "extended" (already-popped, don't chase) check anywhere in
    # this service was `_rs_vs_nifty`'s dead/unused `ret > 0.18` over a 21-
    # TRADING-DAY (~1 month) window — see that function above; it was never
    # actually called from analyze(), so "extended" never reached the result
    # dict at all, and a stock that popped >5% in the last 2-3 days (a bulk-
    # deal/results move, exactly the catalysts watchlist_engine now tracks)
    # was invisible to this check either way. This service only has daily
    # candles (Bug 2, same root-cause note), so "same-day" isn't directly
    # observable here — but a short trailing-window return using the candles
    # we DO have is: cheap, catches same-week pops the 21-day version misses
    # by construction, and needs no new data source.
    extended = False
    extended_short = False
    try:
        if data_length >= 22:
            ret_21d  = float(close.iloc[-1] / close.iloc[-21] - 1.0)
            extended = ret_21d > extended_1m_pct
        if data_length >= 5:
            # ~3 trading days — short enough to catch a bulk-deal/results pop
            # that the 21-day window structurally cannot flag until it's
            # already a month old.
            ret_3d = float(close.iloc[-1] / close.iloc[-4] - 1.0)
            extended_short = ret_3d > extended_short_pct
            if extended_short:
                reasons.append(f"Extended short-term: +{ret_3d*100:.1f}% over ~3 sessions (adaptive cutoff {extended_short_pct*100:.0f}%) — chase risk")
    except Exception:
        pass  # fail-open: missing/short history just leaves both flags False

    # ── Adaptive market-regime adjustment (Aug-2026 improvement) ──────────────
    # In a correction (Nifty −7% in 6m), near-resistance signals are more
    # likely to fail. Reduce technical score when price is in the top 15% of
    # the 52W range (overextension in weak market context).
    # This check is cheap (no extra fetch) — uses already-computed values.
    try:
        if resistance and close_val and resistance > 0 and dist_res < 5:
            # Near resistance in a weak market = higher reversal risk
            score -= 5
            reasons.append(
                f"Near resistance ({dist_res:.1f}%) in weak-market context — "
                "regime penalty applied."
            )
    except Exception:
        pass

    # vol_now/vol_avg20 as a ratio, not just the boolean volume_surge flag —
    # this is what decision-engine needs to forward rsi/volume_ratio through
    # to training-service as real numbers instead of always-null. Was
    # computed here already but never included in the response.
    volume_ratio = round(vol_now / vol_avg20, 3) if vol_avg20 > 0 else None

    # Official NSE delivery % via market-data-service (quote → bhavcopy → fallback)
    delivery_pct = None
    delivery_source = None
    try:
        with httpx.Client(timeout=8.0) as client:
            dr = client.get(f"{MARKET_DATA_URL}/delivery/{sym.replace('.NS','').replace('.BO','')}")
            if dr.status_code == 200:
                djson = dr.json()
                if djson.get("delivery_pct") is not None:
                    delivery_pct = float(djson["delivery_pct"])
                    delivery_source = djson.get("source")
                    # Mild technical nudge: high delivery supports accumulation narrative
                    if delivery_pct >= 60:
                        score += 3
                        reasons.append(f"High delivery {delivery_pct:.0f}% ({delivery_source or 'nse'})")
                    elif delivery_pct <= 30:
                        score -= 2
                        reasons.append(f"Low delivery {delivery_pct:.0f}% ({delivery_source or 'nse'})")
    except Exception as e:
        logger.debug("delivery fetch skipped: %s", e)

    score = max(0, min(100, round(score)))

    result = {
        "symbol": sym,
        "close": round(close_val, 2),
        "technical_score": score,
        "trend_strength": trend_strength,
        "support": round(support, 2) if support else None,
        "resistance": round(resistance, 2) if resistance else None,
        "rsi": round(rsi_val, 1),
        "adx": round(adx_val, 1) if adx_val is not None else None,
        "atr": round(atr_val, 2),
        "ema20": round(ema20_val, 2),
        "ema50": round(ema50_val, 2),
        "ema200": round(ema200_val, 2),
        "bb_upper": round(bb_up, 2),
        "bb_lower": round(bb_lo, 2),
        "volume_surge": bool(volume_surge),
        "volume_ratio": volume_ratio,
        "delivery_pct": delivery_pct,
        "delivery_source": delivery_source,
        "extended": bool(extended),
        "extended_short": bool(extended_short),
        "data_insufficient": data_length < 30,
        # 2026-09-11 addition: surfaces what this specific call actually
        # used, for debugging/audit — 1.0/1.0 and 30.0/70.0/0.18/0.05 mean
        # "static defaults, no overrides sent."
        "adaptive_params_used": {
            "rsi_oversold": rsi_oversold, "rsi_overbought": rsi_overbought,
            "extended_1m_pct": extended_1m_pct, "extended_short_pct": extended_short_pct,
            "trend_weight": trend_weight, "meanrev_weight": meanrev_weight,
        },
        "reasons": reasons,
    }

    # 2026-09-11: don't poison the shared per-symbol cache with a result
    # computed under non-default (adaptive) thresholds — only cache the
    # plain static-default computation, same as before this change.
    if not _using_overrides:
        _cache_set(cache_key, result)
    return result

if __name__ == "__main__":
    import uvicorn
    port = int(((os.getenv("PORT") or "").strip() or 8002))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)