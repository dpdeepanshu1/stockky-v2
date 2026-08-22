"""
Pre-market surprise baselines (≈08:55 IST).

Computes once per session:
  - prev_close
  - avg_15m_volume (daily volume / 25 NSE 15m slots)
  - daily_atr (mean high-low over lookback)
  - high_52w + dist_52w_pct

Writes into Neon table `surprise_static_feed` so intraday scans never
re-query multi-day history on the free-tier dyno.

Sources: yfinance history (free). Optional: daily_bhavcopy table if present.

Step 3 fix: concurrent ThreadPoolExecutor + batched multi-row upserts
instead of sequential per-symbol Neon/yfinance round-trips (was 45–90s).
"""
from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("surprise-premarket")

LOOKBACK_DAYS = int(os.getenv("SURPRISE_LOOKBACK_DAYS", "30"))
MAX_SYMBOLS = int(os.getenv("SURPRISE_MAX_SYMBOLS", "320"))
# Concurrent yfinance workers (free-tier safe; override via env)
MAX_WORKERS = int(os.getenv("SURPRISE_PREMARKET_WORKERS", "6"))  # free-tier safe
# Flush to Neon every N successful rows
UPSERT_BATCH = int(os.getenv("SURPRISE_UPSERT_BATCH", "40"))


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    url = re.sub(r"(?i)([?&]sslmode=)required\b", r"\1require", url)
    if "sslmode=" not in url.lower():
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


# surprise_schema.py owns backend detection, engine construction and the
# dialect-specific SQL. Imported defensively so a missing module degrades to the
# original Neon-only behaviour rather than breaking the service.
try:
    import surprise_schema as _ss
except Exception:  # pragma: no cover
    _ss = None


def _dialect() -> str:
    """'oracle' on the Oracle Cloud VM, 'postgresql' on Render/Neon."""
    if _ss is not None:
        try:
            return _ss.dialect()
        except Exception:
            pass
    return "oracle" if os.environ.get("ORACLE_DSN") else "postgresql"


def _conn_dialect(conn) -> str:
    """Dialect of an already-open connection (for helpers that receive a conn)."""
    try:
        return (conn.dialect.name or "").lower()
    except Exception:
        return _dialect()


def _db_url() -> Optional[str]:
    # Both deployments are now self-sufficient: Render/Neon keeps its Postgres
    # URL, and the Oracle VM gets an Oracle URL instead of the None that used to
    # make this whole feature a silent no-op there.
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


def _engine(app_name: str, **_ignored):
    """Engine for the configured backend, or None.

    Postgres connect_args (connect_timeout/application_name) are psycopg2-only
    and would be rejected by python-oracledb, so engine construction has to go
    through surprise_schema.make_engine() rather than create_engine(_db_url()).
    """
    if _ss is not None:
        try:
            return _ss.make_engine(app_name)
        except Exception as e:
            logger.debug("surprise_schema.make_engine: %s", e)
    url = _db_url()
    if not url:
        return None
    from sqlalchemy import create_engine

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=1,
        connect_args={"connect_timeout": 20, "application_name": app_name},
    )


def _sym_filter(column: str, param: str, dial: str):
    """'<col> = ANY(:p)' on Postgres, an expanding IN list on Oracle.

    Returns (sql_fragment, bindparam_or_None). Oracle has no array type for
    plain binds, so the list has to be expanded into (:p_1, :p_2, ...); its
    hard limit is 1000 expression list elements, hence chunking by callers.
    """
    if dial == "oracle":
        from sqlalchemy import bindparam

        return f"{column} IN :{param}", bindparam(param, expanding=True)
    return f"{column} = ANY(:{param})", None


_ORACLE_IN_CHUNK = 900  # stay under Oracle's 1000-element expression list cap


