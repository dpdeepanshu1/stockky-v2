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

import logging
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
    async def _tier1_call():
        async with httpx.AsyncClient(timeout=_TIER1_HTTP_TIMEOUT) as client:
            hot = await client.get(f"{config.API_GATEWAY_URL}/stockky-hot")
            ipo = await client.get(f"{config.API_GATEWAY_URL}/surprise/ipo/list")
            hot.raise_for_status()
            ipo.raise_for_status()
            return {"hot_picks": hot.json(), "ipo": ipo.json()}

    async def _tier1_fallback():
        cached = load_snapshot(db, "tier1_hot_picks")
        if cached:
            logger.info("watchlist/sources: Tier 1 api-gateway down — serving local cache")
        return cached  # None signals caller to try Tier 2

    payload = await api_gateway_breaker.call(_tier1_call, fallback=_tier1_fallback)
    if payload:
        save_snapshot(db, "tier1_hot_picks", payload)
        candidates = _normalize_tier1(payload)
        if candidates:
            return candidates

    logger.warning("watchlist/sources: Tier 1 (api-gateway) empty/unavailable — trying Tier 2")

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
            out.append({
                "symbol":          sym,
                "catalyst_type":   ctype,
                "catalyst_price":  item.get("price") or item.get("close"),
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
        out.append({
            "symbol":          sym,
            "catalyst_type":   "ipo",
            # real-trade-service's own candidate_engine/candidates.py
            # _rows_from_ipo() reads price as item.get("cmp") or
            # item.get("price") — cmp first — mirrored here.
            "catalyst_price":  item.get("cmp") or item.get("price"),
            "catalyst_ts":     _parse_ts(item.get("listing_date")),
            "source_tier":     1,
            "conviction_score": item.get("score"),
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
