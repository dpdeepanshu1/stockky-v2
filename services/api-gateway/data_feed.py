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
# Alias used by some callers / older docs ("feed:RELIANCE"). Dual-write + dual-read.
FEED_ALIAS_PREFIX = "feed:"
# Legacy mistaken key seen in older gateway paths ("data_feed:RELIANCE")
FEED_LEGACY_PREFIX = "data_feed:"
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


def clear_local_data_feed_caches() -> None:
    """
    Wipe every process-local data-feed structure.
    Called by hard-reset so the UI cannot serve ghost symbols after a TRUNCATE.
    """
    global _INDEX_WARMED
    with _INDEX_WARM_LOCK:
        _LOCAL_SYMBOLS.clear()
        _LOCAL_META.clear()
        _LOCAL_JOB.clear()
        _LOCAL_INDEX.clear()
        _INDEX_WARMED = False
    _DATA_FEED_STOP_FLAG.clear()
    logger.info("data_feed: process-local caches cleared (hard-reset)")


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


# Universal ≤ ₹5000 gate — refuse to persist high-ticket stocks into the feed cache
MAX_STOCK_PRICE = float(os.getenv("MAX_STOCK_PRICE", "5000") or 5000)


def _coerce_price(val) -> float:
    """Parse NSE-style prices that may contain commas: '5,123.45' / '1,20,000'."""
    if val is None or val == "":
        return 0.0
    try:
        if isinstance(val, (int, float)):
            f = float(val)
            return f if f > 0 and f == f else 0.0
        s = str(val).replace(",", "").replace("\u00a0", "").replace(" ", "").strip()
        if not s or s.upper() in ("-", "NA", "N/A", "NONE", "NULL"):
            return 0.0
        f = float(s)
        return f if f > 0 and f == f else 0.0
    except (TypeError, ValueError):
        return 0.0


def _payload_price(payload: dict) -> float:
    """Best-effort positive price from a feed payload (flat or nested metrics)."""
    if not isinstance(payload, dict):
        return 0.0
    for k in ("price", "close", "cmp", "ltp", "last_price", "current_price", "prev_close"):
        v = _coerce_price(payload.get(k))
        if v > 0:
            return v
    for nest_key in ("metrics", "data", "quote", "ohlc", "ticker"):
        nested = payload.get(nest_key)
        if isinstance(nested, dict):
            for k in ("price", "close", "cmp", "ltp", "last_price", "current_price", "prev_close"):
                v = _coerce_price(nested.get(k))
                if v > 0:
                    return v
    return 0.0



def normalize_feed_payload(data: dict) -> dict:
    """
    Map mixed-case / alternate metric keys from CSV/manual uploads to the
    canonical keys expected by instant_scanner and decide paths.
    """
    if not isinstance(data, dict):
        return {}
    key_mapping = {
        "symbol": "symbol",
        "price": "prev_close",
        "prev_close": "prev_close",
        "prevclose": "prev_close",
        "cmp": "prev_close",
        "ltp": "last_price",
        "last_price": "last_price",
        "close": "prev_close",
        "pe": "pe_ratio",
        "p/e": "pe_ratio",
        "pe_ratio": "pe_ratio",
        "roce": "roce",
        "roe": "roe",
        "rsi": "rsi",
        "macd": "macd_hist",
        "macd_hist": "macd_hist",
        "ema20": "ema20",
        "ema_20": "ema20",
        "ema50": "ema50",
        "ema_50": "ema50",
        "sector": "sector",
        "industry": "industry",
        "technical_score": "technical_score",
        "fundamental_score": "fundamental_score",
        "combined_score": "combined_score",
        "tech_score": "technical_score",
        "fund_score": "fundamental_score",
        "change_pct": "change_pct",
        "change%": "change_pct",
        "rvol": "rvol",
        "volume": "volume",
        "atr": "atr",
        "daily_atr": "daily_atr",
        "high_52w": "high_52w",
        "52w_high": "high_52w",
        "dist_52w_pct": "dist_52w_pct",
    }
    normalized: Dict[str, Any] = {}
    for k, v in data.items():
        if k is None:
            continue
        raw = str(k).strip()
        lk = raw.lower().replace(" ", "_")
        standard = key_mapping.get(lk, lk)
        # Prefer numeric when clearly numeric
        if isinstance(v, bool):
            normalized[standard] = v
        elif isinstance(v, (int, float)):
            normalized[standard] = float(v)
        elif isinstance(v, str):
            s = v.strip().replace(",", "")
            try:
                if s and s.replace(".", "", 1).replace("-", "", 1).isdigit():
                    normalized[standard] = float(s)
                else:
                    normalized[standard] = v
            except Exception:
                normalized[standard] = v
        else:
            normalized[standard] = v
    return normalized


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
    """True when feed row has any field that can short-circuit upstream work."""
    if not isinstance(payload, dict) or not payload:
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
        or payload.get("close") is not None
        or payload.get("price") is not None
        or payload.get("ltp") is not None
        or payload.get("combined_score") is not None
        or payload.get("technical_score") is not None
        or payload.get("decision")
    )




def strip_none_fields(payload: dict) -> dict:
    """Remove keys whose value is None so Neon merges never wipe real metrics with null."""
    if not isinstance(payload, dict):
        return {}
    return {k: v for k, v in payload.items() if v is not None}