def _chunks(seq: List[str], size: int) -> List[List[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


# Last-resort Postgres upsert, used only if surprise_schema.py could not be
# imported. Identical text to the original inline statement.
_PG_UPSERT_SQL = """
    INSERT INTO surprise_static_feed
        (symbol, prev_close, avg_15m_volume, daily_atr, high_52w, dist_52w_pct, sector, is_liquid, updated_at)
    VALUES
        (:symbol, :prev_close, :avg_15m_volume, :daily_atr, :high_52w, :dist_52w_pct, :sector, :is_liquid, NOW())
    ON CONFLICT (symbol) DO UPDATE SET
        prev_close = EXCLUDED.prev_close,
        avg_15m_volume = EXCLUDED.avg_15m_volume,
        daily_atr = EXCLUDED.daily_atr,
        high_52w = EXCLUDED.high_52w,
        dist_52w_pct = EXCLUDED.dist_52w_pct,
        sector = COALESCE(EXCLUDED.sector, surprise_static_feed.sector),
        is_liquid = EXCLUDED.is_liquid,
        updated_at = NOW()
"""


def ensure_schema() -> bool:
    # Sticky Fix Step 4: single schema source (surprise_schema.py)
    try:
        from surprise_schema import ensure_surprise_schema
        result = ensure_surprise_schema()
        if result.get("ok"):
            return True
        logger.warning("ensure_surprise_schema: %s", result.get("error"))
    except Exception as e:
        logger.debug("surprise_schema import/call: %s", e)

    url = _db_url()
    if not url:
        logger.warning(
            "No DATABASE_URL/CACHE_DATABASE_URL — cannot ensure surprise_static_feed. "
            "Set Neon pooler URL on api-gateway env."
        )
        return False
    if _dialect() == "oracle":
        # The inline DDL below is Postgres-only (NUMERIC/BIGINT/BOOLEAN/
        # TIMESTAMPTZ/IF NOT EXISTS). Oracle DDL lives solely in
        # surprise_schema.py, so if we got here the import above failed and
        # there is nothing safe to fall back to.
        logger.error(
            "surprise: Oracle mode but surprise_schema.py unavailable — "
            "cannot create surprise_static_feed"
        )
        return False
    try:
        from sqlalchemy import create_engine, text

        eng = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
            connect_args={"connect_timeout": 15, "application_name": "surprise-premarket-ddl"},
        )
        create_sql = text(
            """
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
            """
        )
        idx_sql = text(
            "CREATE INDEX IF NOT EXISTS idx_surprise_static_sym ON surprise_static_feed(symbol)"
        )
        with eng.begin() as conn:
            conn.execute(create_sql)
            conn.execute(idx_sql)
        eng.dispose()
        logger.info("surprise_static_feed schema OK")
        return True
    except Exception as e:
        logger.error("schema ensure failed: %s", e)
        return False


def _yahoo_sym(symbol: str) -> str:
    s = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    if not s:
        return ""
    return f"{s}.NS"


_INDEX_SKIP = {"NIFTY50", "NIFTY", "NIFTY 50", "BANKNIFTY", "NIFTYBANK", "SENSEX"}
YF_BULK_BATCH_SIZE = int(os.getenv("SURPRISE_YF_BULK_BATCH", "50"))
YF_BULK_BATCH_PAUSE = float(os.getenv("SURPRISE_YF_BULK_PAUSE", "0.5"))


def _yahoo_session():
    """Reuse a single browser-like session across all yfinance calls in this
    process instead of letting each symbol/thread negotiate its own
    cookie/crumb — that duplication under concurrency is what produced the
    Invalid Crumb / 429 storm in the logs."""
    try:
        import requests
        import yfinance as yf

        existing = getattr(yf.shared, "_session", None)
        if existing is not None:
            return existing
        sess = requests.Session()
        sess.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
        })
        try:
            yf.set_session(sess)
        except AttributeError:
            yf.shared._session = sess
        return sess
    except Exception:
        return None


