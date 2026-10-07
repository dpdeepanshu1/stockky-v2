"""
watchlist_engine/sources.py — Short-Term Trading Upgrade (2026-09-02)

Tiered signal sourcing ladder for the watchlist. Tries the richest source
first, degrades automatically via circuit breakers, never goes fully silent.

Tier 1: Full pipeline via api-gateway (/stockky-hot + /surprise/ipo/list —
         the same two routes real-trade-service's own candidate_engine
         already calls for the existing candidate pipeline).
         Richest signal — includes conviction score from the full
         analysis-intelligence + decision-prediction pipeline.
         Falls back to last-known-good local cache if the breaker trips.

Tier 2: Raw market/event data via analysis-intelligence-service's event
         sub-service (/events/raw-feed) + local event_depth_local.classify_text.
         No scoring pipeline — just detection + local classification.
         Used when Tier 1 (api-gateway) is unhealthy.

Tier 3: Pure volume-shock via the existing candidate_engine scanner.
         Guaranteed to work as long as market-data-service (yfinance)
         is reachable. Used when both Tier 1 and Tier 2 are unavailable.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

import config
from resilience.circuit_breaker import api_gateway_breaker, event_service_breaker
from resilience.local_cache import save_snapshot, load_snapshot

logger = logging.getLogger("real-trade-watchlist-sources")

_HTTP_TIMEOUT = 8.0
# /stockky-hot can trigger a slow cold-cache scan rather than a cheap cached
# read (see api-gateway/main.py's stockky_hot_endpoint docstring) —
# candidate_engine/candidates.py's own _fetch() uses a 25s timeout for this
# exact call; matching that here so Tier 1 doesn't falsely trip the circuit
# breaker on a slow-but-healthy response.
_TIER1_HTTP_TIMEOUT = 25.0


def _ipo_optional() -> bool:
    return (os.getenv("WATCHLIST_TIER1_IPO_OPTIONAL") or "").strip().lower() not in ("0", "false", "no", "off")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── group 226 (item 5 of the 2026-10-07 list): do not read "Hot Picks still warming" as "no catalysts" ───────
# Right after a boot api-gateway's Hot Picks cache is cold: /stockky-hot answers at once with empty buckets and
# "warming": true while a background job builds the list (api-gateway stockky_hot_cached). The first watchlist refresh
# took that for an empty Tier 1, fell to Tier 2/3 and went a whole cycle without the Hot Picks catalysts. Now, when the
# answer is empty AND flagged warming, Tier 1 re-polls /stockky-hot a few times (bounded) before giving up.
#   WATCHLIST_TIER1_WARMUP_RETRIES   re-polls (default 3, 0 = off)      WATCHLIST_TIER1_WARMUP_WAIT_S   seconds between (default 8)
#   WATCHLIST_TIER1_WARMUP_MIN_GAP_S at most one waiting spell per this many seconds (default 120), so a Hot Picks
#   job that stays slow cannot add the wait to every cycle.
_HOT_BUCKET_KEYS = ("bulk_insider_driven", "results_driven", "news_driven")
_warm_wait_last = [-1e9]


def _env_num(name: str, default: float) -> float:
    try:
        v = float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default
    return v if v == v and v >= 0 else default


def _hot_is_warming_empty(payload: Any) -> bool:
    """True for a Tier 1 payload whose Hot Picks part is flagged warming and lists nothing in any bucket."""
    try:
        hot = payload.get("hot_picks")
        return bool(isinstance(hot, dict) and hot.get("warming") and not any(hot.get(k) for k in _HOT_BUCKET_KEYS))
    except Exception:  # noqa: BLE001
        return False


async def _wait_for_hot_picks(payload: dict) -> dict:
    """Re-poll /stockky-hot while it still says "warming" with empty buckets. Returns the payload with the newest Hot
    Picks answer (the original one when nothing better came). Never raises."""
    retries = int(_env_num("WATCHLIST_TIER1_WARMUP_RETRIES", 3))
    wait_s = _env_num("WATCHLIST_TIER1_WARMUP_WAIT_S", 8.0)
    gap_s = _env_num("WATCHLIST_TIER1_WARMUP_MIN_GAP_S", 120.0)
    if retries <= 0 or not _hot_is_warming_empty(payload):
        return payload
    import time as _t
    if _t.monotonic() - _warm_wait_last[0] < gap_s:
        return payload
    _warm_wait_last[0] = _t.monotonic()
    logger.info("watchlist/sources: Hot Picks is still warming (empty buckets) - re-polling up to %d x %.0fs before "
                "falling back to Tier 2", retries, wait_s)
    for attempt in range(1, retries + 1):
        await asyncio.sleep(wait_s)
        try:
            async with httpx.AsyncClient(timeout=_TIER1_HTTP_TIMEOUT) as client:
                r = await client.get(f"{config.API_GATEWAY_URL}/stockky-hot")
                r.raise_for_status()
                hot = r.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchlist/sources: Hot Picks re-poll %d/%d failed (%s: %s) - giving up the wait",
                           attempt, retries, type(exc).__name__, exc)
            return payload
        if isinstance(hot, dict) and (any(hot.get(k) for k in _HOT_BUCKET_KEYS) or not hot.get("warming")):
            logger.info("watchlist/sources: Hot Picks ready after re-poll %d/%d", attempt, retries)
            return {**payload, "hot_picks": hot}
    logger.info("watchlist/sources: Hot Picks still warming after %d re-poll(s) - continuing without it this cycle", retries)
    return payload


# ── Public entry point ────────────────────────────────────────────────────────

async def fetch_watchlist_candidates(db, mode: str) -> list[dict]:
    """
    Return raw candidate dicts, each containing:
        symbol, catalyst_type, catalyst_price, catalyst_ts,
        source_tier, conviction_score
    Tries Tier 1 → Tier 2 → Tier 3 in order, stopping at the first
    non-empty result.
    """

    # ── Tier 1: full pipeline via api-gateway ────────────────────────────────
    # 2026-09-02 correction: the real api-gateway routes are /stockky-hot
    # (Hot Picks, bucketed by driver — bulk_insider_driven/results_driven/
    # news_driven, see api-gateway/main.py's stockky_hot_stocks) and
    # /surprise/ipo/list (see real-trade-service's own
    # candidate_engine/candidates.py _SOURCES map, which already talks to
    # both of these routes for the existing candidate pipeline). Earlier
    # draft of this file used placeholder paths (/hot-picks, /ipo) that
    # don't exist on the real service — fixed to match the routes
    # candidate_engine already calls successfully today.
    # 2026-10-06 (group 177): the log only said "Tier 1 (api-gateway) empty/unavailable", which fits three very
    # different things: the call failed / the breaker is open, the gateway answered but listed no catalysts
    # (normal outside market hours), or only the IPO list broke. Also /stockky-hot and /surprise/ipo/list were
    # fetched one after the other (up to 25 s each) and a failing IPO list threw away a good Hot Picks answer.
    # Now: both are fetched at the same time, an IPO failure keeps the Hot Picks (WATCHLIST_TIER1_IPO_OPTIONAL=0
    # restores the strict behaviour), errors carry their type, and the final line says which case it was.
    outcome = {"reason": "api-gateway call failed or breaker open, and no cached copy"}

    def _why(exc: BaseException) -> str:
        msg = str(exc).strip()
        return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__

    async def _tier1_call():
        async with httpx.AsyncClient(timeout=_TIER1_HTTP_TIMEOUT) as client:
            hot_res, ipo_res = await asyncio.gather(
                client.get(f"{config.API_GATEWAY_URL}/stockky-hot"),
                client.get(f"{config.API_GATEWAY_URL}/surprise/ipo/list"),
                return_exceptions=True,
            )
            if isinstance(hot_res, BaseException):
                raise RuntimeError(f"/stockky-hot {_why(hot_res)}") from hot_res
            try:
                hot_res.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"/stockky-hot {_why(exc)}") from exc
            ipo_json: Any = {}
            try:
                if isinstance(ipo_res, BaseException):
                    raise ipo_res
                ipo_res.raise_for_status()
                ipo_json = ipo_res.json()
            except Exception as exc:  # noqa: BLE001
                if not _ipo_optional():
                    raise RuntimeError(f"/surprise/ipo/list {_why(exc)}") from exc
                logger.warning(
                    "watchlist/sources: /surprise/ipo/list failed (%s) — using Hot Picks without IPO rows",
                    _why(exc),
                )
            return {"hot_picks": hot_res.json(), "ipo": ipo_json}

    async def _tier1_fallback():
        cached = load_snapshot(db, "tier1_hot_picks")
        if cached:
            logger.info("watchlist/sources: Tier 1 api-gateway down — serving local cache")
            outcome["reason"] = "api-gateway down; the cached copy listed no candidates"
        return cached  # None signals caller to try Tier 2

    payload = await api_gateway_breaker.call(_tier1_call, fallback=_tier1_fallback)
    if payload and isinstance(payload, dict) and _hot_is_warming_empty(payload):
        payload = await _wait_for_hot_picks(payload)       # group 226
    if payload:
        save_snapshot(db, "tier1_hot_picks", payload)
        candidates = _normalize_tier1(payload)
        if candidates:
            return candidates
        if outcome["reason"].startswith("api-gateway call failed"):
            outcome["reason"] = "api-gateway answered but listed no catalysts (Hot Picks buckets and IPO list empty)"

    _tier1_msg = "watchlist/sources: Tier 1 (api-gateway) empty/unavailable (%s) — trying Tier 2"
    if outcome["reason"].startswith("api-gateway answered"):
        logger.info(_tier1_msg, outcome["reason"])      # an empty answer is not an outage
    else:
        logger.warning(_tier1_msg, outcome["reason"])

    # ── Tier 2: raw events via event-service + local classify ────────────────
    async def _tier2_call():
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.get(f"{config.EVENT_URL}/events/raw-feed?hours=24")
            r.raise_for_status()
            return r.json()

    tier2_payload = await event_service_breaker.call(_tier2_call, fallback=lambda: None)
    if tier2_payload:
        candidates = _classify_tier2(tier2_payload)
        if candidates:
            logger.info(
                "watchlist/sources: Tier 2 produced %d candidates", len(candidates)
            )
            return candidates

    logger.warning("watchlist/sources: Tier 2 (event-service) empty/unavailable — falling back to Tier 3")

    # ── Tier 3: pure volume-shock (always available while yfinance is up) ────
    return await _tier3_volume_shock()


# ── Tier normalisers ──────────────────────────────────────────────────────────

def _normalize_tier1(payload: dict) -> list[dict]:
    """
    Convert api-gateway /hot-picks + /ipo response into the standard
    candidate dict list. The hot-picks response has sub-buckets keyed by
    driver category; we map those to catalyst_type strings from decay.py.
    """
    out: list[dict] = []

    hot = payload.get("hot_picks") or {}
    # 2026-09-02 correction: individual items in /stockky-hot's buckets carry
    # no per-item detection timestamp (verified against api-gateway/main.py's
    # stockky_hot_stocks) — only a batch-level "generated_at". Use that as
    # catalyst_ts instead of leaving it unset (which would silently fall
    # back to "now" at ingestion time in watchlist.py — close in practice
    # since ingestion happens right after the fetch, but generated_at is the
    # more accurate/honest value when available).
    _batch_ts = _parse_ts(hot.get("generated_at"))
    _bucket_map = {
        "bulk_insider_driven": "bulk_block",
        "results_driven":      "results",
        "news_driven":         "board",
    }
    for bucket_key, ctype in _bucket_map.items():
        for item in (hot.get(bucket_key) or []):
            sym = (item.get("symbol") or "").upper()
            if not sym:
                continue
            # See models.py WatchlistEntry.catalyst_price_source docstring:
            # "close" here means the live "price" field was absent and we
            # fell back to the previous day's close, which can make a
            # later band-check overrun look like a too-tight band when it's
            # really a stale reference price (an overnight gap counted as
            # "move since catalyst").
            price_source = "live" if item.get("price") is not None else (
                "close" if item.get("close") is not None else "unknown"
            )
            out.append({
                "symbol":          sym,
                "catalyst_type":   ctype,
                "catalyst_price":  item.get("price") or item.get("close"),
                "catalyst_price_source": price_source,
                "catalyst_ts":     _batch_ts,
                "source_tier":     1,
                "conviction_score": item.get("score"),
            })

    ipo_data = payload.get("ipo") or {}
    # 2026-09-02 correction: the real /surprise/ipo/list payload (see
    # api-gateway/ipo_scanner.py's get_ipo_list) wraps results under a
    # "results" key, not "items" — checked directly against the source.
    ipo_items = ipo_data if isinstance(ipo_data, list) else (
        ipo_data.get("results") or ipo_data.get("items") or []
    )
    for item in ipo_items:
        sym = (item.get("symbol") or "").upper()
        if not sym:
            continue
        ipo_price = item.get("current_price") or item.get("cmp") or item.get("price")
        # BUG FIX (2026-09-12): ipo_scanner.py's rows never carry a top-level
        # "score" or "cmp"/"price" field — see the matching fix (and full
        # explanation) in candidate_engine/candidates.py's _rows_from_ipo().
        # The real fields are "ipo_score" and "current_price"; "score"/"cmp"/
        # "price" kept as fallbacks only. This function doesn't hard-reject
        # on conviction_score the way _rows_from_ipo() does, but a permanently
        # None conviction_score/catalyst_price for every IPO row here was
        # still silently wrong data reaching the watchlist.
        out.append({
            "symbol":          sym,
            "catalyst_type":   "ipo",
            "catalyst_price":  ipo_price,
            "catalyst_price_source": "live" if ipo_price is not None else "unknown",
            "catalyst_ts":     _parse_ts(item.get("listing_date")),
            "source_tier":     1,
            "conviction_score": item.get("ipo_score") if item.get("ipo_score") is not None else item.get("score"),
        })

    return out


def _classify_tier2(payload: dict) -> list[dict]:
    """
    Classify raw event items from /events/raw-feed using the local
    keyword matcher. No scoring — conviction_score is None.
    """
    from event_depth_local import classify_text

    out: list[dict] = []
    for item in (payload.get("items") or []):
        sym = (item.get("symbol") or "").upper()
        headline = item.get("headline") or item.get("title") or ""
        if not sym or not headline:
            continue
        tags = classify_text(headline)
        if not tags:
            continue
        out.append({
            "symbol":          sym,
            "catalyst_type":   tags[0],
            "catalyst_price":  item.get("price"),
            "catalyst_price_source": "live" if item.get("price") is not None else "unknown",
            "catalyst_ts":     _parse_ts(item.get("ts") or item.get("detected_at")),
            "source_tier":     2,
            "conviction_score": None,
        })
    return out


async def _tier3_volume_shock() -> list[dict]:
    """
    Reuse the existing volume-shock scanner (candidate_engine.candidates).
    Returns minimal dicts; catalyst_price is left as None — entry_engine
    will set it on first price sight.
    """
    try:
        from candidate_engine.candidates import _fetch_volume_shock_universe
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            symbols = await _fetch_volume_shock_universe(client)
        logger.info(
            "watchlist/sources: Tier 3 volume-shock produced %d symbols", len(symbols)
        )
        return [
            {
                "symbol":           s.upper(),
                "catalyst_type":    "volume_shock",
                "catalyst_price":   None,
                "catalyst_price_source": None,  # set to "live" in entry.py once the first real tick establishes it
                "catalyst_ts":      None,
                "source_tier":      3,
                "conviction_score": None,
            }
            for s in symbols
        ]
    except Exception as exc:
        logger.error("watchlist/sources: Tier 3 volume-shock failed: %s", exc)
        return []


# ── Helper ────────────────────────────────────────────────────────────────────

def _parse_ts(value: Any) -> datetime | None:
    """Best-effort parse of a timestamp value (ISO string, epoch int, or None)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except Exception:
            return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(value[:19], fmt)
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
    return None