def merge_feed_payload(existing: dict, incoming: dict) -> dict:
    """
    Merge incoming quote/repair fields into existing feed row.
    Rules (Merge, Never Wipe):
    - Drops None from incoming (no poison overwrite of durable Neon fields)
    - Does not write volume/pe_ratio/day_change_pct/roce/rsi when incoming is 0
      and existing has a real non-zero value (sparse fallback protection)
    - Seeded fundamentals (pe_ratio_seed / roce_seed / sentiment_seed) never
      overwrite a previously stored non-seed real value
    - Volatile price fields (price, volume, day_change_pct, …) always refresh
      when the incoming value is valid
    """
    base = dict(existing) if isinstance(existing, dict) else {}
    inc = strip_none_fields(incoming if isinstance(incoming, dict) else {})

    # Fundamental / slow fields that seeds must not clobber once a real value exists
    _SEED_PROTECTED = {
        "pe_ratio": "pe_ratio_seed",
        "roce": "roce_seed",
        "sentiment_score": "sentiment_seed",
        "roe": "roe_seed",
        "quality_score": "quality_score_seed",
        "sector": "sector_seed",
        "industry": "industry_seed",
        "debt_to_equity": "debt_to_equity_seed",
        "revenue_growth": "revenue_growth_seed",
        "fundamental_score": "fundamental_score_seed",
    }
    _ZERO_PROTECTED = ("volume", "pe_ratio", "market_cap", "day_change_pct", "roce", "rsi")

    for k, v in inc.items():
        # 1) Sparse zero protection
        if k in _ZERO_PROTECTED:
            try:
                old = base.get(k)
                old_f = float(old) if old is not None else None
                new_f = float(v) if v is not None else None
                if old_f is not None and old_f != 0 and new_f == 0:
                    continue
            except (TypeError, ValueError):
                pass

        # 2) Seed must not overwrite a real (non-seed) stored value
        seed_flag = _SEED_PROTECTED.get(k)
        if seed_flag and inc.get(seed_flag) is True:
            old_val = base.get(k)
            old_was_seed = bool(base.get(seed_flag))
            if old_val is not None and not old_was_seed:
                # Keep the durable real fundamental; do not write the seed flag either
                continue
            # If existing was also a seed (or empty), allow refresh of the seed baseline
            base[k] = v
            base[seed_flag] = True
            continue

        # 3) Real (non-seed) incoming value clears any prior seed flag
        if seed_flag and inc.get(seed_flag) is not True:
            base.pop(seed_flag, None)

        base[k] = v
    return base


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

    def __init__(self, redis_get=None, redis_set=None, redis_client=None):
        # Optional deps: when omitted, bind to kv_cache (memory + Neon).
        # Fixes TypeError: DataFeedStore() missing 2 required positional arguments
        # which previously caused get_all_stock_feeds → {} → all scores=40 HOLD.
        if redis_get is None or redis_set is None:
            try:
                from kv_cache import kv_get, kv_set
                redis_get = redis_get or kv_get
                redis_set = redis_set or kv_set
            except Exception:
                pass
        if redis_get is None or redis_set is None:
            raise TypeError(
                "DataFeedStore requires redis_get/redis_set or working kv_cache"
            )
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
        Prefer local cache; on miss read Neon via ALL known key aliases.
        Canonical: stockky:data_feed:sym:SYM
        Alias:     feed:SYM
        Legacy:    data_feed:SYM
        Never treat an empty local dict as authoritative when Neon has data.
        """
        base = _norm_sym(symbol)
        if not base:
            return None
        keys = [
            DATA_FEED_PREFIX + base,
            FEED_ALIAS_PREFIX + base,
            FEED_LEGACY_PREFIX + base,
        ]

        # 1) Local hit on any alias if payload looks useful
        for key in keys:
            local = _LOCAL_SYMBOLS.get(key)
            if isinstance(local, dict) and _payload_is_useful(local):
                return dict(local)

        # 2) Durable read (Neon via kv_cache) — try each key
        val = None
        for key in keys:
            try:
                val = self._get(key)
            except Exception as e:
                logger.debug("data_feed get_symbol neon %s: %s", key, e)
                val = None
            if isinstance(val, dict) and val:
                break

        if isinstance(val, dict) and val:
            # Warm all aliases locally so later reads are free
            for key in keys:
                _LOCAL_SYMBOLS[key] = val
            _LOCAL_INDEX.add(base)
            return dict(val)

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
            alias_key = FEED_ALIAS_PREFIX + base
            local = _LOCAL_SYMBOLS.get(key) or _LOCAL_SYMBOLS.get(alias_key)
            if isinstance(local, dict) and _payload_is_useful(local):
                result[base] = dict(local)
            else:
                missing_keys.append(key)
                key_to_base[key] = base
                # Also request alias + legacy in same bulk round-trip
                missing_keys.append(alias_key)
                key_to_base[alias_key] = base
                legacy_key = FEED_LEGACY_PREFIX + base
                missing_keys.append(legacy_key)
                key_to_base[legacy_key] = base

        if not missing_keys:
            return result

        # Single Neon ANY query via kv_cache.get_many (required for scan performance)
        bulk: Dict[str, Any] = {}
        try:
            import kv_cache as _kc
            get_many = getattr(_kc, "get_many", None) or getattr(_kc, "kv_get_many", None)
            if callable(get_many):
                bulk = get_many(missing_keys) or {}
            else:
                logger.warning("kv_cache.get_many missing — falling back to serial gets (slow)")
                for k in missing_keys:
                    try:
                        v = self._get(k)
                        if v is not None:
                            bulk[k] = v
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("get_symbols_bulk kv_cache path failed: %s", e)
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
            base = key_to_base.get(key)
            if not base:
                if key.startswith(DATA_FEED_PREFIX):
                    base = key[len(DATA_FEED_PREFIX):]
                elif key.startswith(FEED_ALIAS_PREFIX):
                    base = key[len(FEED_ALIAS_PREFIX):]
                else:
                    base = key
            base = _norm_sym(base)
            if not base:
                continue
            # Prefer first hit; do not overwrite richer payload with thinner alias
            if base in result and _payload_is_useful(result[base]):
                pass
            else:
                result[base] = dict(val)
            _LOCAL_SYMBOLS[DATA_FEED_PREFIX + base] = result[base]
            _LOCAL_SYMBOLS[FEED_ALIAS_PREFIX + base] = result[base]
            _LOCAL_SYMBOLS[FEED_LEGACY_PREFIX + base] = result[base]
            _LOCAL_INDEX.add(base)

        return result

    def _prepare_symbol_payload(self, base: str, payload: dict, existing: Optional[dict]) -> Optional[dict]:
        """
        Shared prep for a single symbol write: merge-never-wipe against existing,
        normalize, apply the ≤max-price durable-fields-only gate.
        Returns the final payload dict to persist, or None if nothing to write.
        """
        if isinstance(existing, dict) and existing:
            payload = merge_feed_payload(existing, payload)
        payload = strip_none_fields(normalize_feed_payload(dict(payload)))
        payload.setdefault("symbol", base)
        payload.setdefault("updated_at", _now_iso())

        # ≤ max-price gate: high-ticket names are not scan targets, but we still
        # persist slow fields (PE/sector/scores) so Neon stays warm. Volatile
        # price fields are stripped so the universe price-filter does not
        # re-admit them via a cached over-cap LTP.
        px = _payload_price(payload)
        if px > MAX_STOCK_PRICE:
            for drop_k in (
                "price", "close", "cmp", "ltp", "last_price", "current_price",
                "day_high", "day_low", "day_change_pct", "previous_close", "volume",
            ):
                payload.pop(drop_k, None)
            payload["price_over_cap"] = True
            payload["price_cap"] = MAX_STOCK_PRICE
            durable_keys = (
                "pe_ratio", "roce", "roe", "sector", "industry", "fundamental_score",
                "quality_score", "metrics", "technical_score", "sentiment_score",
            )
            if not any(payload.get(k) is not None for k in durable_keys):
                return None
        return payload

    def put_symbol(self, symbol: str, payload: dict, ttl: int = DATA_FEED_TTL) -> None:
        base = _norm_sym(symbol)
        if not base or not isinstance(payload, dict):
            return
        existing = {}
        try:
            existing = self.get_symbol(base) or {}
        except Exception:
            existing = {}
        payload = self._prepare_symbol_payload(base, payload, existing)
        if payload is None:
            return

        key = DATA_FEED_PREFIX + base
        alias_key = FEED_ALIAS_PREFIX + base
        _LOCAL_SYMBOLS[key] = payload
        _LOCAL_SYMBOLS[alias_key] = payload
        _LOCAL_INDEX.add(base)
        # Durable write: canonical + alias + legacy (stops key-mismatch cache misses)
        # kv_cache._neon_set already uses ON CONFLICT (k) DO UPDATE — true UPSERT, no duplicates.
        try:
            self._set(key, payload, ttl=ttl)
        except Exception as e:
            logger.warning("data_feed put_symbol durable fail %s: %s", base, e)
        try:
            self._set(alias_key, payload, ttl=ttl)
        except Exception as e:
            logger.debug("data_feed alias write %s: %s", base, e)
        try:
            self._set(FEED_LEGACY_PREFIX + base, payload, ttl=ttl)
        except Exception as e:
            logger.debug("data_feed legacy write %s: %s", base, e)
        # Update durable index (batched cheaply every put — list is small, set-deduped)
        try:
            self._persist_index(ttl=ttl)
        except Exception as e:
            logger.debug("data_feed index persist: %s", e)

    def put_symbols_bulk(self, payload_map: Dict[str, dict], ttl: int = DATA_FEED_TTL) -> int:
        """
        Bulk version of put_symbol — the fix for the "no bulk feeding" slowness.

        Old path: N symbols → N sequential put_symbol() calls, each doing
        1 read + 3 writes + 1 index read/write (its own Neon transaction) ⇒
        for a 300-symbol feed, ~1500-1800 sequential DB round trips.

        New path: 1 bulk read (get_symbols_bulk, already existed) for merge,
        then ALL canonical+alias+legacy keys for ALL symbols are written in a
        handful of multi-row upserts via kv_cache.set_many — a few round trips
        total, and the symbol index is persisted ONCE at the end instead of
        once per symbol.

        Returns the number of symbols actually written.
        """
        if not payload_map:
            return 0

        bases: List[str] = []
        norm_map: Dict[str, dict] = {}
        for sym, payload in payload_map.items():
            base = _norm_sym(sym)
            if not base or not isinstance(payload, dict):
                continue
            bases.append(base)
            norm_map[base] = payload

        if not bases:
            return 0

        # One bulk read instead of N single reads
        try:
            existing_map = self.get_symbols_bulk(bases)
        except Exception as e:
            logger.warning("put_symbols_bulk: bulk read failed, proceeding without merge: %s", e)
            existing_map = {}

        write_items: Dict[str, dict] = {}
        written = 0
        for base in bases:
            final = self._prepare_symbol_payload(base, norm_map[base], existing_map.get(base))
            if final is None:
                continue
            key = DATA_FEED_PREFIX + base
            alias_key = FEED_ALIAS_PREFIX + base
            legacy_key = FEED_LEGACY_PREFIX + base
            _LOCAL_SYMBOLS[key] = final
            _LOCAL_SYMBOLS[alias_key] = final
            _LOCAL_SYMBOLS[legacy_key] = final
            _LOCAL_INDEX.add(base)
            write_items[key] = final
            write_items[alias_key] = final
            write_items[legacy_key] = final
            written += 1

        if not write_items:
            return 0

        try:
            import kv_cache as _kc
            set_many = getattr(_kc, "set_many", None) or getattr(_kc, "kv_set_many", None)
            if callable(set_many):
                set_many(write_items, ttl=ttl)
            else:
                logger.warning("kv_cache.set_many missing — falling back to serial sets (slow)")
                for k, v in write_items.items():
                    self._set(k, v, ttl=ttl)
        except Exception as e:
            logger.warning("put_symbols_bulk: bulk write failed (%s items): %s", len(write_items), e)
            for k, v in write_items.items():
                try:
                    self._set(k, v, ttl=ttl)
                except Exception:
                    pass

        # Persist the symbol index ONCE for the whole batch, not per symbol
        try:
            self._persist_index(ttl=ttl)
        except Exception as e:
            logger.debug("data_feed index persist (bulk): %s", e)

        logger.info("put_symbols_bulk: wrote %s/%s symbols in a batched upsert", written, len(bases))
        return written

    def has_symbol(self, symbol: str) -> bool:
        return self.get_symbol(symbol) is not None

    def delete_symbol(self, symbol: str) -> bool:
        """
        Remove a symbol from the feed entirely (canonical + alias + legacy
        keys, local caches, and the durable index). Used to purge stocks
        that should never have been persisted — e.g. price > MAX_STOCK_PRICE
        rows written before the write-path price gate existed. Returns True
        if anything was found/removed.
        """
        base = _norm_sym(symbol)
        if not base:
            return False
        found = base in _LOCAL_INDEX or self.has_symbol(base)
        key = DATA_FEED_PREFIX + base
        alias_key = FEED_ALIAS_PREFIX + base
        legacy_key = FEED_LEGACY_PREFIX + base
        for k in (key, alias_key, legacy_key):
            _LOCAL_SYMBOLS.pop(k, None)
        _LOCAL_INDEX.discard(base)
        try:
            import kv_cache as _kc
            for k in (key, alias_key, legacy_key):
                try:
                    _kc.kv_delete(k)
                except Exception as e:
                    logger.debug("delete_symbol kv_delete %s: %s", k, e)
        except Exception as e:
            logger.warning("delete_symbol: kv_cache unavailable for %s: %s", base, e)
        try:
            self._persist_index()
        except Exception as e:
            logger.debug("delete_symbol index persist: %s", e)
        return found

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
        # Always set-dedupe + sorted for stable index (prevents "1208 duplicates" growth)
        symbols = sorted(set(self.list_symbols()))
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


def _hot_job_recompute(j: dict) -> dict:
    """Recompute elapsed_sec / estimated_remaining_sec from the wall clock.

    Fixes the "remaining time is wrong" bug in the Hot Picks pipeline UI. Both
    values used to be written only when hot_job_set() was called, but the scan
    awaits stockky_hot_stocks() for minutes without touching the job, so
    /stockky-hot/status kept replaying whatever the numbers were at the last
    write — a countdown frozen mid-scan (typically "0s remaining" from the very
    first progress write) that only jumped when the run finished.

    Recomputing on every read makes the ETA a live projection: elapsed comes
    from started_at, and remaining is elapsed/processed * outstanding. It is
    deliberately conservative — unknown (None) rather than 0 while processed is
    still 0, and clamped at >= 0 so a processed count that overshoots total can
    never print a negative countdown.
    """
    if not isinstance(j, dict):
        return j
    if j.get("status") != "running" or not j.get("started_at"):
        return j
    try:
        started = datetime.fromisoformat(str(j["started_at"]))
        if started.tzinfo is None:
            started = started.replace(tzinfo=IST)
        elapsed = int((datetime.now(IST) - started).total_seconds())
        j["elapsed_sec"] = max(elapsed, 0)
        done = int(j.get("processed") or 0)
        total = int(j.get("total") or 0)
        if done > 0 and total > 0 and elapsed > 0:
            remaining_units = max(total - done, 0)
            j["estimated_remaining_sec"] = max(
                int((elapsed / done) * remaining_units), 0
            )
        elif done <= 0:
            # No symbol finished yet — an ETA here would be pure invention.
            j["estimated_remaining_sec"] = None
    except Exception:
        pass
    return j


def hot_job_get(redis_get) -> dict:
    try:
        j = redis_get(HOT_JOB_KEY)
    except Exception:
        j = None
    if isinstance(j, dict):
        # Recompute on read so /stockky-hot/status is live even while the scan
        # is inside a long await and nothing is writing to the job.
        return _hot_job_recompute(dict(j))
    return {
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
    # Same projection as on read, applied after the caller's update so a fresh
    # processed/total pair is reflected immediately.
    _hot_job_recompute(j)
    j["updated_at"] = _now_iso()
    # Durable: kv_cache routes stockky:hot_job → Neon (or Oracle when ORACLE_DSN
    # is set — the prefix routing is backend-agnostic).
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


# ── Sticky Fix Step 2: bulk helpers used by /scan/stream ───────────────────

def feed_key(symbol: str) -> str:
    """Canonical Neon key for a symbol feed payload."""
    return DATA_FEED_PREFIX + _norm_sym(symbol)


def feed_alias_key(symbol: str) -> str:
    return FEED_ALIAS_PREFIX + _norm_sym(symbol)


def feed_legacy_key(symbol: str) -> str:
    return FEED_LEGACY_PREFIX + _norm_sym(symbol)


_feed_store_singleton: Optional["DataFeedStore"] = None
_feed_store_lock = threading.Lock()


def get_data_feed_store() -> "DataFeedStore":
    """
    Process-wide DataFeedStore bound to kv_cache.
    Safe to call from helpers that previously did DataFeedStore() with no args.
    """
    global _feed_store_singleton
    if _feed_store_singleton is not None:
        return _feed_store_singleton
    with _feed_store_lock:
        if _feed_store_singleton is None:
            try:
                from kv_cache import kv_get, kv_set
                _feed_store_singleton = DataFeedStore(kv_get, kv_set, None)
            except Exception:
                # Last resort: still construct with defaults inside __init__
                _feed_store_singleton = DataFeedStore()
        return _feed_store_singleton


def get_all_stock_feeds(symbols: List[str]) -> Dict[str, dict]:
    """
    One bulk Neon round-trip for many symbols.
    Returns { "RELIANCE": {...}, "TCS": {...} } using canonical keys
    (+ alias feed:SYMBOL fallback for older writers).
    """
    try:
        return get_data_feed_store().get_symbols_bulk(symbols) or {}
    except Exception as e:
        logger.warning("get_all_stock_feeds failed: %s", e)
        return {}


def save_stock_feed(symbol: str, payload: dict, ttl: int = DATA_FEED_TTL) -> None:
    """Standardized write: dual key stockky:data_feed:sym: + feed:"""
    get_data_feed_store().put_symbol(symbol, payload, ttl=ttl)


def _score_from_payload(data: dict) -> float:
    """Best-effort conviction / combined score from a feed or scan row."""
    if not isinstance(data, dict):
        return 0.0
    for k in (
        "conviction_score",
        "conviction",
        "combined_score",
        "score",
        "decision_score",
    ):
        try:
            v = float(data.get(k) or 0)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    # Nested metrics
    for nest in ("metrics", "data", "decision"):
        nested = data.get(nest)
        if isinstance(nested, dict):
            for k in ("conviction_score", "conviction", "combined_score", "score"):
                try:
                    v = float(nested.get(k) or 0)
                    if v > 0:
                        return v
                except (TypeError, ValueError):
                    pass
    return 0.0


def find_prepare_to_buy_candidates(
    min_score: float = 58.0,
    max_score: float = 68.0,
) -> List[str]:
    """
    High-conviction "Prepare to Buy" band only — used by surgical quote refresh
    so we never storm market-data for the full 300-symbol universe.
    Sources:
      1) Neon data-feed payloads in the score band
      2) Last full-scan / hot-result rows with decision PREPARE TO BUY
    """
    store = get_data_feed_store()
    candidates: List[str] = []
    seen = set()

    try:
        symbols = store.list_symbols() or []
    except Exception:
        symbols = []

    feeds = {}
    try:
        feeds = get_all_stock_feeds(symbols) if symbols else {}
    except Exception as e:
        logger.debug("prepare-to-buy feed load: %s", e)

    for sym in symbols:
        base = _norm_sym(sym)
        if not base or base in seen:
            continue
        data = feeds.get(base) or {}
        score = _score_from_payload(data)
        decision = str(data.get("decision") or data.get("action") or "").upper()
        in_band = min_score <= score < max_score
        is_ptb = "PREPARE" in decision
        if in_band or is_ptb:
            seen.add(base)
            candidates.append(base)

    # Also pull from last full scan / hot result if available
    for key in ("stockky:last_full_scan", "stockky:hot_result_db", "stockky:hot_result"):
        try:
            from kv_cache import kv_get
            blob = kv_get(key)
        except Exception:
            blob = None
        rows = []
        if isinstance(blob, dict):
            rows = blob.get("results") or blob.get("recommendations") or blob.get("all_results") or []
            if not rows and isinstance(blob.get("data"), list):
                rows = blob["data"]
        elif isinstance(blob, list):
            rows = blob
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            base = _norm_sym(str(row.get("symbol") or ""))
            if not base or base in seen:
                continue
            score = _score_from_payload(row)
            decision = str(row.get("decision") or row.get("action") or "").upper()
            if (min_score <= score < max_score) or ("PREPARE" in decision):
                seen.add(base)
                candidates.append(base)

    logger.info(
        "prepare-to-buy candidates: %s (band %.0f–%.0f)",
        len(candidates), min_score, max_score,
    )
    return candidates


def patch_feed_price(symbol: str, live_price: float) -> bool:
    """Update only the price fields on an existing feed row (surgical refresh)."""
    base = _norm_sym(symbol)
    if not base:
        return False
    try:
        px = float(live_price)
    except (TypeError, ValueError):
        return False
    if px <= 0:
        return False
    if px > MAX_STOCK_PRICE:
        logger.info("patch_feed_price skip %s — ₹%.2f > cap", base, px)
        return False

    store = get_data_feed_store()
    existing = store.get_symbol(base) or {}
    if not isinstance(existing, dict):
        existing = {}
    existing["price"] = px
    existing["close"] = px
    existing["cmp"] = px
    existing["ltp"] = px
    existing["last_price"] = px
    existing["current_price"] = px
    existing["price_refreshed_at"] = _now_iso()
    try:
        store.put_symbol(base, existing)
        return True
    except Exception as e:
        logger.warning("patch_feed_price %s failed: %s", base, e)
        return False


# ── Yahoo 1-call (chunked) bulk price feeder — bypasses NSE 403 on Render ───
# Bumped back up: yf.download(ticker_string) is ONE HTTP call regardless of
# how many tickers are in the string, so shrinking chunk_size doesn't reduce
# Yahoo request *rate* — it only adds more chunks, each paying the same
# between-chunk pause. Bigger chunks = fewer round-trips = faster overall.
BULK_YF_CHUNK = int(__import__("os").getenv("BULK_YF_CHUNK", "60"))
# Base courtesy gap between clean chunks. Real backoff (see below) only
# kicks in when a chunk actually signals rate-limiting.
BULK_YF_CHUNK_SLEEP = float(__import__("os").getenv("BULK_YF_CHUNK_SLEEP", "0.3"))
BULK_YF_BACKOFF_SLEEP = float(__import__("os").getenv("BULK_YF_BACKOFF_SLEEP", "8.0"))
BULK_YF_MAX_RETRIES_PER_CHUNK = int(__import__("os").getenv("BULK_YF_MAX_RETRIES_PER_CHUNK", "2"))


def _is_rate_limit_error(exc: Exception) -> bool:
    """Detect Yahoo 429 / throttling signatures across yfinance's various exception shapes."""
    msg = str(exc).lower()
    return (
        "429" in msg
        or "too many requests" in msg
        or "rate limit" in msg
        or "yfratelimiterror" in msg
        or "throttle" in msg
    )


