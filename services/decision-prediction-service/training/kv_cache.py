"""
Stockky KV cache — Redis-free by default.

Layers (fast → durable):
  1. In-process memory with TTL  (always on — free, unlimited "commands")
  2. Optional Neon/Postgres table `stockky_kv` for keys that must survive restarts
     (watchlist, data-feed, scan universe). Use CACHE_DATABASE_URL or DATABASE_URL.
  3. Optional Upstash Redis only if USE_REDIS=1 (legacy / multi-instance).

Env:
  USE_REDIS=0|1          default 0 — disconnect Upstash
  CACHE_DATABASE_URL     optional dedicated Neon for cache (else DATABASE_URL / TRAINING_DATABASE_URL)
  KV_MEMORY_MAX_KEYS     default 8000 — soft cap for free 512MB dynos
"""
from __future__ import annotations

import json
import logging
import os
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


# Keys that should also land in Neon so a Render restart does not wipe them
_DURABLE_PREFIXES = (
    "stockky:watchlist",
    "stockky:searched",
    "stockky:scan_universe",
    "stockky:known_symbols",
    "stockky:data_feed",
    "stockky:hot_job",
    "stockky:hot_result",
    "stockky:last_full_scan",
    "stockky:lock:",
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
                # Drop ~10% oldest-ish (simple: random sample of expired + first keys)
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


def _neon_url() -> Optional[str]:
    url = (
        os.getenv("CACHE_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or os.getenv("TRAINING_DATABASE_URL")
    )
    if not url:
        return None
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql") and "sslmode=" not in url.lower():
        url += ("&" if "?" in url else "?") + "sslmode=require"
    if "channel_binding=" in url:
        import re as _re
        url = _re.sub(r"([&?])channel_binding=[^&]*", r"\1", url)
        url = url.replace("?&", "?").rstrip("?&")
    return url


def _get_neon():
    global _neon_engine, _neon_init
    if _neon_init:
        return _neon_engine
    _neon_init = True
    url = _neon_url()
    if not url:
        return None
    try:
        from sqlalchemy import create_engine, text
        eng = create_engine(
            url,
            pool_pre_ping=True,
            pool_size=int(os.getenv("CACHE_DB_POOL_SIZE", "2")),
            max_overflow=0,
            pool_recycle=280,
        )
        with eng.begin() as conn:
            conn.execute(text(
                """
                CREATE TABLE IF NOT EXISTS stockky_kv (
                    k TEXT PRIMARY KEY,
                    v TEXT NOT NULL,
                    expires_at TIMESTAMPTZ NULL
                )
                """
            ))
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS stockky_kv_expires_idx ON stockky_kv (expires_at)"
            ))
        _neon_engine = eng
        logger.info("KV durable layer: Neon/Postgres stockky_kv ready")
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
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT v, expires_at FROM stockky_kv WHERE k = :k"
                ),
                {"k": key},
            ).fetchone()
            if not row:
                return None
            v, exp = row[0], row[1]
            if exp is not None:
                import datetime as _dt
                now = _dt.datetime.now(_dt.timezone.utc)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=_dt.timezone.utc)
                if exp < now:
                    conn.execute(text("DELETE FROM stockky_kv WHERE k = :k"), {"k": key})
                    conn.commit()
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
                    INSERT INTO stockky_kv (k, v, expires_at)
                    VALUES (:k, :v, :e)
                    ON CONFLICT (k) DO UPDATE SET v = EXCLUDED.v, expires_at = EXCLUDED.expires_at
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
    """Memory first → Redis (if enabled) → Neon durable."""
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
    """Write memory always; Redis only if USE_REDIS=1; Neon for durable keys."""
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


def kv_ttl(key: str) -> int:
    t = _mem.ttl(key)
    if t != -2:
        return t
    return -2


def kv_get_many(keys: list) -> dict:
    """Bulk fetch — memory first, then optional Redis, then single Neon ANY query."""
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
    r = _get_redis()
    if r and missing:
        still = []
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
                    still.append(k)
            except Exception:
                still.append(k)
        missing = still
    if not missing:
        return result
    durable = [k for k in missing if _is_durable(k)]
    for k in durable:
        val = _neon_get(k)
        if val is not None:
            result[k] = val
    return result


def get(key: str) -> Any:
    return kv_get(key)


def set(key: str, value: Any, ttl: Optional[int] = None) -> None:  # noqa: A001
    kv_set(key, value, ttl=ttl)


def get_many(keys: list) -> dict:
    return kv_get_many(keys)


# Back-compat aliases used by older modules
def cache_get(key: str) -> Any:
    return kv_get(key)


def cache_set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    kv_set(key, value, ttl=ttl)

