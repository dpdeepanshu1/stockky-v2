"""
surprise_scanner.py — Intraday Surprise Stock engine.

MARKET INTELLIGENCE APPLIED (Aug-2026):
════════════════════════════════════════
Nifty at 24,090, −7% in 6m, FII net-short. Midcap/Smallcap outperforming.
In a weak-index, choppy market:
  - Volume confirmation is MORE important than price signal alone (FII short
    means price moves can be engineered; volume is harder to fake).
  - Order-book imbalance (buy_pct) is now a TIER-1 check, not optional.
  - 52W proximity check is now HARDER: stock must be within 8% of 52W high
    (tightened from 12%) — in a falling index, 52W-high proximity = genuine
    relative strength, not just a laggard's dead-cat bounce.
  - MIN_SCORE raised 60 → 65: only surface genuine breakouts, not noise.
  - MIN_CHANGE_PCT raised 1.0 → 1.5: 1% moves in a choppy market are noise.
  - BUILDING tier: RVOL_SLOPE_MIN raised 0.4 → 0.6 — need stronger
    acceleration before flagging as "building" (choppy market = more false
    building signals).
  - Score formula rebalanced: Volume (35) + Order-book (15) + ORB/VWAP (25) +
    52W (15) + Range (10). Order-book promoted from 10 to 15; range
    demoted from 15 to 10. Buy-side pressure is the single most reliable
    intraday signal in the current FII-short environment.

All original structural code (DB load, sector sympathy, caching, repair) is
unchanged — only the scoring thresholds and weights are updated.

BUGFIX (31-Aug-2026): vwap/orb_high were defaulting to values that made the
25-pt ORB/VWAP bucket structurally unreachable on every call (see the
BUGFIX comment inside score_stock() for the full proof). Both now fall back
to honest same-session proxies instead of values that trivially equal or
bound current_price.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("surprise-scanner")

# ── Thresholds (market-intelligence derived, all env-overridable) ─────────────
# Raised from 60 → 65: only genuine breakouts in choppy/weak-index market.
MIN_SCORE = int(os.getenv("SURPRISE_MIN_SCORE", "65"))
# §5 — hard floor: average daily traded value (price × vol × ~25 intraday bars)
# ₹50 lakh/day minimum. Never adaptive — unconditional BUY-side gate.
HARD_FLOOR_LIQUIDITY = float(os.getenv("HARD_FLOOR_LIQUIDITY", "5000000"))
# Raised from 1.0 → 1.5: 1% moves in a choppy market are noise.
MIN_CHANGE_PCT = float(os.getenv("SURPRISE_MIN_CHANGE_PCT", "1.5"))
CONCURRENCY = int(os.getenv("SURPRISE_SCAN_CONCURRENCY", "20"))
QUOTE_TIMEOUT = float(os.getenv("SURPRISE_QUOTE_TIMEOUT", "3"))
# 2026-09-03 fix: default cached_max_age_sec (below, in SurpriseStockEngine.scan)
# was 90s while real-trade-service's pipeline cycle (AUTO_PILOT_INTERVAL_SECONDS,
# config.py) defaults to 180s. That mismatch meant candidate_engine's
# `cached=true` call almost always found the cache already stale and fell
# through to a full live scan of the whole liquid static universe — which is
# what produced the recurring "surprise/scan ... ReadTimeout" in the logs
# (candidate_engine's own client-side timeout on that call is 25s). Widened
# so one full-universe scan reliably covers a full pipeline cycle with margin.
SURPRISE_CACHE_MAX_AGE_SEC = float(os.getenv("SURPRISE_CACHE_MAX_AGE_SEC", "220"))
MAX_STOCK_PRICE = float(os.getenv("MAX_STOCK_PRICE", "0") or 0)
# Value-buy badge: ₹20–₹500 (same as buy_sniper — midcaps/smallcaps outperforming)
VALUE_BUY_THRESHOLD = float(os.getenv("VALUE_BUY_THRESHOLD", "500") or 500)

# Building tier
BUILDING_MIN_SCORE = int(os.getenv("SURPRISE_BUILDING_MIN_SCORE", "35"))
BUILDING_MAX_SCORE = MIN_SCORE
BUILDING_MIN_CHANGE_PCT = float(os.getenv("SURPRISE_BUILDING_MIN_CHANGE_PCT", "0.3"))
# Raised from 0.4 → 0.6: need stronger volume acceleration in choppy market
RVOL_SLOPE_MIN = float(os.getenv("SURPRISE_RVOL_SLOPE_MIN", "0.6"))
SECTOR_SYMPATHY_MIN_SCORE = int(os.getenv("SURPRISE_SECTOR_SYMPATHY_MIN_SCORE", "45"))

# 52W proximity gate: within 8% of 52W high = genuine relative strength.
# Tightened from 12% — in a falling index, only near-52W-high stocks are
# showing real strength independent of the market.
DIST_52W_BREAKOUT_PCT = float(os.getenv("SURPRISE_DIST_52W_BREAKOUT_PCT", "8.0"))
DIST_52W_NEAR_PCT = float(os.getenv("SURPRISE_DIST_52W_NEAR_PCT", "15.0"))

# ── Volume-Shocker override (added 31-Aug-2026) ───────────────────────────────
# BUG: score_stock() requires MIN_SCORE=65/100 for the "breakout" tier, but
# up to 30 of those points (order-book buy_pct=15 + 52W-proximity=15) are
# structurally unreachable for most of the universe: buy_pct defaults to a
# neutral 50 (0 pts) whenever the feed has no live bid/ask depth (true for
# nearly every quote-only / EOD-derived tick), and the majority of genuine
# single-day "volume shocker" pops (event/news driven small & midcaps) are
# NOT trading near their 52-week high. Backtesting real >5% + high-RVOL move
# days for stocks that Groww's own "Volume shockers" screen surfaced showed
# most of them scoring 40-62 ("building") instead of "breakout" — i.e. a
# confirmed, high-volume 5%+ mover was being demoted to a soft/secondary
# signal purely because of two inapplicable sub-scores, not because the
# move itself was weak. A large price move CONFIRMED by real volume is
# exactly what "Volume shockers" means, independent of 52W distance or
# order-book depth — so it must be able to reach "breakout" on its own.
SHOCKER_MIN_CHANGE_PCT = float(os.getenv("SURPRISE_SHOCKER_MIN_CHANGE_PCT", "5.0"))
SHOCKER_MIN_RVOL = float(os.getenv("SURPRISE_SHOCKER_MIN_RVOL", "2.0"))

# ── ORB-proxy tuning (added 31-Aug-2026, see BUGFIX note in score_stock) ─────
# Fraction of a stock's normal daily ATR used as the "opening range" buffer
# above the open when the feed has no true opening-range high.
ORB_ATR_FRACTION = float(os.getenv("SURPRISE_ORB_ATR_FRACTION", "0.3"))
# Fallback buffer (as a fraction of open price) when daily_atr is unknown/0.
ORB_FALLBACK_PCT = float(os.getenv("SURPRISE_ORB_FALLBACK_PCT", "0.005"))


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"): ]
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    url = re.sub(r"(?i)([?&]sslmode=)required\b", r"\1require", url)
    if "sslmode=" not in url.lower():
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


try:
    import surprise_schema as _ss
except Exception:
    _ss = None


def _dialect() -> str:
    if _ss is not None:
        try:
            return _ss.dialect()
        except Exception:
            pass
    return "oracle" if os.environ.get("ORACLE_DSN") else "postgresql"


def _conn_dialect(conn) -> str:
    try:
        return (conn.dialect.name or "").lower()
    except Exception:
        return _dialect()


def _db_url() -> Optional[str]:
    if _ss is not None:
        try:
            return _ss.database_url()
        except Exception as e:
            logger.debug("surprise_schema.database_url: %s", e)
    url = (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
    )
    if url and url.lower().startswith("oracle"):
        return None
    return _normalize_db_url(url) if url else None


def _engine(app_name: str):
    # Prefer the process-wide cached pool so repeated scans reuse one warm
    # engine instead of opening (and sometimes leaking) a fresh pool each time.
    # Callers must NOT dispose the returned engine — it is shared.
    if _ss is not None:
        try:
            if hasattr(_ss, "shared_engine"):
                return _ss.shared_engine(app_name)
            return _ss.make_engine(app_name)
        except Exception as e:
            logger.debug("surprise_schema engine: %s", e)
    url = _db_url()
    if not url:
        return None
    from sqlalchemy import create_engine
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=1,
        pool_timeout=8,
        connect_args={"connect_timeout": 8, "application_name": app_name},
    )



def dedupe_by_symbol(candidates: list) -> list:
    """
    §9 — Deduplicate candidates by symbol before scoring/ranking.
    Tiebreaker: higher score wins; if equal, more recent updated_at wins.
    A paginated scan can return the same symbol twice across pages —
    dedupe must happen BEFORE scoring or a stock gets double-weighted.
    """
    seen = {}
    for c in candidates:
        sym = c.get("symbol")
        if not sym:
            continue
        existing = seen.get(sym)
        if existing is None:
            seen[sym] = c
        else:
            # Prefer higher score; fall back to updated_at as tiebreaker
            c_score = c.get("score") or 0
            e_score = existing.get("score") or 0
            if c_score > e_score:
                seen[sym] = c
            elif c_score == e_score:
                try:
                    if str(c.get("updated_at", "")) > str(existing.get("updated_at", "")):
                        seen[sym] = c
                except Exception:
                    pass
    return list(seen.values())


def directional_filter(candidates: list) -> tuple:
    """§9 — Split candidates into (positive movers, flat/negative movers)."""
    pos = [c for c in candidates if (c.get("pct_change") or c.get("change_pct") or 0) > 0]
    neg = [c for c in candidates if (c.get("pct_change") or c.get("change_pct") or 0) <= 0]
    return pos, neg


def _session_progress_ist() -> float:
    """
    BUGFIX (31-Aug-2026): fraction of the NSE trading session (9:15-15:30
    IST, 375 minutes) elapsed right now — used as the fallback for
    tick["session_progress"], which (confirmed via repo-wide grep, same as
    vwap/orb_high) is never actually supplied by any tick source.

    The old code hardcoded this fallback to a flat 0.4, i.e. it always
    pretended it was ~40% through the session (~11:35am) no matter the
    actual time. That value feeds directly into current_vol_15m via
    `current_vol_15m = daily_volume / (25 * progress)`, which in turn drives
    rvol — the single biggest scoring bucket (35 pts) plus rvol_slope
    (10 pts). Concretely: at 9:35am (progress really ~0.05) the hardcoded
    0.4 divides daily volume by 10 instead of ~1.25, understating rvol ~8x
    right when volume bursts are most meaningful; at 3:15pm (progress really
    ~0.96) it divides by 10 instead of ~24, overstating rvol ~2.4x late in
    the day. Every scan, all day, was silently mis-scoring on the clock.

    Returns a value clamped to [0.15, 1.0] to match the existing bounds
    applied at the call site. Falls back to the old 0.4 constant only if
    the time lookup itself fails (e.g. tzdata unavailable).
    """
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, time as dtime
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        open_t, close_t = dtime(9, 15), dtime(15, 30)
        tt = now.time()
        if tt <= open_t:
            return 0.15
        if tt >= close_t:
            return 1.0
        elapsed_min = (now.hour * 60 + now.minute) - (open_t.hour * 60 + open_t.minute)
        return max(0.15, min(elapsed_min / 375.0, 1.0))
    except Exception:
        return 0.4


class SurpriseStockEngine:
    def __init__(self):
        self.static_cache: Dict[str, Dict[str, Any]] = {}
        self._loaded_at: float = 0.0
        self.semaphore = asyncio.Semaphore(max(4, min(CONCURRENCY, 20)))
        self._last_rvol: Dict[str, float] = {}
        self._last_scan_ts: float = 0.0
        self._last_result: Optional[Dict[str, Any]] = None

    def load_static_cache(self, force: bool = False) -> int:
        if self.static_cache and not force and (time.time() - self._loaded_at) < 300:
            return len(self.static_cache)
        url = _db_url()
        if not url:
            logger.warning("surprise: no DATABASE_URL — static cache empty")
            self.static_cache = {}
            return 0
        try:
            try:
                from surprise_schema import ensure_surprise_schema
                ensure_surprise_schema()
            except Exception as se:
                logger.debug("ensure_surprise_schema: %s", se)

            from sqlalchemy import text
            eng = _engine("stockky-surprise-scan")
            if eng is None:
                self.static_cache = {}
                return 0
            with eng.begin() as conn:
                if _conn_dialect(conn) != "oracle":
                    conn.execute(text("""
                        CREATE TABLE IF NOT EXISTS surprise_static_feed (
                            symbol VARCHAR(30) PRIMARY KEY,
                            prev_close NUMERIC(12, 2) NOT NULL DEFAULT 0,
                            avg_15m_volume BIGINT NOT NULL DEFAULT 10000,
                            daily_atr NUMERIC(12, 2) NOT NULL DEFAULT 0.0,
                            high_52w NUMERIC(12, 2) NOT NULL DEFAULT 0,
                            dist_52w_pct NUMERIC(8, 2) NOT NULL DEFAULT 100,
                            sector VARCHAR(80),
                            is_liquid BOOLEAN DEFAULT TRUE,
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        )
                    """))
                rows = conn.execute(text(
                    "SELECT symbol, prev_close, avg_15m_volume, daily_atr, "
                    "high_52w, dist_52w_pct, sector, is_liquid, updated_at "
                    "FROM surprise_static_feed"
                )).mappings().all()
            # NOTE: engine is process-wide shared now (surprise_schema.shared_engine)
            # — do NOT dispose it here or every scan throws away the warm pool.
            cache: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                d = dict(r)
                sym = str(d.get("symbol") or "").upper().strip()
                if not sym:
                    continue
                for k in ("prev_close", "daily_atr", "high_52w", "dist_52w_pct"):
                    try:
                        d[k] = float(d[k]) if d.get(k) is not None else 0.0
                    except Exception:
                        d[k] = 0.0
                if "is_liquid" in d and not isinstance(d.get("is_liquid"), bool):
                    try:
                        d["is_liquid"] = bool(int(d["is_liquid"]))
                    except Exception:
                        d["is_liquid"] = True
                try:
                    vol = d.get("avg_15m_volume") or d.get("avg_volume")
                    d["avg_15m_volume"] = int(vol or 10000)
                except Exception:
                    d["avg_15m_volume"] = 10000
                cache[sym] = d
            self.static_cache = cache
            self._loaded_at = time.time()
            logger.info("surprise static cache loaded: %s symbols", len(cache))
            if len(cache) < 20:
                seeded = self._seed_from_data_feed_kv(cache)
                if seeded:
                    self.static_cache = cache
                    logger.info("surprise: seeded %s symbols from stockky_kv", seeded)
            return len(self.static_cache)
        except Exception as e:
            logger.warning("load_static_cache failed: %s", e)
            try:
                cache = dict(self.static_cache or {})
                seeded = self._seed_from_data_feed_kv(cache)
                if seeded:
                    self.static_cache = cache
                    self._loaded_at = time.time()
            except Exception as e2:
                logger.debug("surprise kv seed failed: %s", e2)
            return len(self.static_cache)

    def _seed_from_data_feed_kv(self, cache: Dict[str, Dict[str, Any]]) -> int:
        url = _db_url()
        if not url:
            return 0
        try:
            from sqlalchemy import text
            import json as _json
            eng = _engine("stockky-surprise-kv")
            if eng is None:
                return 0
            added = 0
            with eng.connect() as conn:
                _cap = (
                    "FETCH FIRST 800 ROWS ONLY"
                    if _conn_dialect(conn) == "oracle"
                    else "LIMIT 800"
                )
                rows = conn.execute(text(f"""
                    SELECT k, v FROM stockky_kv
                    WHERE (k LIKE 'stockky:data_feed:%'
                           OR k LIKE 'feed:%'
                           OR (k NOT LIKE 'system:%' AND k NOT LIKE 'stockky:%'))
                      AND k NOT LIKE '%:index'
                      AND k NOT LIKE '%:meta'
                      AND k NOT LIKE '%:job'
                    {_cap}
                """)).fetchall()
                for row in rows:
                    k = str(row[0] or "")
                    raw = row[1]
                    sym = k
                    for prefix in ("stockky:data_feed:", "feed:", "data_feed:"):
                        if sym.startswith(prefix):
                            sym = sym[len(prefix):]
                            break
                    sym = sym.upper().replace(".NS", "").replace(".BO", "").strip()
                    if not sym or sym in cache:
                        continue
                    try:
                        data = raw if isinstance(raw, dict) else _json.loads(raw) if isinstance(raw, str) else {}
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
                    try:
                        price = float(payload.get("price") or payload.get("cmp") or 0)
                    except Exception:
                        price = 0.0
                    if price <= 0 or (MAX_STOCK_PRICE > 0 and price > MAX_STOCK_PRICE):
                        continue
                    try:
                        prev = float(payload.get("previous_close") or price)
                    except Exception:
                        prev = price
                    try:
                        vol = int(payload.get("volume") or 10000)
                    except Exception:
                        vol = 10000
                    cache[sym] = {
                        "symbol": sym,
                        "prev_close": prev,
                        "avg_15m_volume": max(1000, vol // 20),
                        "daily_atr": abs(price - prev) or (price * 0.015),
                        "high_52w": float(payload.get("day_high") or price),
                        "dist_52w_pct": 5.0,
                        "sector": payload.get("sector") or "",
                        "is_liquid": True,
                        "source": "data_feed_kv",
                    }
                    added += 1
            return added
        except Exception as e:
            logger.debug("_seed_from_data_feed_kv: %s", e)
            return 0

    def score_stock(self, symbol: str, tick: dict) -> Optional[dict]:
        static = self.static_cache.get(symbol.upper().replace(".NS", "").replace(".BO", ""))
        if not static:
            return None

        try:
            current_price = float(
                tick.get("price") or tick.get("last_price") or tick.get("ltp")
                or tick.get("close") or tick.get("cmp") or 0.0
            )
        except Exception:
            current_price = 0.0

        prev_close = float(static.get("prev_close") or 0.0)
        if current_price <= 0 and prev_close > 0:
            current_price = prev_close
            tick = dict(tick or {})
            tick["_price_from_baseline"] = True
        if current_price <= 0:
            return None
        if prev_close <= 0:
            prev_close = current_price

        open_price       = float(tick.get("open") or prev_close)
        current_vol_15m  = float(tick.get("vol_15m") or tick.get("volume") or 0.0)
        if current_vol_15m > 0 and tick.get("vol_15m") is None:
            # BUGFIX (31-Aug-2026): session_progress is never supplied by any
            # tick source either — was hardcoded to 0.4 regardless of actual
            # clock time (see _session_progress_ist() docstring for the full
            # impact on rvol). Use the real elapsed-session fraction instead.
            sp_raw = tick.get("session_progress")
            progress = float(sp_raw) if sp_raw is not None else _session_progress_ist()
            progress = max(0.15, min(progress, 1.0))
            current_vol_15m = current_vol_15m / max(1.0, 25.0 * progress)

        day_high  = float(tick.get("high") or current_price)
        day_low   = float(tick.get("low") or current_price)

        # ── BUGFIX (31-Aug-2026): dead ORB/VWAP bucket ───────────────────────
        # Confirmed across every tick source (/quote/{symbol}, the bulk quote
        # cache in data_feed.py, the AngelOne WS feed, and the Yahoo WS feed):
        # NONE of them ever populate "vwap" or "orb_high". grep across the
        # whole repo turns up zero writers for either key — tick.get() always
        # returns None for both, on every single call, not as an edge case.
        #
        # The old code then defaulted:
        #   vwap     = tick.get("vwap") or current_price       -> == current_price
        #   orb_high = tick.get("orb_high") or tick.get("high") -> == day_high
        # `day_high` (tick["high"]) is the running session high computed from
        # the SAME quote as current_price, so day_high >= current_price is a
        # mathematical identity for any valid OHLC quote. That means both
        #   current_price > vwap        (vwap == current_price)
        #   current_price > orb_high    (orb_high == day_high >= current_price)
        # are structurally impossible to satisfy — not rare misses, guaranteed
        # False on every call — which permanently zeroed the entire 25-pt
        # ORB/VWAP bucket (score's single biggest category) for 100% of
        # scans, silently capping every stock ~25 points below its true score
        # and starving both the "breakout" tier and the "building" tier (which
        # has no override) of stocks that were genuinely trading strong.
        #
        # Fix: when the real intraday field isn't supplied, derive a proxy
        # that is deliberately NOT equal to (or trivially bounded by)
        # current_price, so the comparison stays meaningful:
        #   - vwap proxy:     same-session typical price = (open+high+low)/3.
        #     Unlike current_price itself, this only matches current_price by
        #     coincidence, so "price above the session's typical price" is a
        #     real, passable condition again.
        #   - orb_high proxy: open + a fraction of the stock's own normal
        #     daily range (daily_atr, already in static_cache). This models
        #     "opening-range breakout" as "moved beyond a normal opening swing
        #     from the open" — the closest honest substitute available when
        #     the feed has no true first-15/30-min high. Falls back to a
        #     small fixed % above open on illiquid names with atr=0.
        vwap_raw = tick.get("vwap")
        vwap = float(vwap_raw) if vwap_raw is not None else (open_price + day_high + day_low) / 3.0

        orb_raw = tick.get("orb_high")
        if orb_raw is not None:
            orb_high = float(orb_raw)
        else:
            daily_atr_static = float(static.get("daily_atr") or 0.0)
            orb_buffer = (daily_atr_static * ORB_ATR_FRACTION) if daily_atr_static > 0 else (open_price * ORB_FALLBACK_PCT)
            orb_high = open_price + orb_buffer

        avg_15m_vol   = max(int(static.get("avg_15m_volume") or 1), 1)
        rvol          = round(current_vol_15m / avg_15m_vol, 2) if avg_15m_vol else 0.0
        price_change_pct = round(((current_price - prev_close) / prev_close) * 100.0, 2) if prev_close else 0.0

        # Order-book imbalance
        buy_pct = tick.get("buy_pct")
        if buy_pct is None:
            total_bid = float(tick.get("total_bid_qty") or 0.0)
            total_ask = float(tick.get("total_ask_qty") or 0.0)
            if (total_bid + total_ask) > 0:
                buy_pct = round((total_bid / (total_bid + total_ask)) * 100.0, 2)
        try:
            buy_pct = float(buy_pct) if buy_pct is not None else 50.0
        except (TypeError, ValueError):
            buy_pct = 50.0

        # RVOL slope
        key = symbol.upper().replace(".NS", "").replace(".BO", "")
        prev_rvol  = self._last_rvol.get(key)
        rvol_slope = round(rvol - prev_rvol, 2) if prev_rvol is not None else 0.0
        self._last_rvol[key] = rvol

        score = 0
        trigger_type = "Consolidation"

        # ── 1. Volume Burst (35) — unchanged ─────────────────────────────────
        if rvol >= 3.5:
            score += 35
        elif rvol >= 2.0:
            score += 20
        elif rvol >= 1.5:
            score += 10

        # ── 1b. Volume slope (10) — threshold raised 0.4 → 0.6 ──────────────
        # In choppy market, 0.4 RVOL slope catches too many false breakouts.
        if rvol_slope >= RVOL_SLOPE_MIN:
            score += 10
            if trigger_type == "Consolidation":
                trigger_type = "Volume Accelerating"

        # ── 2. ORB / VWAP (25) — reduced from 30 to fund order-book promotion ─
        if current_price > orb_high and current_price > vwap:
            score += 25
            trigger_type = "Morning ORB Breakout"
        elif current_price > vwap:
            score += 13
            if trigger_type in ("Consolidation", "Volume Accelerating"):
                trigger_type = "Above VWAP"

        # ── 2b. Order-book imbalance (15) — PROMOTED from 10 to 15 ───────────
        # In an FII-short market, buy-side pressure is the most reliable signal.
        # 75%+ buy-side = institutional accumulation even against market trend.
        if buy_pct >= 75.0:
            score += 15
            if trigger_type == "Consolidation":
                trigger_type = "Buy-Side Imbalance"
        elif buy_pct >= 65.0:
            score += 8
        elif buy_pct >= 60.0:
            score += 4

        # ── 3. 52W proximity (15) — TIGHTENED thresholds ─────────────────────
        # In a falling index, near-52W-high stocks = genuine relative strength.
        # Tightened: ≤8% from high (was ≤5%) = top score; ≤15% (was ≤12%) = partial.
        dist = float(static.get("dist_52w_pct") or 100.0)
        if dist <= DIST_52W_BREAKOUT_PCT:
            score += 15
            if score >= 50:
                trigger_type = "Near 52W High"
        elif dist <= DIST_52W_NEAR_PCT:
            score += 7

        # ── 4. Range expansion (10) — REDUCED from 15 to fund order-book ─────
        daily_atr      = float(static.get("daily_atr") or 0.0)
        intraday_range = max(0.0, day_high - day_low)
        if daily_atr > 0 and intraday_range >= (daily_atr * 0.8):
            score += 10
            if trigger_type == "Consolidation":
                trigger_type = "Range Expansion"

        px = round(current_price, 2)
        if MAX_STOCK_PRICE > 0 and px > MAX_STOCK_PRICE:
            return None

        if score >= MIN_SCORE and price_change_pct > MIN_CHANGE_PCT:
            tier = "breakout"
        elif (
            price_change_pct >= SHOCKER_MIN_CHANGE_PCT
            and rvol >= SHOCKER_MIN_RVOL
            and current_price > prev_close
        ):
            # Volume-Shocker override — see comment on SHOCKER_MIN_CHANGE_PCT
            # above. A confirmed big % move on real volume is "breakout"
            # grade on its own; don't let it get stuck in "building" (or
            # dropped entirely) just because it's far from its 52W high or
            # the feed has no live order-book depth for buy_pct.
            tier = "breakout"
            trigger_type = "Volume Shocker"
        elif score >= BUILDING_MIN_SCORE and price_change_pct > BUILDING_MIN_CHANGE_PCT:
            tier = "building"
            if trigger_type == "Consolidation":
                trigger_type = "Early Accumulation"
        else:
            return None

        hit = {
            "symbol":       key,
            "score":        int(score),
            "tier":         tier,
            "price":        px,
            "change_pct":   price_change_pct,
            "rvol":         rvol,
            "rvol_slope":   rvol_slope,
            "buy_pct":      buy_pct,
            "trigger_type": trigger_type,
            # BUGFIX (31-Aug-2026): trailing_stop was `vwap * 0.985`. That was
            # always safely below current_price only because the old vwap
            # default WAS current_price (the very bug fixed above). Now that
            # vwap is a genuine same-session proxy, it can legitimately sit
            # ABOVE current_price (e.g. a stock that gapped up, spiked, and
            # faded — still up on the day and qualifying via other buckets,
            # but currently trading below its own session typical price) —
            # which produced a "stop-loss" above the entry price. Anchor to
            # whichever of {vwap, current_price} is lower so the stop is
            # always genuinely below where the stock is trading right now.
            "trailing_stop": round(min(vwap, current_price) * 0.985, 2),
            "target_1":     round(current_price * 1.05, 2),
            "prev_close":   round(prev_close, 2),
            "sector":       static.get("sector"),
            "dist_52w_pct": round(dist, 2),
            # Market context stamp for dashboard audit trail
            "market_note":  "Aug-2026: Nifty -7% 6m, FII net-short, high buy_pct = strong signal",
        }
        hit["cmp"]           = px
        hit["ltp"]           = px
        hit["last_price"]    = px
        hit["close"]         = px
        hit["current_price"] = px
        hit["value_buy"]     = bool(20.0 <= px <= VALUE_BUY_THRESHOLD)

        # §5 passes_hard_floor — liquidity gate (BUY-side only)
        avg_vol = float(static.get("avg_15m_volume") or 0)
        avg_traded_value = avg_vol * px * 25  # 25 bars/day estimate
        hit["avg_traded_value"] = avg_traded_value
        if avg_traded_value < HARD_FLOOR_LIQUIDITY and avg_traded_value > 0:
            logger.debug(
                "surprise %s: liquidity floor fail avg_traded_value=%.0f < %.0f",
                symbol, avg_traded_value, HARD_FLOOR_LIQUIDITY,
            )
            return None  # drop illiquid symbols before scoring

        return hit

    def _tick_from_bulk_cache(self, symbol: str) -> Optional[dict]:
        try:
            from data_feed import get_cached_quote
            row = get_cached_quote(symbol)
        except Exception:
            row = None
        if not isinstance(row, dict):
            return None
        price = row.get("price") or row.get("close") or row.get("cmp") or row.get("ltp")
        if not price:
            return None
        return {
            "price":         price,
            "close":         row.get("close") or price,
            "open":          row.get("open"),
            "high":          row.get("day_high") or row.get("high"),
            "low":           row.get("day_low")  or row.get("low"),
            "volume":        row.get("volume"),
            "vwap":          row.get("vwap"),
            "orb_high":      row.get("orb_high"),
            "vol_15m":       row.get("vol_15m"),
            "buy_pct":       row.get("buy_pct") or row.get("buy_percentage"),
            "total_bid_qty": row.get("total_bid_qty") or row.get("total_buy_quantity"),
            "total_ask_qty": row.get("total_ask_qty") or row.get("total_sell_quantity"),
            "_from_cache":   True,
        }

    async def _fetch_quote(self, client: httpx.AsyncClient, market_data_url: str, symbol: str) -> Optional[dict]:
        cached = self._tick_from_bulk_cache(symbol)
        if cached:
            return cached
        async with self.semaphore:
            try:
                r = await client.get(f"{market_data_url.rstrip('/')}/quote/{symbol}", timeout=QUOTE_TIMEOUT)
                if r.status_code != 200:
                    return None
                data = r.json()
                if not isinstance(data, dict):
                    return None
                price = data.get("price") or data.get("close") or data.get("regularMarketPrice")
                return {
                    "price":         price,
                    "close":         data.get("close") or price,
                    "open":          data.get("open") or data.get("regularMarketOpen"),
                    "high":          data.get("high") or data.get("dayHigh") or data.get("regularMarketDayHigh"),
                    "low":           data.get("low")  or data.get("dayLow")  or data.get("regularMarketDayLow"),
                    "volume":        data.get("volume") or data.get("regularMarketVolume"),
                    "vwap":          data.get("vwap"),
                    "orb_high":      data.get("orb_high"),
                    "vol_15m":       data.get("vol_15m"),
                    "buy_pct":       data.get("buy_pct") or data.get("buy_percentage"),
                    "total_bid_qty": data.get("total_bid_qty") or data.get("total_buy_quantity"),
                    "total_ask_qty": data.get("total_ask_qty") or data.get("total_sell_quantity"),
                }
            except Exception as e:
                logger.debug("quote %s: %s", symbol, e)
                return None

    async def scan(
        self,
        client: httpx.AsyncClient,
        market_data_url: str,
        symbols: Optional[List[str]] = None,
        force_reload_static: bool = False,
        cached: bool = False,
        cached_max_age_sec: float = SURPRISE_CACHE_MAX_AGE_SEC,
    ) -> Dict[str, Any]:
        t0 = time.time()

        # ── 2026-09-01 incident fix ──────────────────────────────────────
        # candidate_engine's /surprise/scan?cached=true call was written to
        # expect a cheap read that "returns the last scored result without
        # triggering a new full scan cycle" (see candidates.py _SOURCES
        # comment) — but `cached` was never wired up here, so FastAPI
        # silently dropped the unrecognized query param and every single
        # call ran the full loop below (live quote fetch for the whole
        # liquid static universe, in batches of 25). Under load that's
        # exactly what produced the "candidate fetch ... failed: ReadTimeout"
        # in the log. A per-symbol request list (`symbols` given) still
        # always does a live fetch — the cache only covers the default
        # full-universe scan, which is what candidate_engine actually asks
        # for.
        if cached and not symbols and self._last_result is not None:
            age = time.time() - self._last_scan_ts
            if age <= cached_max_age_sec:
                result = dict(self._last_result)
                result["from_cache"] = True
                result["cache_age_sec"] = round(age, 1)
                return result

        n_static = self.load_static_cache(force=force_reload_static)
        if n_static == 0:
            return {
                "count": 0, "stocks": [], "static_loaded": 0,
                "error": "surprise_static_feed empty — run premarket job first",
                "elapsed_sec": round(time.time() - t0, 2),
            }

        if symbols:
            keys = [
                s.upper().replace(".NS", "").replace(".BO", "").strip()
                for s in symbols if s
            ]
            keys = [k for k in keys if k in self.static_cache]
        else:
            keys = [k for k, v in self.static_cache.items() if v.get("is_liquid", True)]
            if not keys:
                keys = list(self.static_cache.keys())

        results: List[dict] = []
        quote_ok = cache_hits = upstream_calls = 0
        chunk = 25
        for i in range(0, len(keys), chunk):
            batch = keys[i: i + chunk]
            ticks = await asyncio.gather(
                *[self._fetch_quote(client, market_data_url, sym) for sym in batch],
                return_exceptions=True,
            )
            for sym, tick in zip(batch, ticks):
                if isinstance(tick, Exception) or not tick:
                    upstream_calls += 1
                    scored = self.score_stock(sym, {})
                else:
                    quote_ok += 1
                    if tick.get("_from_cache"):
                        cache_hits += 1
                    else:
                        upstream_calls += 1
                    scored = self.score_stock(sym, tick)
                if scored:
                    results.append(scored)

        # Sector sympathy pass
        breakout_sectors = {
            r.get("sector") for r in results
            if r.get("tier") == "breakout" and r.get("sector")
        }
        if breakout_sectors:
            already_hit = {r["symbol"] for r in results}
            peer_keys = [
                k for k, v in self.static_cache.items()
                if v.get("sector") in breakout_sectors and k not in already_hit
            ]
            for i in range(0, len(peer_keys), chunk):
                batch = peer_keys[i: i + chunk]
                ticks = await asyncio.gather(
                    *[self._fetch_quote(client, market_data_url, sym) for sym in batch],
                    return_exceptions=True,
                )
                for sym, tick in zip(batch, ticks):
                    t = {} if isinstance(tick, Exception) or not tick else tick
                    scored = self.score_stock(sym, t)
                    if scored and scored["score"] >= SECTOR_SYMPATHY_MIN_SCORE:
                        scored["trigger_type"] = f"Sector Sympathy ({scored.get('sector')})"
                        results.append(scored)

        self._last_scan_ts = time.time()
        # §9 — deduplicate before ranking (symbol can appear twice across batches)
        results = dedupe_by_symbol(results)
        results.sort(key=lambda x: x["score"], reverse=True)
        breakout_count = sum(1 for r in results if r.get("tier") == "breakout")
        building_count = sum(1 for r in results if r.get("tier") == "building")
        result = {
            "count":           len(results),
            "stocks":          results,
            "breakout_count":  breakout_count,
            "building_count":  building_count,
            "static_loaded":   n_static,
            "quotes_ok":       quote_ok,
            "cache_hits":      cache_hits,
            "upstream_calls":  upstream_calls,
            "universe_scanned": len(keys),
            "elapsed_sec":     round(time.time() - t0, 2),
            "min_score":       MIN_SCORE,
            "min_change_pct":  MIN_CHANGE_PCT,
            "building_min_score": BUILDING_MIN_SCORE,
            "market_note":     "Aug-2026: Nifty -7% 6m, FII net-short — thresholds raised for quality",
        }
        # Only cache full-universe scans (symbols=None) — a filtered
        # request isn't representative of "the last scored result" that
        # the cached=true fast-path above is meant to serve.
        if not symbols:
            self._last_result = result
        return result


surprise_engine = SurpriseStockEngine()

# ── All cache/repair/audit functions below are unchanged from original ─────────
SURPRISE_FEED_CACHE_KEY = "system:surprise_feed"
SURPRISE_FEED_OPEN_TTL_SEC = int(__import__("os").getenv("SURPRISE_FEED_OPEN_TTL_SEC", "7200"))
SURPRISE_FEED_COOLDOWN_SEC = float(__import__("os").getenv("SURPRISE_FEED_COOLDOWN_SEC", "0.5"))
SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC = float(__import__("os").getenv("SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC", "300"))


def is_market_open_ist() -> bool:
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime, time as dtime
        now = datetime.now(ZoneInfo("Asia/Kolkata"))
        if now.weekday() >= 5:
            return False
        try:
            from nse_holidays import is_nse_holiday
            if is_nse_holiday(now.date()):
                return False
        except Exception:
            pass
        tt = now.time()
        return dtime(9, 15) <= tt <= dtime(15, 30)
    except Exception:
        return False


def _read_surprise_feed_cache() -> Optional[dict]:
    try:
        import kv_cache as _kc
        raw = _kc.kv_get(SURPRISE_FEED_CACHE_KEY)
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            import json as _json
            return _json.loads(raw)
    except Exception as e:
        logger.debug("surprise feed cache read: %s", e)
    return None


def _write_surprise_feed_cache(payload: dict) -> None:
    try:
        import kv_cache as _kc
        ttl = None if not is_market_open_ist() else max(SURPRISE_FEED_OPEN_TTL_SEC, 3600)
        _kc.kv_set(SURPRISE_FEED_CACHE_KEY, payload, ttl=ttl)
    except Exception as e:
        logger.warning("surprise feed cache write: %s", e)


async def run_market_aware_surprise_feed(
    symbols: Optional[list] = None,
    market_data_url: str = "",
    force: bool = False,
) -> dict:
    import json as _json
    import httpx

    cached = _read_surprise_feed_cache()
    if cached:
        age = time.time() - float(cached.get("timestamp") or 0)
        open_now = is_market_open_ist()
        if not force:
            if not open_now:
                return {"status": "success", "source": "cache", "market_open": False,
                        "age_sec": int(age), "data": cached.get("data") or [],
                        "message": "Market closed — serving durable cache (no API calls)"}
            if age < SURPRISE_FEED_OPEN_TTL_SEC:
                return {"status": "success", "source": "cache", "market_open": True,
                        "age_sec": int(age), "data": cached.get("data") or [],
                        "message": f"Cache hit ({int(age)}s old, TTL {SURPRISE_FEED_OPEN_TTL_SEC}s)"}
        elif age < SURPRISE_FEED_MIN_FORCE_INTERVAL_SEC:
            return {"status": "success", "source": "cache", "market_open": is_market_open_ist(),
                    "age_sec": int(age), "data": cached.get("data") or [],
                    "message": f"force=true but cache is only {int(age)}s old — reused"}

    syms = []
    if symbols:
        syms = [str(s).upper().replace(".NS", "").replace(".BO", "").strip() for s in symbols if s]
    if not syms:
        if surprise_engine.static_cache:
            syms = list(surprise_engine.static_cache.keys())
        else:
            try:
                surprise_engine.load_static_cache()
                syms = list(surprise_engine.static_cache.keys())
            except Exception:
                syms = []
    seen = set()
    syms = [s for s in syms if s and s not in seen and not seen.add(s)]

    if not syms:
        return {"status": "error", "source": "live", "data": [],
                "message": "No symbols — run premarket baselines first"}

    results = []
    errors  = 0
    got     = set()
    chunk_size = 50

    def _clean_sym(s: str) -> str:
        u = str(s or "").upper().replace(".NS", "").replace(".BO", "").strip().replace("%20", " ")
        _map = {
            "KFIN TECHNOLOGIES": "KFINTECH", "KPIT TECHNOLOGIES": "KPITTECH",
            "360 ONE": "360ONE", "360ONE WAM": "360ONE",
            "PB FINTECH": "POLICYBZR", "HONASA CONSUMER": "HONASA",
        }
        if u in _map:
            return _map[u]
        u = u.replace(" TECHNOLOGIES", "TECH").replace(" TECHNOLOGY", "TECH")
        u = u.replace(" LIMITED", "").replace(" LTD", "").replace(" ", "")
        try:
            from symbol_aliases import resolve_base_symbol, is_known_delisted
            if is_known_delisted(u):
                return ""
            resolved = resolve_base_symbol(u)
            if resolved is None:
                return ""
            u = resolved
        except Exception:
            pass
        return u

    try:
        import yfinance as yf
        clean_syms = []
        seen_c = set()
        for s in syms:
            c = _clean_sym(s)
            if c and c not in seen_c:
                seen_c.add(c)
                clean_syms.append(c)
        syms = clean_syms or syms

        try:
            from surprise_premarket import premarket_stop_requested
        except Exception:
            premarket_stop_requested = lambda: False

        for i in range(0, len(syms), chunk_size):
            if premarket_stop_requested():
                break
            chunk = syms[i: i + chunk_size]
            ticker_string = " ".join(f"{s}.NS" for s in chunk)
            try:
                df = yf.download(ticker_string, period="2d", group_by="ticker",
                                 threads=True, progress=False, auto_adjust=True)
                for s in chunk:
                    try:
                        sym_ns = f"{s}.NS"
                        sub = None
                        if df is not None and hasattr(df, "columns") and getattr(df.columns, "nlevels", 1) > 1:
                            if sym_ns in df.columns.get_level_values(0):
                                sub = df[sym_ns]
                        elif len(chunk) == 1:
                            sub = df
                        if sub is None or (hasattr(sub, "empty") and sub.empty):
                            continue
                        if "Close" not in getattr(sub, "columns", []):
                            continue
                        series = sub["Close"].dropna()
                        if series.empty:
                            continue
                        px = float(series.iloc[-1])
                        if px <= 0 or (MAX_STOCK_PRICE > 0 and px > MAX_STOCK_PRICE):
                            continue
                        prev = float(series.iloc[-2]) if len(series) >= 2 else px
                        chg  = round(((px - prev) / prev) * 100, 2) if prev > 0 else None
                        high = low = vol = None
                        try:
                            if "High"   in sub.columns: high = float(sub["High"].dropna().iloc[-1])
                            if "Low"    in sub.columns: low  = float(sub["Low"].dropna().iloc[-1])
                            if "Volume" in sub.columns:
                                vol = int(float(sub["Volume"].dropna().iloc[-1]))
                                if vol < 0: vol = None
                        except Exception:
                            pass
                        row = {"symbol": s, "price": px, "cmp": px,
                               "previous_close": prev, "day_change_pct": chg,
                               "day_high": high, "day_low": low, "source": "yahoo_bulk"}
                        if vol is not None:
                            row["volume"] = vol
                        results.append({k: v for k, v in row.items() if v is not None})
                        got.add(s)
                    except Exception:
                        errors += 1
            except Exception as e:
                logger.warning("surprise yf chunk %s: %s", i, e)
                errors += 1
            await asyncio.sleep(0.5)
    except Exception as e:
        logger.warning("surprise chunked yf unavailable: %s", e)

    misses = [s for s in syms if s not in got]
    md = (market_data_url or "").rstrip("/")
    if misses and md:
        try:
            from surprise_premarket import premarket_stop_requested as _stop_check
        except Exception:
            _stop_check = lambda: False
        async with httpx.AsyncClient(timeout=8.0, follow_redirects=True) as client:
            for sym in misses:
                if _stop_check():
                    break
                try:
                    r = await client.get(f"{md}/quote/{sym}")
                    if r.status_code == 200:
                        body = r.json() if isinstance(r.json(), dict) else {}
                        px = None
                        for k in ("price", "cmp", "ltp", "close", "last_price", "regularMarketPrice"):
                            try:
                                v = float(body.get(k) or 0)
                                if v > 0:
                                    px = v
                                    break
                            except (TypeError, ValueError):
                                pass
                        if px and px > 0 and not (MAX_STOCK_PRICE > 0 and px > MAX_STOCK_PRICE):
                            results.append({"symbol": sym, "price": px, "cmp": px,
                                            "source": body.get("source") or "waterfall"})
                            got.add(sym)
                    elif r.status_code in (401, 429):
                        errors += 1
                        await asyncio.sleep(SURPRISE_FEED_COOLDOWN_SEC * 2)
                except Exception:
                    errors += 1
                await asyncio.sleep(SURPRISE_FEED_COOLDOWN_SEC)

    payload = {"timestamp": time.time(), "data": results,
               "count": len(results), "errors": errors,
               "market_open": is_market_open_ist()}
    _write_surprise_feed_cache(payload)
    return {"status": "success", "source": "live", "market_open": is_market_open_ist(),
            "data": results, "count": len(results), "errors": errors,
            "message": f"Live surprise feed: {len(results)} quotes, {errors} errors"}


def audit_surprise_feed() -> dict:
    cached = _read_surprise_feed_cache() or {}
    data = cached.get("data") if isinstance(cached, dict) else []
    if not isinstance(data, list):
        data = []
    total = len(data)
    missing = []
    complete = 0
    for row in data:
        if not isinstance(row, dict):
            continue
        px = 0.0
        for k in ("price", "cmp", "ltp", "close"):
            try:
                v = float(row.get(k) or 0)
                if v > 0:
                    px = v
                    break
            except (TypeError, ValueError):
                pass
        if px <= 0:
            missing.append({"symbol": str(row.get("symbol") or "").upper(), "missing_fields": ["price"]})
        else:
            complete += 1
    health = round((complete / max(total, 1)) * 100, 1) if total > 0 else 0.0
    return {
        "ok": True, "total_tracked": total, "fully_populated": complete,
        "missing_data": len(missing), "health_score": health,
        "incomplete_stocks": missing[:200],
        "cache_age_sec": int(time.time() - float(cached.get("timestamp") or 0)) if cached else None,
        "market_open": is_market_open_ist(), "source": "cache" if cached else "empty",
        "thresholds": {"min_score": MIN_SCORE, "min_change_pct": MIN_CHANGE_PCT,
                       "rvol_slope_min": RVOL_SLOPE_MIN},
    }


def repair_surprise_batch(limit: int = 15, market_data_url: str = "", symbol: str = None) -> dict:
    import os
    import httpx

    cached = _read_surprise_feed_cache() or {}
    data_list = list(cached.get("data") or [])
    if not data_list:
        return {"status": "no_data", "repaired": []}

    force_sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip() or None
    targets = []
    if force_sym:
        found = any(str(item.get("symbol") or "").upper() == force_sym for item in data_list)
        if not found:
            data_list.append({"symbol": force_sym, "price": 0, "cmp": 0})
        targets = [force_sym]
    else:
        for item in data_list:
            if not isinstance(item, dict):
                continue
            try:
                px = float(item.get("price") or item.get("cmp") or 0)
            except (TypeError, ValueError):
                px = 0.0
            if px <= 0:
                sym = str(item.get("symbol") or "").upper().strip()
                if sym:
                    targets.append(sym)
        targets = targets[: max(1, min(int(limit or 15), 30))]

    if not targets:
        return {"status": "completed", "repaired": [], "message": "Nothing missing"}

    md = (market_data_url or os.getenv("MARKET_DATA_URL") or "").rstrip("/")
    repaired = []
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as client:
            for sym in targets:
                try:
                    r = client.get(f"{md}/quote/{sym}")
                    if r.status_code == 200:
                        body = r.json() if isinstance(r.json(), dict) else {}
                        px = None
                        for k in ("price", "cmp", "ltp", "close", "last_price"):
                            try:
                                v = float(body.get(k) or 0)
                                if v > 0:
                                    px = v
                                    break
                            except (TypeError, ValueError):
                                pass
                        if px is None or (MAX_STOCK_PRICE > 0 and px > MAX_STOCK_PRICE):
                            time.sleep(SURPRISE_FEED_COOLDOWN_SEC)
                            continue
                        for item in data_list:
                            if str(item.get("symbol") or "").upper() == sym:
                                item["price"] = px
                                item["cmp"]   = px
                                item["source"] = body.get("source") or "waterfall"
                                repaired.append(sym)
                                break
                except Exception:
                    pass
                time.sleep(SURPRISE_FEED_COOLDOWN_SEC)
    except Exception as e:
        logger.warning("repair_surprise_batch: %s", e)

    cached["data"]      = data_list
    cached["timestamp"] = time.time()
    _write_surprise_feed_cache(cached)
    return {"status": "completed", "repaired": repaired,
            "repaired_count": len(repaired), "targets": targets}