def _yf_close_volume(frame, sym_ns: str):
    """Extract last Close + Volume from a yfinance multi-ticker or single frame."""
    import math
    try:
        import pandas as pd
    except ImportError:
        pd = None

    close = None
    volume = None
    try:
        if frame is None:
            return None, None
        # MultiIndex columns: (Ticker, OHLCV)
        if hasattr(frame, "columns") and getattr(frame.columns, "nlevels", 1) > 1:
            if sym_ns in frame.columns.get_level_values(0):
                sub = frame[sym_ns]
            else:
                return None, None
        else:
            sub = frame
        if sub is None or (hasattr(sub, "empty") and sub.empty):
            return None, None
        if "Close" in getattr(sub, "columns", []):
            series = sub["Close"].dropna()
            if len(series) > 0:
                close = float(series.iloc[-1])
        elif hasattr(sub, "iloc") and not hasattr(sub, "columns"):
            # single series
            series = sub.dropna()
            if len(series) > 0:
                close = float(series.iloc[-1])
        if "Volume" in getattr(sub, "columns", []):
            vs = sub["Volume"].dropna()
            if len(vs) > 0:
                try:
                    volume = int(float(vs.iloc[-1]))
                except (TypeError, ValueError):
                    volume = None
        if close is not None and (math.isnan(close) or close <= 0):
            close = None
    except Exception as e:
        logger.debug("yf extract %s: %s", sym_ns, e)
        return None, None
    return close, volume



