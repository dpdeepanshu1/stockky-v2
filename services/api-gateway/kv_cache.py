"""
Stockky KV cache — Redis-free by default.

Layers (fast → durable):
  1. In-process memory with TTL  (always on — free, unlimited "commands")
  2. Neon/Postgres table `stockky_kv` for keys that must survive restarts
     (watchlist, data-feed, scan universe, notification config, IndianAPI).
     Use CACHE_DATABASE_URL (preferred) or DATABASE_URL / TRAINING_DATABASE_URL.
  3. Optional Upstash Redis only if USE_REDIS=1 (legacy).

Env:
  USE_REDIS=0|1              default 0 — disconnect Upstash
  DISABLE_UPSTASH=1          force Redis off even if USE_REDIS=1
  CACHE_DATABASE_URL         dedicated Neon for cache (recommended separate DB)
  CACHE_DB_POOL_SIZE         default 2
  KV_MEMORY_MAX_KEYS         default 8000
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from typing import Any, Optional

logger = logging.getLogger("kv-cache")

USE_REDIS = os.getenv("USE_REDIS", "0").lower() in ("1", "true", "yes")
if os.getenv("DISABLE_UPSTASH", "0").lower() in ("1", "true", "yes"):
    USE_REDIS = False
if os.getenv("DISABLE_REDIS", "0").lower() in ("1", "true", "yes"):
    USE_REDIS = False

KV_MEMORY_MAX_KEYS = int(os.getenv("KV_MEMORY_MAX_KEYS", "8000"))


# Keys written to Neon so Render restarts do not wipe them
_DURABLE_PREFIXES = (
    "stockky:watchlist",
    "stockky:searched",
    "stockky:scan_universe",
    "stockky:known_symbols",
    "stockky:data_feed",
    "feed:",  # alias key for data-feed payloads (Sticky Fix Step 2)
    "data_feed:",  # legacy mistaken prefix
    "stockky:hot_job",
    "stockky:hot_result",
    "stockky:last_full_scan",
    "stockky:lock:",
    "stockky:notification_config",
    "stockky:notification:",
    "indianapi:fundamentals:",
    "indianapi:",
    "stockky:decide_cache:",  # optional durability for decide (low volume)
    "stockky:batch_result:",  # scan batch cache survives free-tier sleep
    "stockky:rate_limit",  # rate-limit dashboard durable events/stats
)


class _MemEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, ttl: Optional[int]):
        self.value = value
        self.expires_at = (time.time() + ttl) if ttl else None


class MemoryTTLCache:
    def __init__(self, max_keys: int = KV_MEMORY_MAX_KEYS):
        self._store: dict[str, _MemEntry] = {}
        self._lock = threading.RLock()
        self._max = max(100, max_keys)

    def get(self, key: str) -> Any:
        with self._lock:
            e = self._store.get(key)
            if e is None:
                return None
            if e.expires_at is not None and time.time() > e.expires_at:
                self._store.pop(key, None)
                return None
            return e.value

    def set(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        with self._lock:
            if len(self._store) >= self._max and key not in self._store:
                now = time.time()
                expired = [k for k, v in self._store.items() if v.expires_at and v.expires_at < now]
                for k in expired[: max(1, self._max // 10)]:
                    self._store.pop(k, None)
                if len(self._store) >= self._max:
                    for k in list(self._store.keys())[: max(1, self._max // 20)]:
                        self._store.pop(k, None)
            self._store[key] = _MemEntry(value, ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def ttl(self, key: str) -> int:
        with self._lock:
            e = self._store.get(key)
            if e is None:
                return -2
            if e.expires_at is None:
                return -1
            left = int(e.expires_at - time.time())
            return left if left > 0 else -2


_mem = MemoryTTLCache()
_neon_engine = None
_neon_init = False
_redis = None
_redis_init = False


def _is_durable(key: str) -> bool:
    return any(key.startswith(p) or key == p for p in _DURABLE_PREFIXES)


def _normalize_db_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Neon pooler rejects channel_binding & invalid sslmode=required
    if "channel_binding=" in url:
        url = re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    url = re.sub(r"(?i)([?&]sslmode=)required\b", r"\1require", url)
    if "sslmode=" not in url.lower():
        url = url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


def _neon_url() -> Optional[str]:
    """
    Routes Data Feed & Surprise Feed storage exclusively to CACHE_DATABASE_URL
    if configured, leaving DATABASE_URL / TRAINING_DATABASE_URL strictly for training.
    Prevents data bleeding into the Training Database.
    """
    url = (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("KV_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
    )
    if not url:
        return None
    return _normalize_db_url(url)


_neon_lock = threading.Lock()


def _get_neon():
    """
    Strict singleton Neon engine for stockky_kv.
    Double-checked locking prevents concurrent create_engine() storms
    (Render 512MB + Neon free max connections).
    """
    global _neon_engine, _neon_init
    if _neon_init:
        return _neon_engine
    with _neon_lock:
        if _neon_init:
            return _neon_engine
        _neon_init = True
        url = _neon_url()
        if not url:
            logger.info("KV: no CACHE_DATABASE_URL/DATABASE_URL — memory-only (Redis ignored)")
            return None
        try:
            from sqlalchemy import create_engine, text

            # Free-tier: default pool 1 + overflow 1 (max 2 total). Cap hard at 2.
            # Prefer Neon *pooler* URL (port 6543) in CACHE_DATABASE_URL.
            # With 5 services × max 2 = 10 cluster-wide — stays under Neon free 20.
            pool_size = int(os.getenv("CACHE_DB_POOL_SIZE", "1"))
            max_overflow = int(os.getenv("CACHE_DB_MAX_OVERFLOW", "1"))
            eng = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=max(1, min(pool_size, 2)),
                max_overflow=max(0, min(max_overflow, 1)),
                pool_recycle=int(os.getenv("CACHE_DB_POOL_RECYCLE", "180")),
                pool_use_lifo=True,
                pool_timeout=8,
                connect_args={
                    "connect_timeout": int(os.getenv("CACHE_DB_CONNECT_TIMEOUT", "6")),
                    "application_name": "stockky-kv-cache",
                },
            )
            with eng.begin() as conn:
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS stockky_kv (
                            k TEXT PRIMARY KEY,
                            v TEXT NOT NULL,
                            expires_at TIMESTAMPTZ NULL,
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS stockky_kv_expires_idx ON stockky_kv (expires_at)"
                    )
                )
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS idx_stockky_kv_k ON stockky_kv (k)"
                    )
                )
                # Durable settings tables — NEVER truncated by hard_reset / data-feed wipe
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS stockky_notification (
                            k TEXT PRIMARY KEY,
                            v TEXT NOT NULL,
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        )
                        """
                    )
                )
                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS stockky_watchlist (
                            k TEXT PRIMARY KEY,
                            v TEXT NOT NULL,
                            updated_at TIMESTAMPTZ DEFAULT NOW()
                        )
                        """
                    )
                )
            _neon_engine = eng
            logger.info(
                "KV durable layer: Neon stockky_kv + stockky_notification + stockky_watchlist ready (pool_size=%s max_overflow=%s redis_disabled=%s)",
                pool_size,
                max_overflow,
                not USE_REDIS,
            )
        except Exception as e:
            logger.warning("KV Neon unavailable (memory-only): %s", e)
            _neon_engine = None
        return _neon_engine



def _neon_get(key: str) -> Any:
    eng = _get_neon()
    if not eng:
        return None
    try:
        from sqlalchemy import text
        import datetime as _dt

        with eng.connect() as conn:
            row = conn.execute(
                text("SELECT v, expires_at FROM stockky_kv WHERE k = :k"),
                {"k": key},
            ).fetchone()
            if not row:
                return None
            v, exp = row[0], row[1]
            if exp is not None:
                now = _dt.datetime.now(_dt.timezone.utc)
                if getattr(exp, "tzinfo", None) is None:
                    exp = exp.replace(tzinfo=_dt.timezone.utc)
                if exp < now:
                    with eng.begin() as c2:
                        c2.execute(text("DELETE FROM stockky_kv WHERE k = :k"), {"k": key})
                    return None
            try:
                return json.loads(v)
            except Exception:
                return v
    except Exception as e:
        logger.debug("neon get %s: %s", key, e)
        return None


def _neon_set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    eng = _get_neon()
    if not eng:
        return
    try:
        from sqlalchemy import text
        import datetime as _dt

        payload = json.dumps(value, default=str)
        exp = None
        if ttl:
            exp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=int(ttl))
        with eng.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO stockky_kv (k, v, expires_at, updated_at)
                    VALUES (:k, :v, :e, NOW())
                    ON CONFLICT (k) DO UPDATE
                      SET v = EXCLUDED.v,
                          expires_at = EXCLUDED.expires_at,
                          updated_at = NOW()
                    """
                ),
                {"k": key, "v": payload, "e": exp},
            )
    except Exception as e:
        logger.debug("neon set %s: %s", key, e)