def bulk_baselines_from_yfinance(
    symbols: List[str], batch_size: int = YF_BULK_BATCH_SIZE
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Real bulk path: chunked yf.download() (group_by='ticker') instead of
    one yf.Ticker(...).history()+.info() round-trip per symbol — this is
    what the 'gateway premarket job' rate-limit storm in the logs was
    missing. Also skips index pseudo-symbols (NIFTY50 etc.) that don't take
    a .NS suffix and were logging as 'possibly delisted'. Optionally paces
    itself against the shared rate limiter (see rate_limiter.py) so this
    doesn't stack with other jobs (data-feed, refill-additional, hot-picks)
    hitting the same upstream providers concurrently."""
    try:
        import numpy as np
        import pandas as pd
        import yfinance as yf
    except ImportError as e:
        logger.error("numpy/pandas/yfinance required: %s", e)
        return [], list(symbols)

    try:
        from rate_limiter import acquire as rl_acquire
    except Exception:
        rl_acquire = None

    _yahoo_session()

    clean: List[str] = []
    skipped: List[str] = []
    for s in symbols:
        base = (s or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if not base:
            continue
        if base in _INDEX_SKIP or base.startswith("^"):
            skipped.append(base)
            continue
        clean.append(base)

    rows: List[Dict[str, Any]] = []
    found = set()

    for i in range(0, len(clean), batch_size):
        batch = clean[i : i + batch_size]
        yf_tickers = [f"{b}.NS" for b in batch]
        if rl_acquire:
            rl_acquire("yfinance", weight=len(batch))
        try:
            data = yf.download(
                tickers=" ".join(yf_tickers),
                period="1y",
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            logger.warning("surprise bulk yf.download batch [%s:%s] failed: %s", i, i + len(batch), e)
            continue

        if data is None or (hasattr(data, "empty") and data.empty):
            continue

        is_multi = isinstance(data.columns, pd.MultiIndex) if hasattr(data, "columns") else False

        for base, ysym in zip(batch, yf_tickers):
            try:
                if is_multi:
                    if ysym not in data.columns.levels[0]:
                        continue
                    sub = data[ysym].dropna(how="all")
                else:
                    sub = data.dropna(how="all")
                if sub is None or len(sub) < 5:
                    continue

                tail = sub.tail(max(LOOKBACK_DAYS, 20))
                highs = tail["High"].astype("float64").values
                lows = tail["Low"].astype("float64").values
                closes = tail["Close"].astype("float64").values
                volumes = tail["Volume"].astype("float64").values

                prev_close = float(closes[-1])
                high_52w = float(np.nanmax(sub["High"].astype("float64").values))
                if high_52w <= 0 or prev_close <= 0:
                    continue
                dist_52w_pct = float(((high_52w - prev_close) / high_52w) * 100.0)
                avg_daily_vol = float(np.nanmean(volumes)) if len(volumes) else 0.0
                avg_15m_vol = int(max(1, avg_daily_vol / 25.0))
                daily_atr = float(np.nanmean(highs - lows)) if len(highs) else 0.0

                rows.append({
                    "symbol": base,
                    "prev_close": round(prev_close, 2),
                    "avg_15m_volume": avg_15m_vol,
                    "daily_atr": round(daily_atr, 2),
                    "high_52w": round(high_52w, 2),
                    "dist_52w_pct": round(dist_52w_pct, 2),
                    "sector": None,
                    "is_liquid": bool(avg_daily_vol >= 50000),
                })
                found.add(base)
            except Exception as e:
                logger.debug("surprise bulk extract failed for %s: %s", base, e)
                continue

        if i + batch_size < len(clean):
            time.sleep(YF_BULK_BATCH_PAUSE)

    remaining = [b for b in clean if b not in found]
    logger.info(
        "surprise bulk yfinance: %s ok, %s remaining, %s index symbols skipped",
        len(rows), len(remaining), len(skipped),
    )
    return rows, remaining


def compute_baseline_for_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    """Single-symbol fallback for whatever bulk_baselines_from_yfinance
    couldn't resolve — should only run against a small residual list, not
    the whole universe. Sector/.info lookup dropped (crumb-hungry, non-
    critical) — the same fix already applied to the market-data-service
    copy of this file."""
    try:
        import numpy as np
        import yfinance as yf
    except ImportError as e:
        logger.error("numpy/yfinance required: %s", e)
        return None

    base = symbol.upper().replace(".NS", "").replace(".BO", "").strip()
    if base in _INDEX_SKIP or base.startswith("^"):
        return None
    ysym = _yahoo_sym(base)
    if not ysym:
        return None

    _yahoo_session()
    try:
        from rate_limiter import acquire as rl_acquire
        rl_acquire("yfinance", weight=1)
    except Exception:
        pass

    try:
        t = yf.Ticker(ysym)
        hist = t.history(period="1y", interval="1d", auto_adjust=True)
        if hist is None or hist.empty or len(hist) < 5:
            return None
        tail = hist.tail(max(LOOKBACK_DAYS, 20))
        highs = tail["High"].astype("float64").values
        lows = tail["Low"].astype("float64").values
        closes = tail["Close"].astype("float64").values
        volumes = tail["Volume"].astype("float64").values

        prev_close = float(closes[-1])
        high_52w = float(np.nanmax(hist["High"].astype("float64").values))
        if high_52w <= 0:
            return None
        dist_52w_pct = float(((high_52w - prev_close) / high_52w) * 100.0)
        avg_daily_vol = float(np.nanmean(volumes)) if len(volumes) else 0.0
        avg_15m_vol = int(max(1, avg_daily_vol / 25.0))
        daily_atr = float(np.nanmean(highs - lows)) if len(highs) else 0.0

        return {
            "symbol": base,
            "prev_close": round(prev_close, 2),
            "avg_15m_volume": avg_15m_vol,
            "daily_atr": round(daily_atr, 2),
            "high_52w": round(high_52w, 2),
            "dist_52w_pct": round(dist_52w_pct, 2),
            "sector": None,
            "is_liquid": bool(avg_daily_vol >= 50000),
        }
    except Exception as e:
        logger.debug("baseline %s failed: %s", base, e)
        return None


def compute_baseline_from_bhavcopy(symbol: str, conn) -> Optional[Dict[str, Any]]:
    """
    Optional fast path: read from daily_bhavcopy if the table exists.
    Returns None when table/rows missing so caller falls back to yfinance.
    """
    try:
        import numpy as np
        from sqlalchemy import text

        base = symbol.upper().replace(".NS", "").replace(".BO", "").strip()
        # Oracle has no LIMIT; FETCH FIRST n ROWS ONLY is the 12c+ equivalent.
        _limit = (
            "FETCH FIRST 20 ROWS ONLY"
            if _conn_dialect(conn) == "oracle"
            else "LIMIT 20"
        )
        rows = conn.execute(
            text(
                f"""
                SELECT high, low, close, volume
                FROM daily_bhavcopy
                WHERE symbol = :sym
                ORDER BY trade_date DESC
                {_limit}
                """
            ),
            {"sym": base},
        ).mappings().all()
        if not rows or len(rows) < 5:
            return None

        closes = np.array([float(r["close"]) for r in rows], dtype=np.float64)
        highs = np.array([float(r["high"]) for r in rows], dtype=np.float64)
        lows = np.array([float(r["low"]) for r in rows], dtype=np.float64)
        volumes = np.array([float(r["volume"] or 0) for r in rows], dtype=np.float64)

        prev_close = float(closes[0])
        high_52w = float(np.max(highs))
        if high_52w <= 0:
            return None
        dist_52w_pct = float(((high_52w - prev_close) / high_52w) * 100.0)
        avg_15m_vol = int(max(1, float(np.mean(volumes)) / 25.0))
        daily_atr = float(np.mean(highs - lows))

        return {
            "symbol": base,
            "prev_close": round(prev_close, 2),
            "avg_15m_volume": avg_15m_vol,
            "daily_atr": round(daily_atr, 2),
            "high_52w": round(high_52w, 2),
            "dist_52w_pct": round(dist_52w_pct, 2),
            "sector": None,
            "is_liquid": bool(float(np.mean(volumes)) >= 50000),
        }
    except Exception as e:
        # Table missing or query error → silent fallback to yfinance
        logger.debug("bhavcopy path %s: %s", symbol, e)
        return None



def _table_exists(conn, table_name: str) -> bool:
    try:
        from sqlalchemy import text

        if _conn_dialect(conn) == "oracle":
            # No information_schema in Oracle; user_tables lists this schema's
            # tables and stores unquoted names upper-cased.
            n = conn.execute(
                text("SELECT COUNT(*) FROM user_tables WHERE table_name = UPPER(:t)"),
                {"t": table_name},
            ).scalar()
            return bool(n)
        row = conn.execute(
            text(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = :t
                LIMIT 1
                """
            ),
            {"t": table_name},
        ).fetchone()
        return row is not None
    except Exception:
        return False