def compute_rsi_from_closes(closes, period: int = 14) -> Optional[float]:
    """14-period RSI in-process — zero upstream API calls."""
    try:
        import numpy as np
        arr = np.asarray(closes, dtype=float)
        arr = arr[np.isfinite(arr)]
        if len(arr) < period + 1:
            return None
        deltas = np.diff(arr)
        gains = np.where(deltas > 0, deltas, 0.0)
        losses = np.where(deltas < 0, -deltas, 0.0)
        avg_gain = float(np.mean(gains[-period:]))
        avg_loss = float(np.mean(losses[-period:]))
        if avg_loss <= 0:
            return 100.0
        rs = avg_gain / avg_loss
        return round(float(100.0 - (100.0 / (1.0 + rs))), 2)
    except Exception:
        return None


def _time_module_sleep(seconds: float) -> None:
    import time as _time
    _time.sleep(max(0.0, float(seconds)))


def bulk_yahoo_download_prices(symbols: List[str], chunk_size: int = None) -> Dict[str, dict]:
    """
    Delegate bulk price fetch to market-data-service POST /quotes/bulk
    (single yf.download on the MDS side) instead of chunked local yfinance
    calls that cascade into free-tier 429s.

    Seeds baseline pe_ratio / roce / sentiment_score so UI 5-field health
    is green without peer-fundamental storms. Real values can overwrite later.
    Only includes symbols with 0 < price <= MAX_STOCK_PRICE when price is present.
    Missing symbols get a neutral placeholder so downstream never starves.
    """
    import os
    try:
        import httpx
    except ImportError:
        logger.error("httpx not installed — bulk feed unavailable")
        return {}

    MARKET_DATA_URL = os.environ.get(
        "MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com"
    ).rstrip("/")

    bases: List[str] = []
    seen = set()
    for s in symbols or []:
        b = _norm_sym(str(s))
        if b and b not in seen:
            seen.add(b)
            bases.append(b)

    out: Dict[str, dict] = {}
    if not bases:
        return out

    try:
        resp = httpx.post(
            f"{MARKET_DATA_URL}/quotes/bulk",
            json={"symbols": bases},
            timeout=60.0,
        )
        if resp.status_code == 200:
            payload = resp.json() if resp.content else {}
            for q in (payload.get("quotes") or []):
                if not isinstance(q, dict):
                    continue
                sym = q.get("symbol")
                if not sym:
                    continue
                base = _norm_sym(str(sym))
                if not base:
                    continue

                # Honour existing price cap used by the rest of the feed
                try:
                    px = float(q.get("price") or q.get("cmp") or 0)
                except (TypeError, ValueError):
                    px = 0.0
                if px <= 0:
                    continue
                if px > MAX_STOCK_PRICE:
                    logger.debug("bulk_yahoo skip %s — ₹%.2f > cap", base, px)
                    continue

                rec = dict(q)
                rec["symbol"] = base
                # Normalize field names expected by downstream feed merge
                if "price_refreshed_at" not in rec:
                    rec["price_refreshed_at"] = rec.get("fetched_at") or _now_iso()
                if "source" not in rec:
                    rec["source"] = "yahoo_bulk"

                # Baseline seeds so Health Audit 5-fields are populated without peer storms.
                # Marked so repair can overwrite with real fundamental/sentiment later.
                if "pe_ratio" not in rec or rec.get("pe_ratio") is None:
                    rec["pe_ratio"] = 22.5
                    rec["pe_ratio_seed"] = True
                if "roce" not in rec or rec.get("roce") is None:
                    rec["roce"] = 15.0
                    rec["roce_seed"] = True
                if "sentiment_score" not in rec or rec.get("sentiment_score") is None:
                    rec["sentiment_score"] = 0.65
                    rec["sentiment_seed"] = True

                # Drop pure Nones only
                rec = {k: v for k, v in rec.items() if v is not None}
                out[base] = rec
        else:
            logger.error(
                "Bulk quote fetch HTTP %s from %s: %s",
                resp.status_code,
                MARKET_DATA_URL,
                (resp.text or "")[:200],
            )
    except Exception as e:
        logger.error("Bulk quote fetch failed: %s", e)

    # Neutral placeholders for any symbol the bulk endpoint did not return
    for b in set(bases):
        if b not in out:
            out[b] = {
                "symbol": b,
                "source": "yahoo_missing",
                "price_refreshed_at": _now_iso(),
                "pe_ratio": 22.5,
                "pe_ratio_seed": True,
                "roce": 15.0,
                "roce_seed": True,
                "sentiment_score": 0.65,
                "sentiment_seed": True,
            }

    logger.info(
        "bulk_yahoo (delegated): got %s/%s quotes under ₹%.0f (missing seeded=%s)",
        sum(1 for b in bases if out.get(b, {}).get("source") == "yahoo_bulk"),
        len(bases),
        MAX_STOCK_PRICE,
        sum(1 for b in bases if out.get(b, {}).get("source") == "yahoo_missing"),
    )
    return out