def _get_redis():
    global _redis, _redis_init
    if _redis_init:
        return _redis
    _redis_init = True
    if not USE_REDIS:
        logger.info("KV: USE_REDIS=0 — Upstash disconnected (memory + optional Neon)")
        return None
    url = os.getenv("UPSTASH_REDIS_REST_URL")
    tok = os.getenv("UPSTASH_REDIS_REST_TOKEN")
    if not url or not tok:
        return None
    try:
        from upstash_redis import Redis

        _redis = Redis(url=url, token=tok)
        _redis.ping()
        logger.info("KV: Upstash Redis connected (USE_REDIS=1)")
    except Exception as e:
        logger.warning("KV: Redis unavailable: %s", e)
        _redis = None
    return _redis


def kv_get(key: str) -> Any:
    """Memory → Redis (if USE_REDIS=1) → Neon durable."""
    val = _mem.get(key)
    if val is not None:
        return val
    r = _get_redis()
    if r:
        try:
            raw = r.get(key)
            if raw is not None:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode()
                try:
                    val = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    val = raw
                _mem.set(key, val, ttl=300)
                return val
        except Exception as e:
            logger.debug("redis get %s: %s", key, e)
    if _is_durable(key):
        val = _neon_get(key)
        if val is not None:
            _mem.set(key, val, ttl=600)
            return val
    return None