def bulk_baselines_from_bhavcopy(symbols: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    Fast path: one Neon connection, pull last ~20 sessions per symbol from
    daily_bhavcopy (if the table exists). Returns (rows, remaining_symbols).
    remaining_symbols should fall back to yfinance.
    """
    url = _db_url()
    if not url or not symbols:
        return [], list(symbols)
    try:
        import numpy as np
        from sqlalchemy import text

        dial = _dialect()
        eng = _engine("surprise-bhav-bulk")
        if eng is None:
            return [], list(symbols)
        raw: List[Dict[str, Any]] = []
        with eng.connect() as conn:
            if not _table_exists(conn, "daily_bhavcopy"):
                eng.dispose()
                return [], list(symbols)

            # Normalize symbols for IN clause
            bases = [s.upper().replace(".NS", "").replace(".BO", "").strip() for s in symbols]
            bases = [b for b in bases if b]
            if not bases:
                eng.dispose()
                return [], list(symbols)

            # Window function: last 20 rows per symbol
            where, bp = _sym_filter("symbol", "syms", dial)
            sql = text(
                f"""
                WITH ranked AS (
                    SELECT
                        symbol,
                        high,
                        low,
                        close,
                        volume,
                        ROW_NUMBER() OVER (PARTITION BY symbol ORDER BY trade_date DESC) AS rn
                    FROM daily_bhavcopy
                    WHERE {where}
                )
                SELECT symbol, high, low, close, volume
                FROM ranked
                WHERE rn <= 20
                ORDER BY symbol, rn
                """
            )
            if bp is not None:
                sql = sql.bindparams(bp)
            # Oracle caps an expression list at 1000 entries, so run the query
            # once per chunk and concatenate; Postgres takes the whole array in
            # a single round-trip exactly as before.
            batches = _chunks(bases, _ORACLE_IN_CHUNK) if dial == "oracle" else [bases]
            try:
                for batch in batches:
                    result = conn.execute(sql, {"syms": batch})
                    raw.extend(result.mappings().all())
            except Exception as e:
                # Some Neon/pg versions prefer different array binding
                logger.debug("bulk bhavcopy query failed: %s", e)
                eng.dispose()
                return [], list(symbols)

        eng.dispose()

        by_sym: Dict[str, List[Dict[str, Any]]] = {}
        for r in raw:
            sym = str(r["symbol"]).upper().strip()
            by_sym.setdefault(sym, []).append(dict(r))

        rows: List[Dict[str, Any]] = []
        found = set()
        for sym, hist in by_sym.items():
            if len(hist) < 5:
                continue
            try:
                closes = np.array([float(x["close"]) for x in hist], dtype=np.float64)
                highs = np.array([float(x["high"]) for x in hist], dtype=np.float64)
                lows = np.array([float(x["low"]) for x in hist], dtype=np.float64)
                volumes = np.array([float(x.get("volume") or 0) for x in hist], dtype=np.float64)
                prev_close = float(closes[0])
                high_52w = float(np.max(highs))
                if high_52w <= 0 or prev_close <= 0:
                    continue
                dist_52w_pct = float(((high_52w - prev_close) / high_52w) * 100.0)
                avg_15m_vol = int(max(1, float(np.mean(volumes)) / 25.0))
                daily_atr = float(np.mean(highs - lows))
                rows.append({
                    "symbol": sym,
                    "prev_close": round(prev_close, 2),
                    "avg_15m_volume": avg_15m_vol,
                    "daily_atr": round(daily_atr, 2),
                    "high_52w": round(high_52w, 2),
                    "dist_52w_pct": round(dist_52w_pct, 2),
                    "sector": None,
                    "is_liquid": bool(float(np.mean(volumes)) >= 50000),
                })
                found.add(sym)
            except Exception:
                continue

        remaining = [s for s in bases if s not in found]
        logger.info(
            "bulk bhavcopy: %s baselines from Neon, %s remaining for yfinance",
            len(rows), len(remaining),
        )
        return rows, remaining
    except Exception as e:
        logger.debug("bulk_baselines_from_bhavcopy: %s", e)
        return [], list(symbols)


def upsert_baselines(rows: List[Dict[str, Any]]) -> int:
    """Batch upsert — single transaction, executemany-style."""
    if not rows:
        return 0
    url = _db_url()
    if not url:
        return 0
    # Oracle: MERGE instead of ON CONFLICT, and is_liquid bool -> NUMBER(1) 1/0.
    # Postgres: the original INSERT ... ON CONFLICT text and untouched rows.
    dial = _dialect()
    if _ss is not None:
        sql_text = _ss.upsert_sql(dial)
        rows = _ss.adapt_rows(rows, dial)
    else:
        sql_text = _PG_UPSERT_SQL
    try:
        from sqlalchemy import text

        eng = _engine("surprise-premarket-upsert")
        if eng is None:
            return 0
        sql = text(sql_text)
        with eng.begin() as conn:
            # executemany in one transaction (far fewer Neon round-trips)
            conn.execute(sql, rows)
        eng.dispose()
        return len(rows)
    except Exception as e:
        logger.error("upsert_baselines failed (%s rows): %s", len(rows), e)
        # Fallback: one-by-one so partial success is still stored
        n = 0
        try:
            from sqlalchemy import text
            eng = _engine("surprise-premarket-upsert-1by1")
            if eng is None:
                return 0
            sql = text(sql_text)
            with eng.begin() as conn:
                for r in rows:
                    try:
                        conn.execute(sql, r)
                        n += 1
                    except Exception:
                        pass
            eng.dispose()
        except Exception as e2:
            logger.error("upsert fallback failed: %s", e2)
        return n


# Progress file for UI polling (manual premarket button)
_PROGRESS_PATH = os.getenv(
    "SURPRISE_PREMARKET_PROGRESS_PATH",
    "/tmp/surprise_premarket_progress.json",
)
_job_lock = False


def _write_progress(data: Dict[str, Any]) -> None:
    try:
        import json as _json
        payload = dict(data)
        payload["updated_at"] = time.time()
        with open(_PROGRESS_PATH, "w", encoding="utf-8") as f:
            _json.dump(payload, f)
    except Exception as e:
        logger.debug("progress write: %s", e)


def get_premarket_progress() -> Dict[str, Any]:
    try:
        import json as _json
        with open(_PROGRESS_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {
        "stage": "idle",
        "percent": 0,
        "processed": 0,
        "total": 0,
        "errors": 0,
        "elapsed_sec": 0,
        "eta_sec": None,
        "is_running": False,
        "current_symbol": None,
        "message": "Idle",
    }


def _freshness_check(symbols: List[str]) -> Dict[str, Any]:
    """
    Count how many of `symbols` already have a surprise_static_feed row
    updated today (IST). Used to skip a full recompute when the button/cron
    is triggered more than once on the same trading day — baselines are only
    meaningful "as of premarket", so recomputing mid-afternoon just burns
    yfinance quota for an identical answer.
    """
    url = _db_url()
    if not url or not symbols:
        return {"fresh": 0, "total": len(symbols), "coverage": 0.0}
    try:
        from sqlalchemy import text
        from zoneinfo import ZoneInfo
        from datetime import datetime, timezone

        dial = _dialect()
        ist_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        bases = [s.upper().replace(".NS", "").replace(".BO", "").strip() for s in symbols]
        bases = [b for b in bases if b]

        eng = _engine("surprise-premarket-freshness")
        if eng is None:
            return {"fresh": 0, "total": len(symbols), "coverage": 0.0}
        rows = []
        with eng.connect() as conn:
            if not _table_exists(conn, "surprise_static_feed"):
                eng.dispose()
                return {"fresh": 0, "total": len(bases), "coverage": 0.0}
            where, bp = _sym_filter("symbol", "syms", dial)
            stmt = text(
                f"SELECT symbol, updated_at FROM surprise_static_feed WHERE {where}"
            )
            if bp is not None:
                stmt = stmt.bindparams(bp)
            for batch in (_chunks(bases, _ORACLE_IN_CHUNK) if dial == "oracle" else [bases]):
                rows.extend(conn.execute(stmt, {"syms": batch}).fetchall())
        eng.dispose()

        fresh = 0
        for _sym, updated_at in rows:
            try:
                ua = updated_at
                if getattr(ua, "tzinfo", None) is not None:
                    ua_ist = ua.astimezone(ZoneInfo("Asia/Kolkata"))
                elif dial == "oracle":
                    # Oracle TIMESTAMP is tz-naive and SYSTIMESTAMP on ADB is
                    # UTC; without this the date would land up to 5h30m early
                    # and premarket rows written before 05:30 IST would look
                    # stale, forcing a needless full recompute.
                    ua_ist = ua.replace(tzinfo=timezone.utc).astimezone(
                        ZoneInfo("Asia/Kolkata")
                    )
                else:
                    ua_ist = ua
                if ua_ist.date() == ist_today:
                    fresh += 1
            except Exception:
                continue
        total = len(bases) or 1
        return {"fresh": fresh, "total": total, "coverage": round(fresh / total, 3)}
    except Exception as e:
        logger.debug("freshness check failed: %s", e)
        return {"fresh": 0, "total": len(symbols), "coverage": 0.0}


def precalculate_surprise_baselines(symbols: List[str], force: bool = False) -> Dict[str, Any]:
    """
    Main entry: schema → concurrent compute → batched upsert.

    Step 3: ThreadPoolExecutor (default 10 workers) + batch Neon writes
    replaces the old sequential loop (sleep 0.12s × 300 ≈ 40s+ of pure wait).

    force=False (default): if today's (IST) surprise_static_feed already
    covers ≥90% of the requested universe, skip the recompute entirely —
    baselines are a once-a-trading-day snapshot, so re-running mid-day
    (double-click, duplicate cron dispatch, manual retrigger) should reuse
    what premarket already computed instead of re-hitting yfinance for
    every symbol again.
    """
    global _job_lock
    t0 = time.time()
    if _job_lock:
        return {"ok": False, "error": "already_running", "progress": get_premarket_progress()}
    _job_lock = True

    try:
        if not ensure_schema():
            out = {"ok": False, "error": "schema_failed", "upserted": 0}
            _write_progress({
                "stage": "error",
                "percent": 0,
                "processed": 0,
                "total": 0,
                "errors": 0,
                "elapsed_sec": 0,
                "eta_sec": None,
                "is_running": False,
                "message": "Schema ensure failed (check DATABASE_URL)",
                "error": "schema_failed",
            })
            return out

        uniq: List[str] = []
        seen = set()
        for s in symbols:
            b = (s or "").upper().replace(".NS", "").replace(".BO", "").strip()
            if b and b not in seen:
                seen.add(b)
                uniq.append(b)
        uniq = uniq[:MAX_SYMBOLS]
        total = len(uniq)

        if not force:
            fresh = _freshness_check(uniq)
            if fresh["coverage"] >= 0.9:
                _write_progress({
                    "stage": "done",
                    "percent": 100,
                    "processed": fresh["fresh"],
                    "total": total,
                    "computed": 0,
                    "errors": 0,
                    "elapsed_sec": round(time.time() - t0, 1),
                    "eta_sec": 0,
                    "is_running": False,
                    "current_symbol": None,
                    "message": (
                        f"Already fresh today: {fresh['fresh']}/{fresh['total']} baselines "
                        f"({int(fresh['coverage']*100)}%) — skipped recompute (pass force=true to override)"
                    ),
                })
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_fresh_today",
                    "symbols_requested": total,
                    "fresh_coverage": fresh["coverage"],
                    "computed": 0,
                    "upserted": 0,
                    "elapsed_sec": round(time.time() - t0, 1),
                }

        _write_progress({
            "stage": "starting",
            "percent": 1,
            "processed": 0,
            "total": total,
            "errors": 0,
            "elapsed_sec": 0,
            "eta_sec": None,
            "is_running": True,
            "current_symbol": None,
            "message": f"Starting concurrent baselines for {total} symbols (workers={MAX_WORKERS})",
        })

        workers = max(1, min(MAX_WORKERS, total or 1))
        ok_rows: List[Dict[str, Any]] = []
        errors = 0
        computed = 0
        processed = 0
        total_upserted = 0
        current_sym: Optional[str] = None
        source_bhav = 0
        source_yf = 0

        def _flush() -> None:
            nonlocal ok_rows, total_upserted
            if not ok_rows:
                return
            n = upsert_baselines(ok_rows)
            total_upserted += n
            ok_rows = []

        # Step 5: try bulk Neon daily_bhavcopy first (seconds, not tens of seconds)
        _write_progress({
            "stage": "bhavcopy",
            "percent": 2,
            "processed": 0,
            "total": total,
            "errors": 0,
            "elapsed_sec": 0,
            "eta_sec": None,
            "is_running": True,
            "current_symbol": None,
            "message": "Checking daily_bhavcopy fast path…",
        })
        bhav_rows, remaining = bulk_baselines_from_bhavcopy(uniq)
        if bhav_rows:
            ok_rows.extend(bhav_rows)
            computed += len(bhav_rows)
            processed += len(bhav_rows)
            source_bhav = len(bhav_rows)
            _flush()
            _write_progress({
                "stage": "computing",
                "percent": max(5, int(100 * processed / total)) if total else 5,
                "processed": processed,
                "total": total,
                "computed": computed,
                "errors": errors,
                "elapsed_sec": round(time.time() - t0, 1),
                "eta_sec": None,
                "is_running": True,
                "current_symbol": None,
                "message": f"Neon bhavcopy: {source_bhav} · yfinance left: {len(remaining)}",
            })
        else:
            remaining = list(uniq)

        # Bulk yfinance for symbols not covered by bhavcopy — replaces the
        # old per-symbol ThreadPoolExecutor fan-out (was the source of the
        # Invalid Crumb / 429 storm in the logs).
        _write_progress({
            "stage": "computing",
            "percent": max(10, int(100 * processed / total)) if total else 10,
            "processed": processed,
            "total": total,
            "computed": computed,
            "errors": errors,
            "elapsed_sec": round(time.time() - t0, 1),
            "eta_sec": None,
            "is_running": True,
            "current_symbol": None,
            "message": f"Bulk yfinance for {len(remaining)} symbols…",
        })
        bulk_rows, still_remaining = bulk_baselines_from_yfinance(remaining)
        if bulk_rows:
            ok_rows.extend(bulk_rows)
            computed += len(bulk_rows)
            processed += len(bulk_rows)
            source_yf += len(bulk_rows)
            _flush()
        remaining = still_remaining

        # Small residual fallback only — per-symbol, few workers.
        if remaining:
            residual_workers = max(1, min(3, len(remaining)))
            with ThreadPoolExecutor(max_workers=residual_workers) as pool:
                futures = {pool.submit(compute_baseline_for_symbol, sym): sym for sym in remaining}
                for fut in as_completed(futures):
                    sym = futures[fut]
                    current_sym = sym
                    try:
                        row = fut.result()
                        if row:
                            ok_rows.append(row)
                            computed += 1
                            source_yf += 1
                        else:
                            errors += 1
                    except Exception as e:
                        errors += 1
                        logger.debug("residual worker %s: %s", sym, e)

                    processed += 1
                    if len(ok_rows) >= UPSERT_BATCH:
                        _flush()

                    elapsed = time.time() - t0
                    rate = processed / elapsed if elapsed > 0.5 else 0
                    eta = (total - processed) / rate if rate > 0 else None
                    pct = int(100 * processed / total) if total else 0
                    _write_progress({
                        "stage": "computing",
                        "percent": min(99, pct),
                        "processed": processed,
                        "total": total,
                        "computed": computed,
                        "errors": errors,
                        "elapsed_sec": round(elapsed, 1),
                        "eta_sec": round(eta, 1) if eta is not None else None,
                        "is_running": True,
                        "current_symbol": current_sym,
                        "message": f"residual {processed}/{total} · {sym} · {residual_workers}w",
                    })
                    if processed % 50 == 0:
                        logger.info(
                            "surprise premarket progress %s/%s (computed=%s errors=%s elapsed=%.1fs)",
                            processed, total, computed, errors, elapsed,
                        )

        _flush()  # remaining rows

        elapsed = round(time.time() - t0, 1)
        result = {
            "ok": True,
            "symbols_requested": total,
            "computed": computed,
            "errors": errors,
            "elapsed_sec": elapsed,
            "table": "surprise_static_feed",
            "upserted": total_upserted,
            "upserted_last_batch": total_upserted,
            "workers": workers,
            "source_bhavcopy": source_bhav,
            "source_yfinance": source_yf,
        }
        _write_progress({
            "stage": "done",
            "percent": 100,
            "processed": total,
            "total": total,
            "computed": computed,
            "errors": errors,
            "elapsed_sec": elapsed,
            "eta_sec": 0,
            "is_running": False,
            "current_symbol": None,
            "message": f"Done · {computed} baselines · {errors} errors · {elapsed}s · {workers} workers",
            "result": result,
        })
        logger.info(
            "precalculate_surprise_baselines done: %s computed, %s errors, %.1fs, workers=%s",
            computed, errors, elapsed, workers,
        )
        return result
    except Exception as e:
        logger.exception("precalculate_surprise_baselines failed")
        _write_progress({
            "stage": "error",
            "percent": 0,
            "processed": 0,
            "total": 0,
            "errors": 1,
            "elapsed_sec": round(time.time() - t0, 1),
            "eta_sec": None,
            "is_running": False,
            "message": str(e)[:200],
            "error": str(e)[:200],
        })
        return {"ok": False, "error": str(e)[:200]}
    finally:
        _job_lock = False


def default_universe_from_env() -> List[str]:
    raw = os.getenv("SURPRISE_UNIVERSE", "") or os.getenv("SCAN_UNIVERSE", "")
    if raw.strip():
        return [x.strip() for x in raw.replace(";", ",").split(",") if x.strip()]
    return [
        "RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBIN", "BHARTIARTL",
        "ITC", "LT", "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    ]