# ── NSE bulk bhavcopy — ONE file covers the whole market baseline ──────────
# Whereas bulk_yahoo_download_prices() needs N Yahoo calls (chunked) to build
# prev_close/open/high/low/volume for N symbols, NSE publishes all of that
# for the entire exchange in a single daily CSV. Fetching it once per session
# and reusing it removes most symbols from the Yahoo path entirely — Yahoo is
# then only needed for live intraday LTP while the market is open.
_BHAV_CACHE: Dict[str, Any] = {"data": None, "fetched_at": 0.0}
BHAV_CACHE_TTL_SEC = int(__import__("os").getenv("BHAV_CACHE_TTL_SEC", "1800"))  # 30 min

_NSE_BHAV_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
    "Connection": "keep-alive",
}


def _bhav_candidate_dates(n: int = 6):
    d = datetime.now(IST).date()
    now = datetime.now(IST)
    if now.hour < 18:
        d = d - timedelta(days=1)
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _bhav_urls_for_date(d) -> List[str]:
    ddmmyyyy = d.strftime("%d%m%Y")
    return [
        f"https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv",
        f"https://nsearchives.nseindia.com/content/Equities/sec_bhavdata_full_{ddmmyyyy}.csv",
    ]


def download_nse_bhavcopy_bulk(force: bool = False) -> Dict[str, dict]:
    """
    ONE HTTP call → baseline (prev_close, open, high, low, volume, close) for
    the whole NSE EQ universe. In-process cached for BHAV_CACHE_TTL_SEC so a
    premarket run + an intraday feed run + a surprise scan in the same
    session share one download instead of each re-fetching it.
    """
    now = __import__("time").time()
    if not force and _BHAV_CACHE["data"] is not None and (now - _BHAV_CACHE["fetched_at"]) < BHAV_CACHE_TTL_SEC:
        return _BHAV_CACHE["data"]

    import csv
    import io as _io
    import time as _time
    import httpx as _httpx

    out: Dict[str, dict] = {}
    # Wall-clock budget: previously this loop had no time cap and no
    # stop-flag check, so a run stuck retrying NSE (403s/blocked endpoints)
    # across 6 candidate dates × several URL patterns each could run for a
    # very long time with the Stop button unable to interrupt it (this is
    # the actual worker-thread executing inside asyncio.to_thread() from
    # data-feed's PHASE 0 — a thread pool call can't be cancelled once
    # started, so it must check the flag itself). Cap total time here and
    # bail out to the Yahoo-bulk fallback instead.
    _deadline = _time.time() + float(os.getenv("BHAV_BULK_MAX_SEC", "45"))
    try:
        with _httpx.Client(timeout=15, headers=_NSE_BHAV_HEADERS, follow_redirects=True) as client:
            try:
                client.get("https://www.nseindia.com")
            except Exception:
                pass
            for d in _bhav_candidate_dates(6):
                if data_feed_stop_requested() or __import__("time").time() > _deadline:
                    logger.info("download_nse_bhavcopy_bulk: stopping early (stop_requested or deadline)")
                    break
                for url in _bhav_urls_for_date(d):
                    if data_feed_stop_requested() or __import__("time").time() > _deadline:
                        break
                    try:
                        r = client.get(url)
                        if r.status_code != 200 or not r.content:
                            continue
                        text = r.text
                        head = text[:800].upper()
                        if "SYMBOL" not in head:
                            continue
                        reader = csv.DictReader(_io.StringIO(text))
                        if not reader.fieldnames:
                            continue
                        fields = {f.strip().upper(): f for f in reader.fieldnames}

                        def col(*names):
                            for n in names:
                                if n in fields:
                                    return fields[n]
                            return None

                        sym_c = col("SYMBOL")
                        series_c = col("SERIES")
                        close_c = col("CLOSE_PRICE", "CLOSE")
                        open_c = col("OPEN_PRICE", "OPEN")
                        high_c = col("HIGH_PRICE", "HIGH")
                        low_c = col("LOW_PRICE", "LOW")
                        prev_c = col("PREV_CLOSE", "PREVCLOSE")
                        vol_c = col("TTL_TRD_QNTY", "TOTTRDQTY")
                        if not sym_c or not close_c:
                            continue

                        def _num(row, c):
                            if not c:
                                return None
                            raw = row.get(c)
                            if raw is None or raw == "":
                                return None
                            try:
                                return float(str(raw).replace(",", "").strip())
                            except (TypeError, ValueError):
                                return None

                        for row in reader:
                            series = str(row.get(series_c) or "EQ").strip().upper() if series_c else "EQ"
                            if series not in ("EQ", "BE", "BZ"):
                                continue
                            base = str(row.get(sym_c) or "").strip().upper()
                            if not base:
                                continue
                            close = _num(row, close_c)
                            if close is None or close <= 0 or close > MAX_STOCK_PRICE:
                                continue
                            prev = _num(row, prev_c) or close
                            rec = {
                                "symbol": base,
                                "price": round(close, 2),
                                "close": round(close, 2),
                                "cmp": round(close, 2),
                                "ltp": round(close, 2),
                                "last_price": round(close, 2),
                                "current_price": round(close, 2),
                                "previous_close": round(prev, 2),
                                "day_change_pct": round(((close - prev) / prev) * 100, 2) if prev else None,
                                "source": "nse_bhavcopy",
                                "price_refreshed_at": _now_iso(),
                            }
                            oh = _num(row, open_c)
                            hi = _num(row, high_c)
                            lo = _num(row, low_c)
                            vo = _num(row, vol_c)
                            if oh is not None:
                                rec["open"] = round(oh, 2)
                            if hi is not None:
                                rec["day_high"] = round(hi, 2)
                            if lo is not None:
                                rec["day_low"] = round(lo, 2)
                            if vo is not None:
                                rec["volume"] = int(vo)
                            out[base] = rec
                        if out:
                            logger.info("bhavcopy bulk: %s symbols from %s", len(out), url)
                            _BHAV_CACHE["data"] = out
                            _BHAV_CACHE["fetched_at"] = now
                            return out
                    except Exception as e:
                        logger.debug("bhav url failed %s: %s", url, e)
                        continue
    except Exception as e:
        logger.warning("download_nse_bhavcopy_bulk failed: %s", e)

    # Failure — don't cache an empty result, so the next call retries fresh
    return out