def kv_set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    """Memory always; Redis only if USE_REDIS=1; Neon for durable prefixes."""
    _mem.set(key, value, ttl=ttl)
    r = _get_redis()
    if r:
        try:
            data = json.dumps(value, default=str)
            if ttl:
                r.setex(key, int(ttl), data)
            else:
                r.set(key, data)
        except Exception as e:
            logger.debug("redis set %s: %s", key, e)
    if _is_durable(key):
        _neon_set(key, value, ttl=ttl)


def kv_delete(key: str) -> None:
    _mem.delete(key)
    r = _get_redis()
    if r:
        try:
            r.delete(key)
        except Exception:
            pass
    if _is_durable(key):
        eng = _get_neon()
        if eng:
            try:
                from sqlalchemy import text

                with eng.begin() as conn:
                    conn.execute(text("DELETE FROM stockky_kv WHERE k = :k"), {"k": key})
            except Exception:
                pass


def kv_ttl(key: str) -> int:
    return _mem.ttl(key)


def kv_set_many(items: dict, ttl: Optional[int] = None) -> None:
    """
    Bulk upsert many key/value pairs in as few Neon round-trips as possible.
    Mirrors kv_get_many. Fixes the data-feed N+1 write storm where a 300-symbol
    feed run was issuing ~1500-1800 sequential single-row transactions
    (one put_symbol → up to 3 key writes + 1 index read/write, each its own
    eng.begin()). This batches everything into a handful of multi-row
    INSERT ... VALUES ... ON CONFLICT statements inside ONE transaction.
    """
    if not items:
        return

    for k, v in items.items():
        _mem.set(k, v, ttl=ttl)

    r = _get_redis()
    if r:
        for k, v in items.items():
            try:
                data = json.dumps(v, default=str)
                if ttl:
                    r.setex(k, int(ttl), data)
                else:
                    r.set(k, data)
            except Exception as e:
                logger.debug("redis set_many %s: %s", k, e)

    durable_items = {k: v for k, v in items.items() if _is_durable(k)}
    if not durable_items:
        return

    eng = _get_neon()
    if not eng:
        return

    try:
        from sqlalchemy import text
        import datetime as _dt

        exp = None
        if ttl:
            exp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=int(ttl))

        rows = [
            {"k": k, "v": json.dumps(v, default=str), "e": exp}
            for k, v in durable_items.items()
        ]

        CHUNK = 200  # keep each statement/param-count reasonable
        with eng.begin() as conn:
            for i in range(0, len(rows), CHUNK):
                chunk = rows[i:i + CHUNK]
                values_sql = ", ".join(
                    f"(:k{j}, :v{j}, :e{j}, NOW())" for j in range(len(chunk))
                )
                params: dict = {}
                for j, row in enumerate(chunk):
                    params[f"k{j}"] = row["k"]
                    params[f"v{j}"] = row["v"]
                    params[f"e{j}"] = row["e"]
                conn.execute(
                    text(
                        f"""
                        INSERT INTO stockky_kv (k, v, expires_at, updated_at)
                        VALUES {values_sql}
                        ON CONFLICT (k) DO UPDATE
                          SET v = EXCLUDED.v,
                              expires_at = EXCLUDED.expires_at,
                              updated_at = NOW()
                        """
                    ),
                    params,
                )
        logger.info("kv_set_many: upserted %s durable keys in %s statement(s)",
                    len(rows), (len(rows) + CHUNK - 1) // CHUNK)
    except Exception as e:
        logger.warning("neon set_many failed (%s items), falling back to per-key sets: %s",
                        len(durable_items), e)
        for k, v in durable_items.items():
            try:
                _neon_set(k, v, ttl=ttl)
            except Exception:
                pass


def kv_get_many(keys: list) -> dict:
    """
    Bulk fetch many keys in as few round-trips as possible.
    Order: memory → (optional Redis) → single Neon IN/ANY query for remaining durable keys.
    Critical for scanning ~300 stocks without N+1 latency (~12s → ~50ms for static feeds).
    """
    if not keys:
        return {}
    result: dict = {}
    missing: list = []

    for k in keys:
        val = _mem.get(k)
        if val is not None:
            result[k] = val
        else:
            missing.append(k)

    if not missing:
        return result

    # Optional Redis path (only when USE_REDIS=1)
    r = _get_redis()
    if r and missing:
        still_missing = []
        for k in missing:
            try:
                raw = r.get(k)
                if raw is not None:
                    if isinstance(raw, (bytes, bytearray)):
                        raw = raw.decode()
                    try:
                        val = json.loads(raw) if isinstance(raw, str) else raw
                    except Exception:
                        val = raw
                    _mem.set(k, val, ttl=300)
                    result[k] = val
                else:
                    still_missing.append(k)
            except Exception:
                still_missing.append(k)
        missing = still_missing

    if not missing:
        return result

    # Single Neon round-trip for remaining durable keys
    eng = _get_neon()
    if eng and missing:
        durable_missing = [k for k in missing if _is_durable(k)]
        if durable_missing:
            try:
                from sqlalchemy import text
                import datetime as _dt

                with eng.connect() as conn:
                    rows = conn.execute(
                        text(
                            "SELECT k, v, expires_at FROM stockky_kv WHERE k = ANY(:keys)"
                        ),
                        {"keys": durable_missing},
                    ).fetchall()
                    now = _dt.datetime.now(_dt.timezone.utc)
                    for row in rows:
                        k, v, exp = row[0], row[1], row[2]
                        if exp is not None:
                            if getattr(exp, "tzinfo", None) is None:
                                exp = exp.replace(tzinfo=_dt.timezone.utc)
                            if exp < now:
                                continue
                        try:
                            val = json.loads(v)
                        except Exception:
                            val = v
                        _mem.set(k, val, ttl=600)
                        result[k] = val
            except Exception as e:
                logger.debug("neon get_many failed: %s", e)
                # Safe fallback: individual gets
                for k in durable_missing:
                    val = _neon_get(k)
                    if val is not None:
                        result[k] = val

    return result


# Module-level API expected by api-gateway: _kv_cache.get / _kv_cache.set
def get(key: str) -> Any:
    return kv_get(key)


def set(key: str, value: Any, ttl: Optional[int] = None) -> None:  # noqa: A001
    kv_set(key, value, ttl=ttl)


def delete(key: str) -> None:
    kv_delete(key)


def set_many(items: dict, ttl: Optional[int] = None) -> None:
    kv_set_many(items, ttl=ttl)


def get_many(keys: list) -> dict:
    """Public bulk API — used by full_market_scan to avoid 300 sequential DB hits."""
    return kv_get_many(keys)


# Back-compat
def cache_get(key: str) -> Any:
    return kv_get(key)


def cache_set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    kv_set(key, value, ttl=ttl)


def status() -> dict:
    """Health snippet for /system/health or Service Manager."""
    neon_ok = False
    neon_err = None
    try:
        eng = _get_neon()
        if eng is not None:
            from sqlalchemy import text

            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            neon_ok = True
    except Exception as e:
        neon_err = str(e)[:120]
    return {
        "use_redis": USE_REDIS,
        "memory_keys": len(_mem._store),
        "neon_connected": neon_ok,
        "neon_error": neon_err,
        "cache_database_configured": bool(_neon_url()),
    }


# Per-symbol data-feed canonical key prefix (kept in sync with data_feed.py's
# DATA_FEED_PREFIX — duplicated here rather than imported to avoid a circular
# import between kv_cache and data_feed).
_SYM_PREFIX = "stockky:data_feed:sym:"

# Fields that change every tick / every day and should NOT be preserved across
# a hard-reset — a stale price is worse than no price, so these get dropped.
_VOLATILE_PRICE_FIELDS = {
    "price", "close", "cmp", "ltp", "last_price", "current_price",
    "day_high", "day_low", "day_change_pct", "previous_close", "volume",
    "prev_close", "price_over_cap", "price_cap",
}
# Never carry these across — they'll be re-set on the next write.
_ALWAYS_DROP_ON_PRESERVE = {"symbol", "updated_at", "source"} | _VOLATILE_PRICE_FIELDS


def hard_reset_stockky_kv(preserve_days: int = 7) -> dict:
    """
    Wipe stockky_kv and re-assert uniqueness + index on k.
    Used by the "Feed Fresh Data" flow so a corrupted / bloated
    universe is nuked before the next full feed rebuild.
    Primary key already enforces uniqueness; we still DROP/ADD the
    named constraint for compatibility with older schemas and
    re-create the supporting index.

    IMPORTANT: a hard-reset used to TRUNCATE every field for every symbol,
    including slow-changing / expensive-to-recompute fields (PE ratio, ROCE,
    sector, technical/fundamental scores, model prediction outputs, etc.)
    that a rate-limited refill can take ~90 minutes to rebuild. Those fields
    are only valid/useful for `preserve_days` anyway (default 7, matching the
    weekly refill cadence), so we now snapshot them before truncating and
    restore them afterwards — ONLY volatile price/volume fields are dropped.
    """
    eng = _get_neon()
    if eng is None:
        # Memory-only mode: clear process cache and report success
        try:
            with _mem._lock:
                _mem._store.clear()
        except Exception:
            pass
        return {
            "status": "success",
            "message": "No Neon configured — cleared in-process memory only.",
            "mode": "memory-only",
        }

    from sqlalchemy import text
    import datetime as _dt

    # k is already PRIMARY KEY (unique). We TRUNCATE, ensure a named UNIQUE
    # constraint exists for older schemas, and re-assert supporting indexes.
    # Protect user settings: notification + watchlist live in dedicated tables
    # and must survive feed hard-reset. Also snapshot legacy keys from stockky_kv
    # before truncate and restore them into the dedicated tables.
    try:
        preserved_count = 0
        with eng.begin() as conn:
            # Ensure settings tables exist
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS stockky_notification (
                    k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS stockky_watchlist (
                    k TEXT PRIMARY KEY, v TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """))
            # Migrate any legacy keys still sitting in stockky_kv
            for legacy_k, table, dest_k in (
                ("stockky:notification_config", "stockky_notification", "config"),
                ("stockky:watchlist", "stockky_watchlist", "default"),
            ):
                try:
                    row = conn.execute(
                        text("SELECT v FROM stockky_kv WHERE k = :k"),
                        {"k": legacy_k},
                    ).fetchone()
                    if row and row[0]:
                        conn.execute(
                            text(
                                f"""
                                INSERT INTO {table} (k, v, updated_at)
                                VALUES (:k, :v, NOW())
                                ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = NOW()
                                """
                            ),
                            {"k": dest_k, "v": row[0]},
                        )
                except Exception as mig_e:
                    logger.debug("hard_reset migrate %s: %s", legacy_k, mig_e)

            # ── Snapshot durable/slow per-symbol fields BEFORE truncating ──
            preserved_rows: list = []
            try:
                cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=preserve_days)
                rows = conn.execute(
                    text(
                        "SELECT k, v FROM stockky_kv "
                        "WHERE k LIKE :prefix AND updated_at >= :cutoff"
                    ),
                    {"prefix": _SYM_PREFIX + "%", "cutoff": cutoff},
                ).fetchall()
                for k, v in rows:
                    try:
                        data = json.loads(v)
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    sym = k[len(_SYM_PREFIX):]
                    keep = {
                        kk: vv for kk, vv in data.items()
                        if kk not in _ALWAYS_DROP_ON_PRESERVE and vv is not None
                    }
                    if keep:
                        keep["symbol"] = sym
                        keep["_preserved_from_reset"] = True
                        preserved_rows.append((sym, keep))
            except Exception as snap_e:
                logger.warning("hard_reset: snapshot of durable fields failed (continuing without preserve): %s", snap_e)
                preserved_rows = []

            # Wipe ONLY stockky_kv (data-feed / scan cache) — never settings tables
            conn.execute(text("TRUNCATE TABLE stockky_kv"))
            try:
                conn.execute(text("ALTER TABLE stockky_kv DROP CONSTRAINT IF EXISTS uq_stockky_kv_k"))
                conn.execute(text("ALTER TABLE stockky_kv ADD CONSTRAINT uq_stockky_kv_k UNIQUE (k)"))
            except Exception as e:
                logger.debug("hard_reset constraint: %s", e)
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_stockky_kv_k ON stockky_kv (k)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS stockky_kv_expires_idx ON stockky_kv (expires_at)"))

            # ── Restore preserved durable fields AFTER truncating ──
            # These land back under the canonical key so the next put_symbol's
            # merge-never-wipe logic (merge_feed_payload) treats them as
            # "existing" data and layers fresh price on top instead of losing
            # a week's worth of PE/ROCE/model-prediction hydration work.
            if preserved_rows:
                try:
                    exp = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=preserve_days)
                    CHUNK = 200
                    for i in range(0, len(preserved_rows), CHUNK):
                        chunk = preserved_rows[i:i + CHUNK]
                        values_sql = ", ".join(
                            f"(:k{j}, :v{j}, :e{j}, NOW())" for j in range(len(chunk))
                        )
                        params: dict = {}
                        for j, (sym, keep) in enumerate(chunk):
                            params[f"k{j}"] = _SYM_PREFIX + sym
                            params[f"v{j}"] = json.dumps(keep, default=str)
                            params[f"e{j}"] = exp
                        conn.execute(
                            text(
                                f"""
                                INSERT INTO stockky_kv (k, v, expires_at, updated_at)
                                VALUES {values_sql}
                                ON CONFLICT (k) DO UPDATE
                                  SET v = EXCLUDED.v,
                                      expires_at = EXCLUDED.expires_at,
                                      updated_at = NOW()
                                """
                            ),
                            params,
                        )
                    preserved_count = len(preserved_rows)
                except Exception as restore_e:
                    logger.warning("hard_reset: restore of durable fields failed: %s", restore_e)
                    preserved_count = 0

        # Clear process memory EXCEPT protected settings keys
        try:
            protect = {"stockky:notification_config", "stockky:watchlist"}
            with _mem._lock:
                for k in list(_mem._store.keys()):
                    if k not in protect and not k.startswith("stockky:notification"):
                        _mem._store.pop(k, None)
        except Exception:
            pass
        logger.info(
            "hard_reset_stockky_kv: stockky_kv truncated; notification+watchlist tables preserved; "
            "durable fields restored for %s symbols (preserve_days=%s)",
            preserved_count, preserve_days,
        )
        return {
            "status": "success",
            "message": (
                f"Data-feed cache wiped. Notification settings and watchlist preserved. "
                f"Durable fields (PE/ROCE/scores/model predictions) restored for {preserved_count} symbols."
            ),
            "mode": "neon",
            "preserved": ["stockky_notification", "stockky_watchlist"],
            "preserved_symbol_fields_count": preserved_count,
            "preserve_days": preserve_days,
        }
    except Exception as e:
        logger.exception("hard_reset_stockky_kv failed: %s", e)
        return {
            "status": "error",
            "message": str(e)[:240],
            "mode": "neon",
        }


# ── Durable settings tables (never hard-reset) ─────────────────────────────
# stockky_notification  — bot tokens, chat ids, channel config
# stockky_watchlist     — user watchlist symbols
# Only explicit DELETE from the frontend/API removes these.

_SETTINGS_MEM: dict = {}
_SETTINGS_LOCK = threading.RLock()


def _settings_table_ok(table: str) -> str:
    allowed = {"stockky_notification", "stockky_watchlist"}
    if table not in allowed:
        raise ValueError(f"settings table not allowed: {table}")
    return table


def settings_get(table: str, key: str = "default") -> Any:
    """Read from dedicated durable table (no TTL expiry)."""
    table = _settings_table_ok(table)
    mk = f"{table}:{key}"
    with _SETTINGS_LOCK:
        if mk in _SETTINGS_MEM:
            return _SETTINGS_MEM[mk]
    eng = _get_neon()
    if eng is None:
        return None
    try:
        from sqlalchemy import text
        with eng.connect() as conn:
            row = conn.execute(
                text(f"SELECT v FROM {table} WHERE k = :k"),
                {"k": key},
            ).fetchone()
            if not row:
                return None
            raw = row[0]
            try:
                val = json.loads(raw) if isinstance(raw, str) else raw
            except Exception:
                val = raw
            with _SETTINGS_LOCK:
                _SETTINGS_MEM[mk] = val
            return val
    except Exception as e:
        logger.warning("settings_get %s/%s: %s", table, key, e)
        return None


def settings_set(table: str, key: str, value: Any) -> bool:
    """Upsert into dedicated durable table (no expiry). Survives hard_reset."""
    table = _settings_table_ok(table)
    mk = f"{table}:{key}"
    try:
        payload = json.dumps(value) if not isinstance(value, str) else value
    except Exception:
        payload = json.dumps(value, default=str)
    with _SETTINGS_LOCK:
        try:
            _SETTINGS_MEM[mk] = json.loads(payload) if isinstance(value, (dict, list)) else value
        except Exception:
            _SETTINGS_MEM[mk] = value
    eng = _get_neon()
    if eng is None:
        logger.warning("settings_set: no Neon — memory only for %s/%s", table, key)
        return True
    try:
        from sqlalchemy import text
        with eng.begin() as conn:
            conn.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        k TEXT PRIMARY KEY,
                        v TEXT NOT NULL,
                        updated_at TIMESTAMPTZ DEFAULT NOW()
                    )
                    """
                )
            )
            conn.execute(
                text(
                    f"""
                    INSERT INTO {table} (k, v, updated_at)
                    VALUES (:k, :v, NOW())
                    ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, updated_at = NOW()
                    """
                ),
                {"k": key, "v": payload},
            )
        return True
    except Exception as e:
        logger.error("settings_set %s/%s failed: %s", table, key, e)
        return False


def settings_delete(table: str, key: str = "default") -> bool:
    """Explicit delete only (frontend clear). Never called by hard_reset."""
    table = _settings_table_ok(table)
    mk = f"{table}:{key}"
    with _SETTINGS_LOCK:
        _SETTINGS_MEM.pop(mk, None)
    eng = _get_neon()
    if eng is None:
        return True
    try:
        from sqlalchemy import text
        with eng.begin() as conn:
            conn.execute(text(f"DELETE FROM {table} WHERE k = :k"), {"k": key})
        return True
    except Exception as e:
        logger.error("settings_delete %s/%s: %s", table, key, e)
        return False


def notification_config_get() -> Any:
    """Prefer dedicated table; migrate from legacy stockky_kv key once."""
    val = settings_get("stockky_notification", "config")
    if val is not None:
        return val
    # One-time migrate from legacy key in stockky_kv
    legacy = kv_get("stockky:notification_config")
    if legacy is not None:
        settings_set("stockky_notification", "config", legacy)
        return legacy
    return None


def notification_config_set(cfg: Any) -> bool:
    ok = settings_set("stockky_notification", "config", cfg)
    # Also mirror to legacy key for older readers (optional, still durable until hard_reset)
    try:
        kv_set("stockky:notification_config", cfg, ttl=None)
    except Exception:
        pass
    return ok


def notification_config_delete() -> bool:
    try:
        kv_delete("stockky:notification_config")
    except Exception:
        pass
    return settings_delete("stockky_notification", "config")


def watchlist_get() -> Any:
    val = settings_get("stockky_watchlist", "default")
    if val is not None:
        return val
    legacy = kv_get("stockky:watchlist")
    if legacy is not None:
        settings_set("stockky_watchlist", "default", legacy)
        return legacy
    return None


def watchlist_set(symbols: Any) -> bool:
    ok = settings_set("stockky_watchlist", "default", symbols)
    try:
        kv_set("stockky:watchlist", symbols, ttl=None)
    except Exception:
        pass
    return ok


def watchlist_delete() -> bool:
    try:
        kv_delete("stockky:watchlist")
    except Exception:
        pass
    return settings_delete("stockky_watchlist", "default")
