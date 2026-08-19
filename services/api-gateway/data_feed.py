"""
Data Feed — durable cache of slow-changing fields (12–24h).

Purpose: free-tier rate-limit relief. Real-time paths (quote, decide, scan)
reuse fundamentals / sector / peer / multi-quarter / static event snapshot
from this store instead of hitting upstream APIs every time.

Persistence (required):
  - Every symbol payload → Neon via kv_cache (prefix stockky:data_feed:)
  - Meta + job status → Neon so UI survives Render cold starts
  - Symbol index → Neon for "STOCKS IN FEED" count without scanning all keys

NOT stored here (always live when needed):
  - last price / quote
  - intraday technicals
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Set
import threading

logger = logging.getLogger("data-feed")

IST = timezone(timedelta(hours=5, minutes=30))

DATA_FEED_PREFIX = "stockky:data_feed:sym:"
DATA_FEED_META_KEY = "stockky:data_feed:meta"
DATA_FEED_JOB_KEY = "stockky:data_feed:job"
DATA_FEED_INDEX_KEY = "stockky:data_feed:index"  # list of symbols currently in feed
# Default 24h — long enough for full trading day + overnight; midnight scheduler refreshes
DATA_FEED_TTL = int(os.getenv("DATA_FEED_TTL_SECONDS", str(24 * 3600)))

# Process-local hot cache (speed). Durable source of truth is Neon via _get/_set.
_LOCAL_SYMBOLS: Dict[str, dict] = {}
_LOCAL_META: Dict[str, Any] = {}
_LOCAL_JOB: Dict[str, Any] = {}
_LOCAL_INDEX: Set[str] = set()
_INDEX_WARMED = False
_INDEX_WARM_LOCK = threading.Lock()

# Hard stop: process-local flag so Stop is immediate (does not wait for next Neon read)
_DATA_FEED_STOP_FLAG = threading.Event()


def request_data_feed_stop() -> None:
    """Called by /data-feed/stop — worker checks this every symbol."""
    _DATA_FEED_STOP_FLAG.set()


def clear_data_feed_stop() -> None:
    """Called when starting/resuming a feed run."""
    _DATA_FEED_STOP_FLAG.clear()


def data_feed_stop_requested() -> bool:
    return _DATA_FEED_STOP_FLAG.is_set()


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


def _norm_sym(symbol: str) -> str:
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def extract_feed_payload(
    symbol: str,
    fundamental: Optional[dict] = None,
    events: Optional[dict] = None,
    extra: Optional[dict] = None,
) -> Dict[str, Any]:
    """Normalize slow fields from upstream responses."""
    f = fundamental if isinstance(fundamental, dict) else {}
    e = events if isinstance(events, dict) else {}
    metrics = f.get("metrics") if isinstance(f.get("metrics"), dict) else {}
    payload = {
        "symbol": _norm_sym(symbol),
        "updated_at": _now_iso(),
        "fundamental_score": f.get("fundamental_score"),
        "valuation": f.get("valuation"),
        "sector": f.get("sector"),
        "industry": f.get("industry"),
        "peer_relative_score": f.get("peer_relative_score"),
        "peer_relative": f.get("peer_relative"),
        "peer_list": f.get("peer_list"),
        "multi_quarter_score": f.get("multi_quarter_score"),
        "multi_quarter_ok": f.get("multi_quarter_ok"),
        "multi_quarter_detail": f.get("multi_quarter_detail"),
        "quality_score": f.get("quality_score"),
        "metrics": metrics,
        "fundamental_reasons": (f.get("reasons") or [])[:6],
        "fallback_used": f.get("fallback_used"),
        "bulk_deals": (e.get("bulk_deals") or e.get("bulk") or [])[:5] if isinstance(e, dict) else [],
        "insider": (e.get("insider") or e.get("insider_trades") or [])[:5] if isinstance(e, dict) else [],
        "recent_insider_transactions": (
            e.get("recent_insider_transactions") or e.get("insider") or e.get("insider_trades") or []
        )[:5]
        if isinstance(e, dict)
        else [],
        "earnings_surprise": e.get("earnings_surprise") if isinstance(e, dict) else None,
        "next_earnings_date": e.get("next_earnings_date") if isinstance(e, dict) else None,
        "event_summary": (e.get("summary") or e.get("event_summary")) if isinstance(e, dict) else None,
        "events_count": e.get("count") or e.get("total") if isinstance(e, dict) else None,
        "has_positive_catalyst": e.get("has_positive_catalyst") if isinstance(e, dict) else None,
        "recent_event_score": e.get("recent_event_score") if isinstance(e, dict) else None,
    }
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in payload and v is not None:
                payload[k] = v
    return payload


def _payload_is_useful(payload: Optional[dict]) -> bool:
    """True when feed row has any slow field worth short-circuiting upstream."""
    if not isinstance(payload, dict):
        return False
    return bool(
        payload.get("fundamental_score") is not None
        or payload.get("metrics")
        or payload.get("sector")
        or payload.get("valuation")
        or payload.get("quality_score") is not None
        or payload.get("multi_quarter_score") is not None
        or payload.get("event_summary")
        or payload.get("bulk_deals")
        or payload.get("recent_insider_transactions")
        or payload.get("earnings_surprise") is not None
        or payload.get("next_earnings_date")
    )


class DataFeedStore:
    """
    Durable data-feed store.

    _get / _set must point at kv_cache (memory + Neon). Redis is optional
    and disabled when USE_REDIS=0.

    Read path (cold-start safe):
      1. process-local dict
      2. kv_cache.get → memory → (optional Redis) → Neon stockky_kv
    Write path always hits Neon for durable prefixes.
    """

    def __init__(self, redis_get, redis_set, redis_client=None):
        self._get = redis_get
        self._set = redis_set
        self._redis = redis_client  # legacy; may be None

    def warm(self) -> None:
        """Load index/meta/job from Neon into process-local caches (call once after boot)."""
        global _INDEX_WARMED
        with _INDEX_WARM_LOCK:
            try:
                self.meta()
                self.job()
                self.list_symbols()
                _INDEX_WARMED = True
                logger.info(
                    "data_feed warm: index=%s meta_count=%s",
                    len(_LOCAL_INDEX),
                    (self.meta() or {}).get("last_count"),
                )
            except Exception as e:
                logger.warning("data_feed warm failed: %s", e)

    # ── Symbol payload ──────────────────────────────────────────────────
    def get_symbol(self, symbol: str) -> Optional[dict]:
        """
        Prefer local cache; on miss always read Neon (via kv_cache).
        Never treat an empty local dict as authoritative when Neon has data.
        """
        base = _norm_sym(symbol)
        if not base:
            return None
        key = DATA_FEED_PREFIX + base

        # 1) Local hit only if payload looks useful
        local = _LOCAL_SYMBOLS.get(key)
        if isinstance(local, dict) and _payload_is_useful(local):
            return dict(local)

        # 2) Durable read (Neon via kv_cache)
        try:
            val = self._get(key)
        except Exception as e:
            logger.debug("data_feed get_symbol neon %s: %s", base, e)
            val = None

        if isinstance(val, dict) and val:
            _LOCAL_SYMBOLS[key] = val
            _LOCAL_INDEX.add(base)
            return dict(val)

        # Keep empty local miss so we don't thrash; still return None
        return None

    def get_symbols_bulk(self, symbols: List[str]) -> Dict[str, dict]:
        """
        Bulk-load many symbol feeds in one Neon round-trip via kv_cache.get_many.
        Populates the process-local cache so subsequent get_symbol() hits are free.
        Returns mapping of base_symbol → payload (only keys that had data).
        """
        if not symbols:
            return {}
        result: Dict[str, dict] = {}
        missing_keys: List[str] = []
        key_to_base: Dict[str, str] = {}

        for sym in symbols:
            base = _norm_sym(sym)
            if not base:
                continue
            key = DATA_FEED_PREFIX + base
            local = _LOCAL_SYMBOLS.get(key)
            if isinstance(local, dict) and _payload_is_useful(local):
                result[base] = dict(local)
            else:
                missing_keys.append(key)
                key_to_base[key] = base

        if not missing_keys:
            return result

        # Prefer kv_cache.get_many when available (single Neon ANY query)
        bulk: Dict[str, Any] = {}
        try:
            import kv_cache as _kc
            if hasattr(_kc, "get_many"):
                bulk = _kc.get_many(missing_keys) or {}
            else:
                for k in missing_keys:
                    try:
                        v = self._get(k)
                        if v is not None:
                            bulk[k] = v
                    except Exception:
                        pass
        except Exception as e:
            logger.debug("get_symbols_bulk kv_cache path: %s", e)
            for k in missing_keys:
                try:
                    v = self._get(k)
                    if v is not None:
                        bulk[k] = v
                except Exception:
                    pass

        for key, val in bulk.items():
            if not isinstance(val, dict) or not val:
                continue
            base = key_to_base.get(key) or key.replace(DATA_FEED_PREFIX, "")
            _LOCAL_SYMBOLS[key] = val
            _LOCAL_INDEX.add(base)
            result[base] = dict(val)

        return result

    def put_symbol(self, symbol: str, payload: dict, ttl: int = DATA_FEED_TTL) -> None:
        base = _norm_sym(symbol)
        if not base or not isinstance(payload, dict):
            return
        key = DATA_FEED_PREFIX + base
        payload = dict(payload)
        payload.setdefault("symbol", base)
        payload.setdefault("updated_at", _now_iso())
        _LOCAL_SYMBOLS[key] = payload
        _LOCAL_INDEX.add(base)
        # Durable write (Neon via kv_cache for stockky:data_feed:*)
        try:
            self._set(key, payload, ttl=ttl)
        except Exception as e:
            logger.warning("data_feed put_symbol durable fail %s: %s", base, e)
        # Update durable index (batched cheaply every put — list is small)
        try:
            self._persist_index(ttl=ttl)
        except Exception as e:
            logger.debug("data_feed index persist: %s", e)

    def has_symbol(self, symbol: str) -> bool:
        return self.get_symbol(symbol) is not None

    def list_symbols(self) -> List[str]:
        """Symbols currently in feed (local ∪ durable index from Neon)."""
        global _INDEX_WARMED
        idx = set(_LOCAL_INDEX)
        try:
            durable = self._get(DATA_FEED_INDEX_KEY)
            if isinstance(durable, list):
                idx.update(_norm_sym(s) for s in durable if s)
            elif isinstance(durable, dict):
                syms = durable.get("symbols")
                if isinstance(syms, list):
                    idx.update(_norm_sym(s) for s in syms if s)
            _INDEX_WARMED = True
        except Exception as e:
            logger.debug("data_feed list_symbols durable: %s", e)
        _LOCAL_INDEX.update(idx)
        return sorted(s for s in idx if s)

    def count_symbols(self) -> int:
        return len(self.list_symbols())

    def _persist_index(self, ttl: int = DATA_FEED_TTL) -> None:
        symbols = self.list_symbols()
        payload = {
            "symbols": symbols,
            "count": len(symbols),
            "updated_at": _now_iso(),
        }
        self._set(DATA_FEED_INDEX_KEY, payload, ttl=ttl)

    # ── Meta (STOCKS IN FEED / LAST SUCCESS) ─────────────────────────────
    def meta(self) -> dict:
        durable = None
        try:
            durable = self._get(DATA_FEED_META_KEY)
        except Exception:
            durable = None
        if not isinstance(durable, dict):
            durable = {}
        # Local overrides only when it has real progress (running or newer)
        local = dict(_LOCAL_META) if _LOCAL_META else {}
        m = {**durable, **local} if local else dict(durable)
        if not m:
            m = {
                "last_success_at": None,
                "last_count": 0,
                "last_message": "No data feed run yet",
                "source": None,
            }
        # Heal last_count from index if meta is empty/stale zero but index has symbols
        try:
            cnt = self.count_symbols()
            if cnt > 0 and int(m.get("last_count") or 0) < cnt:
                m["last_count"] = cnt
            if cnt > 0 and not m.get("last_success_at"):
                idx = self._get(DATA_FEED_INDEX_KEY)
                if isinstance(idx, dict) and idx.get("updated_at"):
                    m["last_success_at"] = idx["updated_at"]
        except Exception:
            pass
        return m

    def set_meta(self, **kwargs) -> dict:
        m = self.meta()
        m.update(kwargs)
        m["updated_at"] = _now_iso()
        # Keep stocks count honest
        try:
            cnt = self.count_symbols()
            if cnt > int(m.get("last_count") or 0):
                m["last_count"] = cnt
        except Exception:
            pass
        _LOCAL_META.clear()
        _LOCAL_META.update(m)
        try:
            self._set(DATA_FEED_META_KEY, m, ttl=7 * 86400)  # meta survives a week
        except Exception as e:
            logger.warning("data_feed set_meta durable fail: %s", e)
        return m

    # ── Job (progress UI) ────────────────────────────────────────────────
    def job(self) -> dict:
        durable = None
        try:
            durable = self._get(DATA_FEED_JOB_KEY)
        except Exception:
            durable = None
        if not isinstance(durable, dict):
            durable = {}
        local = dict(_LOCAL_JOB) if _LOCAL_JOB else {}
        # Prefer local when actively running
        if local.get("status") == "running":
            j = {**durable, **local}
        elif local:
            j = {**durable, **local}
        else:
            j = dict(durable) if durable else {}
        if not j:
            j = {
                "status": "idle",
                "processed": 0,
                "total": 0,
                "started_at": None,
                "elapsed_sec": 0,
                "estimated_remaining_sec": None,
                "message": "Idle",
                "ok_count": 0,
            }
        return j

    def set_job(self, **kwargs) -> dict:
        j = self.job()
        j.update(kwargs)
        if j.get("started_at") and j.get("status") == "running":
            try:
                started = datetime.fromisoformat(str(j["started_at"]))
                if started.tzinfo is None:
                    started = started.replace(tzinfo=IST)
                j["elapsed_sec"] = int((datetime.now(IST) - started).total_seconds())
                done = int(j.get("processed") or 0)
                total = max(int(j.get("total") or 1), 1)
                if done > 0:
                    rate = j["elapsed_sec"] / done
                    j["estimated_remaining_sec"] = int(rate * (total - done))
            except Exception:
                pass
        j["updated_at"] = _now_iso()
        _LOCAL_JOB.clear()
        _LOCAL_JOB.update(j)
        try:
            self._set(DATA_FEED_JOB_KEY, j, ttl=7 * 86400)
        except Exception as e:
            logger.warning("data_feed set_job durable fail: %s", e)
        # Keep meta in sync for UI cards during / after run
        try:
            ok_n = int(j.get("ok_count") or j.get("processed") or 0)
            if ok_n > 0 or j.get("status") in ("done", "stopped", "error", "idle"):
                meta_kw = {
                    "last_count": max(ok_n, int(self.meta().get("last_count") or 0)),
                    "last_message": j.get("message") or self.meta().get("last_message"),
                    "source": "job_progress",
                }
                if j.get("status") in ("done", "stopped"):
                    meta_kw["last_success_at"] = j.get("finished_at") or j.get("updated_at") or _now_iso()
                elif ok_n > 0:
                    meta_kw["last_success_at"] = j.get("updated_at") or _now_iso()
                self.set_meta(**meta_kw)
        except Exception:
            pass
        return j


# ── Hot-picks job (on-demand) — Neon durable via kv_cache prefixes ─────────
HOT_JOB_KEY = "stockky:hot_job"
HOT_RESULT_KEY = "stockky:hot_result_db"


def hot_job_get(redis_get) -> dict:
    try:
        j = redis_get(HOT_JOB_KEY)
    except Exception:
        j = None
    return j if isinstance(j, dict) else {
        "status": "idle",
        "processed": 0,
        "total": 0,
        "started_at": None,
        "elapsed_sec": 0,
        "estimated_remaining_sec": None,
        "message": "Idle — click Search Hot Picks Stocks",
    }


def hot_job_set(redis_set, redis_get, **kwargs) -> dict:
    j = hot_job_get(redis_get)
    j.update(kwargs)
    if j.get("started_at") and j.get("status") == "running":
        try:
            started = datetime.fromisoformat(str(j["started_at"]))
            if started.tzinfo is None:
                started = started.replace(tzinfo=IST)
            j["elapsed_sec"] = int((datetime.now(IST) - started).total_seconds())
            done = int(j.get("processed") or 0)
            total = max(int(j.get("total") or 1), 1)
            if done > 0:
                j["estimated_remaining_sec"] = int((j["elapsed_sec"] / done) * (total - done))
        except Exception:
            pass
    j["updated_at"] = _now_iso()
    # Durable: kv_cache routes stockky:hot_job → Neon
    try:
        redis_set(HOT_JOB_KEY, j, ttl=7 * 86400)
    except Exception as e:
        logger.warning("hot_job_set durable fail: %s", e)
    return j


def hot_result_get(redis_get) -> Optional[dict]:
    """Load last hot-picks result from Neon/memory."""
    try:
        val = redis_get(HOT_RESULT_KEY)
        if isinstance(val, dict):
            return val
        # legacy key
        val2 = redis_get("stockky:hot_result")
        return val2 if isinstance(val2, dict) else None
    except Exception as e:
        logger.debug("hot_result_get: %s", e)
        return None


def hot_result_set(redis_set, payload: dict, ttl: int = 7 * 86400) -> None:
    if not isinstance(payload, dict):
        return
    payload = dict(payload)
    payload.setdefault("persisted_at", _now_iso())
    try:
        redis_set(HOT_RESULT_KEY, payload, ttl=ttl)
        # also mirror to short key for older readers
        redis_set("stockky:hot_result", payload, ttl=ttl)
    except Exception as e:
        logger.warning("hot_result_set durable fail: %s", e)


# ── Cache stampede protection (memory lock when Redis off) ────────────────
LOCK_PREFIX = "stockky:lock:refresh:"
_MEM_LOCKS: Dict[str, float] = {}


def try_refresh_lock(redis_client, symbol: str, ttl_sec: int = 5) -> bool:
    key = f"{LOCK_PREFIX}{_norm_sym(symbol)}"
    now = datetime.now(IST).timestamp()
    # Always use process lock first
    exp = _MEM_LOCKS.get(key)
    if exp and exp > now:
        return False
    _MEM_LOCKS[key] = now + ttl_sec
    if redis_client is None:
        return True
    try:
        ok = redis_client.set(key, "1", nx=True, ex=int(ttl_sec))
        return bool(ok)
    except TypeError:
        try:
            ok = redis_client.set(key, "1", ex=ttl_sec, nx=True)
            return bool(ok)
        except Exception:
            return True
    except Exception:
        return True


def release_refresh_lock(redis_client, symbol: str) -> None:
    key = f"{LOCK_PREFIX}{_norm_sym(symbol)}"
    _MEM_LOCKS.pop(key, None)
    if redis_client is None:
        return
    try:
        redis_client.delete(key)
    except Exception:
        pass


def soft_ttl_should_refresh(redis_client, key: str, soft_window: int = 10) -> bool:
    if redis_client is None:
        return False
    try:
        ttl = redis_client.ttl(key)
        return isinstance(ttl, int) and 0 < ttl <= soft_window
    except Exception:
        return False