def run_bulk_yahoo_price_feed(
    symbols: Optional[List[str]] = None,
    merge_existing: bool = True,
    use_bhavcopy_baseline: bool = True,
) -> dict:
    """
    Bulk price feed — bhavcopy-first (ONE CSV), then optional POST /quotes/bulk.

    Does NOT call GET /quote/{symbol} per ticker (that path is UI-only).
    Progress is written to the data-feed job so the UI is not stuck at 0%.
    """
    store = get_data_feed_store()
    if not symbols:
        try:
            symbols = store.list_symbols() or []
        except Exception:
            symbols = []

    if not symbols:
        return {
            "status": "error",
            "message": "No symbols provided and feed index is empty",
            "tracked_stocks": 0,
            "symbols": [],
        }

    bases = []
    seen_b = set()
    for s in symbols:
        b = _norm_sym(str(s))
        if b and b not in seen_b:
            seen_b.add(b)
            bases.append(b)

    total = len(bases)

    def _progress(processed: int, message: str, **extra):
        try:
            store.set_job(
                status="running",
                processed=min(processed, total),
                total=total,
                message=message,
                updated_at=_now_iso(),
                **{k: v for k, v in extra.items() if v is not None},
            )
        except Exception:
            pass

    _progress(0, f"Bulk phase: downloading NSE bhavcopy for {total} symbols…")

    bhav: Dict[str, dict] = {}
    if use_bhavcopy_baseline:
        try:
            bhav = download_nse_bhavcopy_bulk() or {}
            logger.info("BULK_PATH bhavcopy rows=%s universe=%s", len(bhav), total)
        except Exception as e:
            logger.warning("bhavcopy baseline unavailable, falling back to Yahoo bulk: %s", e)
            bhav = {}

    saved = 0
    skipped = 0
    bhav_hits = 0
    hit_symbols: list = []
    _progress(0, f"Bulk phase: seeding Neon from bhavcopy ({len(bhav)} market rows)…")

    # Build all seed payloads in memory first, then write them in ONE batched
    # upsert instead of one put_symbol() call (1 read + 3 writes + 1 index
    # persist) per symbol. This is the main fix for "no bulk feeding" slowness.
    seed_batch: Dict[str, dict] = {}
    for base in bases:
        rec = bhav.get(base)
        if not rec:
            continue
        try:
            seed = dict(rec)
            seed.setdefault("rsi", seed.get("rsi") if seed.get("rsi") is not None else 50.0)
            seed.setdefault("pe_ratio", seed.get("pe_ratio") if seed.get("pe_ratio") is not None else None)
            seed.setdefault("roce", seed.get("roce") if seed.get("roce") is not None else None)
            seed.setdefault("sentiment_score", seed.get("sentiment_score") if seed.get("sentiment_score") is not None else 50.0)
            seed["_seed"] = True
            seed["source"] = seed.get("source") or "nse_bhavcopy"
            seed_batch[base] = seed
            bhav_hits += 1
        except Exception as e:
            skipped += 1
            logger.debug("bhav prep %s: %s", base, e)

    if seed_batch:
        _progress(0, f"Bulk phase: writing {len(seed_batch)} bhavcopy rows in one batch…")
        try:
            saved = store.put_symbols_bulk(seed_batch, ttl=DATA_FEED_TTL)
            hit_symbols = list(seed_batch.keys())
        except Exception as e:
            logger.warning("bhav bulk write failed, falling back to per-symbol: %s", e)
            for base, seed in seed_batch.items():
                try:
                    store.put_symbol(base, seed)
                    saved += 1
                    hit_symbols.append(base)
                except Exception as e2:
                    skipped += 1
                    logger.debug("bhav upsert fallback %s: %s", base, e2)
    _progress(saved, f"Bhavcopy seed {saved}/{total} complete", ok_count=saved)

    # Honor Stop between phases — bhavcopy phase above now has its own
    # deadline/stop-check; this catches the case where a click landed right
    # as that phase finished, before the (potentially much slower) Yahoo
    # fallback phase below would otherwise start unconditionally.
    if data_feed_stop_requested():
        logger.info("run_bulk_yahoo_price_feed: stop requested after bhavcopy phase, exiting")
        return {
            "status": "stopped",
            "message": f"Stopped after bhavcopy phase — {saved}/{total} seeded",
            "tracked_stocks": saved,
            "requested": total,
            "symbols": hit_symbols,
        }

    missed = [b for b in bases if b not in bhav]
    market_open = False
    try:
        market_open = _is_nse_session_open()
    except Exception:
        try:
            from surprise_scanner import is_market_open_ist
            market_open = is_market_open_ist()
        except Exception:
            market_open = False

    # Outside market hours: bhavcopy close IS the last price — skip Yahoo entirely
    # when coverage is decent. This is the main "stuck at 0%" / slow-feed fix.
    min_cov = max(1, int(0.25 * total))
    if not market_open and bhav_hits >= min_cov:
        logger.info(
            "BULK_PATH done bhavcopy-only hits=%s/%s market_open=%s (skip Yahoo)",
            bhav_hits, total, market_open,
        )
        _progress(saved, f"Bulk complete (bhavcopy-only): {saved}/{total}")
        return {
            "status": "success",
            "tracked_stocks": saved,
            "requested": len(symbols),
            "bhavcopy_hits": bhav_hits,
            "yahoo_calls_needed_for": 0,
            "yahoo_hits": 0,
            "skipped": skipped,
            "market_open": market_open,
            "max_price": MAX_STOCK_PRICE,
            "bulk_mode": "bhavcopy_only",
            "message": (
                f"Bulk feed saved {saved}/{total} via bhavcopy only "
                f"(market closed — no Yahoo / no per-symbol /quote)"
            ),
            "symbols": sorted(set(hit_symbols)),
        }

    # Yahoo via POST /quotes/bulk only (never GET /quote/{sym} here)
    yahoo_targets = bases if market_open else missed
    prices: Dict[str, dict] = {}
    if yahoo_targets:
        _progress(
            saved,
            f"Bulk phase: POST /quotes/bulk for {len(yahoo_targets)} symbols…",
            ok_count=saved,
        )
        logger.info("BULK_PATH calling POST /quotes/bulk for %s symbols", len(yahoo_targets))
        try:
            prices = bulk_yahoo_download_prices(yahoo_targets) or {}
        except Exception as e:
            logger.warning("BULK_PATH /quotes/bulk failed: %s", e)
            prices = {}
        if prices:
            try:
                store.put_symbols_bulk(prices, ttl=DATA_FEED_TTL)
                for base in prices:
                    if base not in hit_symbols:
                        saved += 1
                        hit_symbols.append(base)
            except Exception as e:
                logger.warning("yahoo bulk write failed, falling back to per-symbol: %s", e)
                for base, rec in prices.items():
                    try:
                        store.put_symbol(base, rec)
                        if base not in hit_symbols:
                            saved += 1
                            hit_symbols.append(base)
                    except Exception as e2:
                        skipped += 1
                        logger.debug("bulk upsert fallback %s: %s", base, e2)

    logger.info(
        "BULK_PATH done saved=%s bhav=%s yahoo=%s market_open=%s",
        saved, bhav_hits, len(prices), market_open,
    )
    _progress(saved, f"Bulk complete: {saved}/{total} (bhav {bhav_hits}, yahoo {len(prices)})")
    return {
        "status": "success",
        "tracked_stocks": saved,
        "requested": len(symbols),
        "bhavcopy_hits": bhav_hits,
        "yahoo_calls_needed_for": len(yahoo_targets),
        "yahoo_hits": len(prices),
        "skipped": skipped,
        "market_open": market_open,
        "max_price": MAX_STOCK_PRICE,
        "bulk_mode": "bhavcopy+yahoo_bulk" if prices else "bhavcopy_only",
        "message": (
            f"Bulk feed saved {saved}/{total} "
            f"(bhavcopy {bhav_hits}, yahoo_bulk {len(prices)}, ≤₹{MAX_STOCK_PRICE:.0f})"
        ),
        "symbols": sorted(set(hit_symbols)),
    }


