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


def _now_iso() -> str:
    return datetime.now(IST).isoformat()


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
        "symbol": symbol.upper().replace(".NS", "").replace(".BO", ""),
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
        "earnings_surprise": e.get("earnings_surprise") if isinstance(e, dict) else None,
        "event_summary": e.get("summary") if isinstance(e, dict) else None,
        "events_count": e.get("count") or e.get("total") if isinstance(e, dict) else None,
    }
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in payload and v is not None:
                payload[k] = v
    return payload


class DataFeedStore:
    """
    Durable data-feed store.

    _get / _set must point at kv_cache (memory + Neon). Redis is optional
    and disabled when USE_REDIS=0.
    """

    def __init__(self, redis_get, redis_set, redis_client=None):
        self._get = redis_get
        self._set = redis_set
        self._redis = redis_client  # legacy; may be None

    # ── Symbol payload ──────────────────────────────────────────────────
    def get_symbol(self, symbol: str) -> Optional[dict]:
        key = DATA_FEED_PREFIX + (symbol or "").upper().replace(".NS", "").replace(".BO", "")
        if key in _LOCAL_SYMBOLS:
            return dict(_LOCAL_SYMBOLS[key])
        val = self._get(key)
        if isinstance(val, dict):
            _LOCAL_SYMBOLS[key] = val
            base = key.split(":")[-1]
            _LOCAL_INDEX.add(base)
            return dict(val)
        return None

    def put_symbol(self, symbol: str, payload: dict, ttl: int = DATA_FEED_TTL) -> None:
        base = (symbol or "").upper().replace(".NS", "").replace(".BO", "")
        key = DATA_FEED_PREFIX + base
        if not isinstance(payload, dict):
            return
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
        # Update durable index periodically (every put is fine; small list)
        try:
            self._persist_index(ttl=ttl)
        except Exception as e:
            logger.debug("data_feed index persist: %s", e)

    def has_symbol(self, symbol: str) -> bool:
        return self.get_symbol(symbol) is not None

    def list_symbols(self) -> List[str]:
        """Symbols currently in feed (local ∪ durable index)."""
        idx = set(_LOCAL_INDEX)
        try:
            durable = self._get(DATA_FEED_INDEX_KEY)
            if isinstance(durable, list):
                idx.update(str(s).upper() for s in durable)
            elif isinstance(durable, dict) and isinstance(durable.get("symbols"), list):
                idx.update(str(s).upper() for s in durable["symbols"])
        except Exception:
            pass
        return sorted(idx)

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
                # best-effort: use index updated_at
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


# ── Hot-picks job (on-demand) ─────────────────────────────────────────────
HOT_JOB_KEY = "stockky:hot_job"
HOT_RESULT_KEY = "stockky:hot_result_db"


def hot_job_get(redis_get) -> dict:
    j = redis_get(HOT_JOB_KEY)
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
    redis_set(HOT_JOB_KEY, j, ttl=86400)
    return j


# ── Cache stampede protection (memory lock when Redis off) ────────────────
LOCK_PREFIX = "stockky:lock:refresh:"
_MEM_LOCKS: Dict[str, float] = {}


def try_refresh_lock(redis_client, symbol: str, ttl_sec: int = 5) -> bool:
    key = f"{LOCK_PREFIX}{(symbol or '').upper()}"
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
    key = f"{LOCK_PREFIX}{(symbol or '').upper()}"
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