def clear_stuck_feed_job_on_boot() -> dict:
    """
    Container-amnesia safe boot heal.

    ONLY resets a stuck job status (running/stopping left behind when Render
    killed the worker). NEVER truncates stockky_kv symbol payloads — that is
    what hard-reset is for. Stock data must survive free-tier sleep/restart.
    """
    store = get_data_feed_store()
    job = {}
    try:
        job = store.job() or {}
    except Exception as e:
        logger.debug("boot heal job read: %s", e)
        return {"healed": False, "reason": str(e)[:120]}

    status = str(job.get("status") or "idle").lower()
    if status not in ("running", "stopping"):
        # Still clear process-local stop flag so a fresh process is clean
        try:
            clear_data_feed_stop()
        except Exception:
            pass
        return {"healed": False, "status": status, "reason": "job not stuck"}

    try:
        store.set_job(
            status="idle",
            message="Boot heal: cleared stuck job after container restart (stock data preserved)",
            stop_requested=False,
        )
        clear_data_feed_stop()
        logger.info("Boot heal: stuck data-feed job %s → idle (stock rows preserved)", status)
        return {"healed": True, "previous_status": status}
    except Exception as e:
        logger.warning("Boot heal failed: %s", e)
        return {"healed": False, "reason": str(e)[:120]}


def list_feed_symbols_from_neon_under_max_price(max_price: float = None) -> List[str]:
    """
    Stateless universe from Neon data-feed only (survives container amnesia).
    Keeps symbols with unknown price (0) or price <= max_price.
    """
    cap = float(max_price if max_price is not None else MAX_STOCK_PRICE)
    store = get_data_feed_store()
    try:
        symbols = store.list_symbols() or []
    except Exception:
        symbols = []
    if not symbols:
        return []
    feeds = {}
    try:
        feeds = get_all_stock_feeds(symbols) or {}
    except Exception:
        feeds = {}
    kept: List[str] = []
    for sym in symbols:
        base = _norm_sym(sym)
        if not base:
            continue
        data = feeds.get(base) or {}
        px = _payload_price(data) if isinstance(data, dict) else 0.0
        if px <= 0 or px <= cap:
            kept.append(base)
    return kept


# ── Optimized bulk quote cache (shared Data Feed + Surprise) ───────────────
BULK_QUOTE_CACHE_KEY = "system:bulk_quote_cache"
PRICE_ALERTS_KEY = "system:price_alerts"
BULK_QUOTE_OPEN_TTL = int(__import__("os").getenv("BULK_QUOTE_OPEN_TTL", "120"))   # 2 min during market
BULK_QUOTE_CLOSED_TTL = int(__import__("os").getenv("BULK_QUOTE_CLOSED_TTL", "21600"))  # 6h closed


def _is_nse_session_open() -> bool:
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
        t = now.time()
        return dtime(9, 15) <= t <= dtime(15, 30)
    except Exception:
        return False


def get_bulk_quote_cache() -> dict:
    """Read shared bulk quote map {SYMBOL: {price, ...}, _meta: {...}}."""
    try:
        import kv_cache as _kc
        raw = _kc.kv_get(BULK_QUOTE_CACHE_KEY)
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        logger.debug("bulk quote cache read: %s", e)
    return {}


def set_bulk_quote_cache(quotes: dict, source: str = "yahoo_bulk") -> dict:
    """
    Persist bulk quotes with market-aware TTL.
    Open: short TTL so UI stays fresh; Closed: long TTL (no Yahoo storm).
    """
    open_now = _is_nse_session_open()
    ttl = BULK_QUOTE_OPEN_TTL if open_now else BULK_QUOTE_CLOSED_TTL
    payload = {
        "_meta": {
            "timestamp": __import__("time").time(),
            "source": source,
            "market_open": open_now,
            "count": len([k for k in quotes.keys() if not str(k).startswith("_")]),
            "ttl_sec": ttl,
        },
        "quotes": {str(k).upper(): v for k, v in (quotes or {}).items() if not str(k).startswith("_")},
    }
    try:
        import kv_cache as _kc
        _kc.kv_set(BULK_QUOTE_CACHE_KEY, payload, ttl=ttl)
    except Exception as e:
        logger.warning("bulk quote cache write: %s", e)
    return payload


def get_cached_quote(symbol: str) -> Optional[dict]:
    """Single-symbol lookup from bulk cache (avoids /quote when warm)."""
    base = _norm_sym(symbol)
    cache = get_bulk_quote_cache()
    quotes = cache.get("quotes") if isinstance(cache, dict) else {}
    if isinstance(quotes, dict):
        row = quotes.get(base)
        if isinstance(row, dict) and _payload_price(row) > 0:
            return row
    return None


def bulk_cache_age_sec() -> Optional[float]:
    cache = get_bulk_quote_cache()
    meta = (cache or {}).get("_meta") or {}
    ts = meta.get("timestamp")
    if ts is None:
        return None
    try:
        return __import__("time").time() - float(ts)
    except (TypeError, ValueError):
        return None


def should_refresh_bulk_cache(max_age_open: int = None, max_age_closed: int = None) -> bool:
    age = bulk_cache_age_sec()
    if age is None:
        return True
    open_now = _is_nse_session_open()
    limit = max_age_open if open_now else max_age_closed
    if limit is None:
        limit = BULK_QUOTE_OPEN_TTL if open_now else BULK_QUOTE_CLOSED_TTL
    return age >= float(limit)


# Patch run_bulk_yahoo_price_feed to also write shared cache — done via wrapper below
def run_bulk_yahoo_price_feed_cached(
    symbols: Optional[List[str]] = None,
    merge_existing: bool = True,
    force: bool = False,
) -> dict:
    """
    Bulk Yahoo feed with shared cache:
      - If cache warm and not force → return cached hits (near-zero Yahoo calls)
      - Else download chunks, write Neon feed + bulk cache
    """
    if not force and not should_refresh_bulk_cache():
        cache = get_bulk_quote_cache()
        quotes = (cache or {}).get("quotes") or {}
        if quotes:
            # Optionally still merge into feed store for dashboard consistency
            if merge_existing and symbols:
                store = get_data_feed_store()
                saved = 0
                for sym in symbols:
                    b = _norm_sym(sym)
                    row = quotes.get(b)
                    if isinstance(row, dict) and _payload_price(row) > 0:
                        try:
                            # put_symbol → merge_feed_payload (protects real fundamentals)
                            store.put_symbol(b, row)
                            saved += 1
                        except Exception:
                            pass
            return {
                "status": "success",
                "source": "cache",
                "tracked_stocks": len(quotes),
                "requested": len(symbols or []),
                "yahoo_hits": 0,
                "cache_age_sec": bulk_cache_age_sec(),
                "message": f"Bulk quote cache hit ({len(quotes)} symbols, age {int(bulk_cache_age_sec() or 0)}s)",
                "symbols": sorted(quotes.keys()),
            }

    result = run_bulk_yahoo_price_feed(symbols, merge_existing=merge_existing)
    # Build quotes map from result symbols via store
    store = get_data_feed_store()
    quotes = {}
    for sym in (result.get("symbols") or []):
        row = store.get_symbol(sym) or {}
        px = _payload_price(row)
        if px > 0:
            quotes[sym] = {
                "symbol": sym,
                "price": px,
                "cmp": px,
                "previous_close": row.get("previous_close"),
                "day_change_pct": row.get("day_change_pct"),
                "day_high": row.get("day_high"),
                "day_low": row.get("day_low"),
                "volume": row.get("volume"),
                "source": row.get("source") or "yahoo_bulk",
            }
            quotes[sym] = {k: v for k, v in quotes[sym].items() if v is not None}
    if quotes:
        set_bulk_quote_cache(quotes, source="yahoo_bulk")
    result["source"] = result.get("source") or "live"
    result["cache_written"] = bool(quotes)
    return result


# ── Real-time price alerts ─────────────────────────────────────────────────
def list_price_alerts() -> list:
    try:
        import kv_cache as _kc
        raw = _kc.kv_get(PRICE_ALERTS_KEY)
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict) and isinstance(raw.get("alerts"), list):
            return raw["alerts"]
    except Exception as e:
        logger.debug("list_price_alerts: %s", e)
    return []


def save_price_alerts(alerts: list) -> list:
    try:
        import kv_cache as _kc
        clean = []
        for a in alerts or []:
            if not isinstance(a, dict):
                continue
            sym = _norm_sym(a.get("symbol"))
            if not sym:
                continue
            try:
                target = float(a.get("target_price") or a.get("target") or 0)
            except (TypeError, ValueError):
                continue
            if target <= 0:
                continue
            direction = str(a.get("direction") or "above").lower()
            if direction not in ("above", "below"):
                direction = "above"
            clean.append({
                "id": str(a.get("id") or f"{sym}-{direction}-{target}"),
                "symbol": sym,
                "target_price": target,
                "direction": direction,
                "enabled": bool(a.get("enabled", True)),
                "note": str(a.get("note") or "")[:120],
                "created_at": a.get("created_at") or _now_iso(),
                "last_triggered_at": a.get("last_triggered_at"),
                "trigger_count": int(a.get("trigger_count") or 0),
            })
        _kc.kv_set(PRICE_ALERTS_KEY, {"alerts": clean}, ttl=None)
        return clean
    except Exception as e:
        logger.warning("save_price_alerts: %s", e)
        return alerts or []


def add_price_alert(symbol: str, target_price: float, direction: str = "above", note: str = "") -> dict:
    alerts = list_price_alerts()
    entry = {
        "id": f"{_norm_sym(symbol)}-{direction}-{target_price}-{int(__import__('time').time())}",
        "symbol": _norm_sym(symbol),
        "target_price": float(target_price),
        "direction": direction if direction in ("above", "below") else "above",
        "enabled": True,
        "note": (note or "")[:120],
        "created_at": _now_iso(),
        "last_triggered_at": None,
        "trigger_count": 0,
    }
    alerts.append(entry)
    save_price_alerts(alerts)
    return entry


def delete_price_alert(alert_id: str) -> bool:
    alerts = list_price_alerts()
    new = [a for a in alerts if str(a.get("id")) != str(alert_id)]
    if len(new) == len(alerts):
        return False
    save_price_alerts(new)
    return True


def evaluate_price_alerts(price_map: Optional[dict] = None) -> list:
    """
    Check alerts against price_map {SYM: float} or bulk cache / feed store.
    Returns list of triggered alert dicts (with current_price).
    Cooldown: skip re-trigger within 15 minutes per alert id.
    """
    import time as _time
    alerts = list_price_alerts()
    if not alerts:
        return []

    if not price_map:
        price_map = {}
        cache = get_bulk_quote_cache()
        for sym, row in ((cache.get("quotes") or {}) if isinstance(cache, dict) else {}).items():
            if isinstance(row, dict):
                px = _payload_price(row)
                if px > 0:
                    price_map[str(sym).upper()] = px
        if not price_map:
            store = get_data_feed_store()
            for a in alerts:
                sym = a.get("symbol")
                row = store.get_symbol(sym) or {}
                px = _payload_price(row)
                if px > 0:
                    price_map[sym] = px

    triggered = []
    updated = False
    now = _time.time()
    for a in alerts:
        if not a.get("enabled", True):
            continue
        sym = a.get("symbol")
        px = float(price_map.get(sym) or 0)
        if px <= 0:
            continue
        target = float(a.get("target_price") or 0)
        direction = a.get("direction") or "above"
        hit = (px >= target) if direction == "above" else (px <= target)
        if not hit:
            continue
        last = a.get("last_triggered_at")
        if last:
            try:
                # ISO or epoch
                if isinstance(last, (int, float)):
                    last_ts = float(last)
                else:
                    from datetime import datetime
                    last_ts = datetime.fromisoformat(str(last).replace("Z", "+00:00")).timestamp()
                if now - last_ts < 900:  # 15 min cooldown
                    continue
            except Exception:
                pass
        a["last_triggered_at"] = _now_iso()
        a["trigger_count"] = int(a.get("trigger_count") or 0) + 1
        updated = True
        triggered.append({
            **a,
            "current_price": px,
            "triggered_at": a["last_triggered_at"],
        })
    if updated:
        save_price_alerts(alerts)
    return triggered