"""
Recent IPO scanner — Surprise tab subsection.

Goal: for a stock that recently listed on NSE (or lists TODAY), decide
whether there's a short-term buy opportunity in it as fast as possible —
ideally right as it opens for trade around 10:00 AM IST on listing day —
and if so, at what price to enter and at what price to book profit.

Pipeline:
  1. Discover recent/listing-today IPOs (NSE's unofficial IPO API, same
     cookie-bootstrap pattern already used in market-data-service/bhavcopy.py,
     with a manual-add fallback since NSE blocks non-browser IPs often
     enough that auto-discovery alone isn't reliable — see add_manual_ipo).
  2. For each, pull price history since listing via the shared bulk
     yfinance path (rate-limited via rate_limiter.py, same as everywhere
     else in this app) and score it.
  3. Map the score to the same five decisions the rest of the app uses
     (BUY NOW / PREPARE TO BUY / HOLD / DO NOT BUY / SELL) and produce a
     BuySuggestion-shaped dict so the frontend can open the existing
     BuySniperModal directly — no new UI plumbing needed for the actual
     "Buy Now" / "Prepare to Buy" action.

Used by:
  POST /surprise/ipo/scan        — trigger a background scan
  GET  /surprise/ipo/status      — scan progress
  GET  /surprise/ipo/list        — current analyzed IPO list
  POST /surprise/ipo/add         — manually register an IPO (symbol, issue
                                    price, listing date) when NSE's API is
                                    blocked or a listing isn't showing up yet
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger("ipo-scanner")

IST = timezone(timedelta(hours=5, minutes=30))

# ── Scoring weights (documented, tunable via env — same convention as
# horizons.py) ────────────────────────────────────────────────────────────
# momentum:        is price still trending up post-listing, or rolling over?
# pullback_quality: pulled back from the post-listing high but still above
#                   issue price = healthy (demand confirmed, room to run).
#                   AT the post-listing high = extended/risky. BELOW issue
#                   price = broken listing, red flag.
# listing_strength: how strong was the Day-1 pop (demand signal), saturating
#                   so an extreme +300% pop isn't linearly "better" than +80%.
# volume_trend:     rising volume since listing = sustained interest, not a
#                   one-day flip-and-forget.
# recency:          fresher IPOs get weighted higher — this is specifically a
#                   listing-momentum play, not a long-term hold.
IPO_WEIGHTS = {
    "momentum": 0.30,
    "pullback_quality": 0.25,
    "listing_strength": 0.20,
    "volume_trend": 0.15,
    "recency": 0.10,
}

BUY_NOW_BAR = float(os.getenv("IPO_BUY_NOW_BAR", "66"))
PREPARE_BAR = float(os.getenv("IPO_PREPARE_BAR", "54"))
DO_NOT_BUY_BAR = float(os.getenv("IPO_DO_NOT_BUY_BAR", "40"))
FRESH_WINDOW_DAYS = int(os.getenv("IPO_FRESH_WINDOW_DAYS", "30"))
# IPO_CHECKER_DEFAULT_DISPLAY_DAYS is the "IPO Checker" tab's default DISPLAY
# filter (last ~1 month) — but the DISCOVERY/scan window below (LOOKBACK_DAYS_MAX
# / HARD_CAP) is intentionally much wider (last ~1 year) so the scan itself
# actually finds every IPO that listed within the last year; the frontend then
# filters the returned list down to the last IPO_CHECKER_DEFAULT_DISPLAY_DAYS
# days by default, with the option to widen the filter without re-scanning.
IPO_CHECKER_DEFAULT_DISPLAY_DAYS = int(os.getenv("IPO_CHECKER_DEFAULT_DISPLAY_DAYS", "30"))
LOOKBACK_DAYS_MAX = int(os.getenv("IPO_LOOKBACK_DAYS_MAX", "365"))
# Hard ceiling regardless of IPO_LOOKBACK_DAYS_MAX — "recent IPO" stops
# meaning anything past a year no matter how that env var is set (previously
# hard-capped at 60 days, which silently dropped nearly every real-world IPO
# candidate from ipoalerts/NSE discovery and made the Scan button look broken
# — it was working, just filtering its own results down to ~nothing).
IPO_LOOKBACK_DAYS_HARD_CAP = int(os.getenv("IPO_LOOKBACK_DAYS_HARD_CAP", "365"))
# Not-yet-listed IPOs (listing today/tomorrow/this week) — how far forward
# to look for those.
IPO_UPCOMING_WINDOW_DAYS = int(os.getenv("IPO_UPCOMING_WINDOW_DAYS", "21"))

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")
FUNDAMENTAL_URL = os.getenv(
    "FUNDAMENTAL_URL",
    os.getenv("ANALYSIS_INTELLIGENCE_URL", "https://analysis-intelligence-service.onrender.com").rstrip("/") + "/fundamental",
).rstrip("/")

NSE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
}

IPO_LIST_KEY = "stockky:ipo:list"          # analyzed results (durable, short TTL)
IPO_MANUAL_KEY = "stockky:ipo:manual"      # user/admin-added IPO entries (durable, long TTL)
IPO_JOB_KEY = "stockky:ipo:job"            # scan progress

# ipoalerts.in — a purpose-built Indian IPO data API (symbol, price band,
# listing date, schedule, GMP on some plans). Confirmed working schema via
# live fetch during development. Without a key it only ever returns one
# demo record regardless of the status filter, so this is optional-but-
# recommended: get a free key at https://ipoalerts.in and set
# IPOALERTS_API_KEY. When unset, this source is skipped entirely and the
# pipeline falls back to NSE's unofficial API (best-effort, frequently
# blocked on cloud IPs) and manual entries — nothing else depends on it.
IPOALERTS_API_KEY = os.getenv("IPOALERTS_API_KEY", "").strip()
IPOALERTS_BASE = "https://api.ipoalerts.in/ipos"

_LOCAL_JOB: Dict[str, Any] = {"status": "idle", "message": "Idle", "processed": 0, "total": 0}

# ── Stop support (mirrors data_feed.py's _DATA_FEED_STOP_FLAG pattern) ──────
# Lets the IPO Checker tab's Stop button halt an in-progress scan between
# symbols instead of waiting for the whole (now up-to-a-year-wide) universe
# to finish.
_IPO_STOP_FLAG = threading.Event()


def request_ipo_stop() -> None:
    _IPO_STOP_FLAG.set()


def clear_ipo_stop() -> None:
    _IPO_STOP_FLAG.clear()


def ipo_stop_requested() -> bool:
    return _IPO_STOP_FLAG.is_set()


def _ipo_db_upsert(rows: List[Dict[str, Any]]) -> int:
    """Persist raw/scored IPO rows to ipo_static_feed (Neon on Render, Oracle
    on the Oracle deployment — same ensure_ipo_schema()/dialect() selection
    as surprise_static_feed). Best-effort: a DB write failure here must never
    break the scan — kv_cache remains the fast-path source the endpoints
    actually serve from; this table is the durable "what did we last see"
    record for debugging + the 24h freshness check."""
    try:
        import ipo_schema
    except Exception:
        return 0
    url = ipo_schema.database_url()
    if not url or not rows:
        return 0
    eng = None
    try:
        from sqlalchemy import text
        import json as _json

        dial = ipo_schema.dialect()
        eng = ipo_schema.make_engine("stockky-ipo-writer")
        if eng is None:
            return 0
        payload = []
        for r in rows:
            row = {k: r.get(k) for k in ipo_schema.ROW_KEYS}
            row["listing_date"] = row.get("listing_date") or ""
            bs = r.get("buy_suggestion")
            row["buy_suggestion_json"] = _json.dumps(bs) if bs else None
            payload.append(row)
        payload = ipo_schema.adapt_rows(payload, dial)
        stmt = text(ipo_schema.upsert_sql(dial))
        n = 0
        if dial == "oracle":
            with eng.begin() as conn:
                for row in payload:
                    conn.execute(stmt, row)
                    n += 1
        else:
            with eng.begin() as conn:
                conn.execute(stmt, payload)
                n = len(payload)
        return n
    except Exception as e:
        logger.warning("ipo db upsert failed (non-fatal, kv cache still served): %s", e)
        return 0
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except Exception:
                pass


def _ipo_db_freshness_hours() -> Optional[float]:
    """Hours since ipo_static_feed was last written, or None if empty/unavailable.
    Used the same way surprise_premarket's freshness check is used: a scan
    within IPO_DB_FRESH_HOURS (default 24) of the last one can reuse the
    table instead of re-hitting NSE, exactly like the premarket baselines."""
    try:
        import ipo_schema
        from sqlalchemy import text

        eng = ipo_schema.make_engine("stockky-ipo-freshness")
        if eng is None:
            return None
        try:
            with eng.connect() as conn:
                dial = ipo_schema.dialect()
                cnt = conn.execute(text(ipo_schema.table_exists_sql(dial)), {"tbl": "ipo_static_feed"}).scalar()
                if not cnt:
                    return None
                now_expr = "SYSTIMESTAMP" if dial == "oracle" else "NOW()"
                row = conn.execute(text(f"SELECT MAX(updated_at) AS last_at FROM ipo_static_feed")).fetchone()
                last_at = row[0] if row else None
                if last_at is None:
                    return None
                from datetime import datetime as _dt, timezone as _tz
                now = _dt.now(_tz.utc)
                la = last_at if getattr(last_at, "tzinfo", None) else last_at.replace(tzinfo=_tz.utc)
                return (now - la).total_seconds() / 3600.0
        finally:
            eng.dispose()
    except Exception as e:
        logger.debug("ipo db freshness check failed: %s", e)
        return None


IPO_DB_FRESH_HOURS = float(os.getenv("IPO_DB_FRESH_HOURS", "24"))


def _kv():
    import kv_cache
    return kv_cache


def _set_job(**kw) -> dict:
    _LOCAL_JOB.update(kw)
    _LOCAL_JOB["updated_at"] = datetime.now(IST).isoformat()
    try:
        _kv().kv_set(IPO_JOB_KEY, dict(_LOCAL_JOB), ttl=3600)
    except Exception:
        pass
    return dict(_LOCAL_JOB)


def get_ipo_scan_progress() -> dict:
    try:
        durable = _kv().kv_get(IPO_JOB_KEY)
        if isinstance(durable, dict):
            return {**durable, **_LOCAL_JOB} if _LOCAL_JOB.get("status") == "running" else durable
    except Exception:
        pass
    return dict(_LOCAL_JOB)


# ── NSE IPO calendar discovery (best-effort; manual add is the reliable path) ──

def fetch_ipoalerts_calendar() -> List[Dict[str, Any]]:
    """
    Primary, reliable IPO discovery source when IPOALERTS_API_KEY is set —
    confirmed live schema: {symbol, name, type, listingDate, priceRange,
    startDate, endDate, issueSize, schedule[...]}. priceRange is a band
    like "95-99"; we take the upper bound as issue_price, matching NSE
    convention (retail investors pay the cap price on allotment).
    Queries a few status buckets defensively since the exact vocabulary
    for "already listed" isn't guaranteed stable — analyze_ipo() stages
    everything off the parsed listing_date itself, not this status string,
    so any mismatch here only affects which bucket we bothered to ask for,
    never the actual pre-listing/listing-day/listed classification.
    """
    if not IPOALERTS_API_KEY:
        return []

    try:
        from rate_limiter import acquire as rl_acquire
        rl_acquire("indianapi", weight=1)  # shares the conservative IndianAPI-style bucket
    except Exception:
        pass

    out: List[Dict[str, Any]] = []
    headers = {"X-API-KEY": IPOALERTS_API_KEY, "Accept": "application/json"}
    for status in ("open", "listed", "upcoming", "closed"):
        try:
            r = httpx.get(IPOALERTS_BASE, params={"status": status}, headers=headers, timeout=15)
            if r.status_code != 200:
                logger.info("ipoalerts status=%s -> HTTP %s", status, r.status_code)
                continue
            data = r.json()
            for row in data.get("ipos") or []:
                norm = _normalize_ipoalerts_row(row, status)
                if norm:
                    out.append(norm)
        except Exception as e:
            logger.info("ipoalerts status=%s fetch failed: %s", status, e)

    # De-dupe by symbol, keep first occurrence
    seen = set()
    deduped = []
    for r in out:
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        deduped.append(r)
    return deduped


def _normalize_ipoalerts_row(row: Dict[str, Any], status: str) -> Optional[Dict[str, Any]]:
    try:
        symbol = str(row.get("symbol") or "").upper().strip()
        if not symbol:
            return None
        price_range = str(row.get("priceRange") or "")
        issue_price = None
        if price_range:
            try:
                issue_price = float(price_range.replace(",", "").split("-")[-1].strip())
            except (TypeError, ValueError):
                issue_price = None
        # GMP (grey market premium) — only present on ipoalerts' paid plans
        # per their docs; absent on the free tier, which is fine, this is
        # opportunistic. Accept a couple of plausible field-name spellings
        # since their schema for this field specifically isn't confirmed
        # the way the rest of this row is (see module note above).
        gmp = row.get("gmp")
        if gmp is None:
            gmp = row.get("greyMarketPremium")
        if isinstance(gmp, dict):
            gmp = gmp.get("value") or gmp.get("premium")
        try:
            gmp = float(gmp) if gmp is not None else None
        except (TypeError, ValueError):
            gmp = None
        return {
            "symbol": symbol,
            "company_name": row.get("name") or symbol,
            "issue_price": issue_price,
            "listing_date": row.get("listingDate"),
            "status": status,
            "source": "ipoalerts",
            "subscription_times": row.get("subscriptionTimes") or (row.get("subscription") or {}).get("total"),
            "gmp": gmp,
        }
    except Exception:
        return None


def _nse_session() -> httpx.Client:
    c = httpx.Client(timeout=20, headers=NSE_HEADERS, follow_redirects=True)
    try:
        c.get("https://www.nseindia.com")
        c.get("https://www.nseindia.com/market-data/all-upcoming-issues-ipo")
    except Exception as e:
        logger.debug("nse session bootstrap: %s", e)
    return c


def fetch_nse_ipo_calendar() -> List[Dict[str, Any]]:
    """
    Best-effort discovery of recent/current NSE mainboard + SME IPOs.
    NSE's IPO endpoints are unofficial and frequently block cloud-hosted IPs
    (Render, AWS, etc.) even with a proper cookie handshake — that's exactly
    why add_manual_ipo() exists as the reliable path. When this succeeds it
    saves you from typing anything in; when it doesn't, nothing else in this
    pipeline depends on it.
    """
    try:
        from rate_limiter import acquire as rl_acquire
        rl_acquire("nse", weight=1)
    except Exception:
        pass

    out: List[Dict[str, Any]] = []
    candidates = [
        ("https://www.nseindia.com/api/public-past-issues", "past"),
        ("https://www.nseindia.com/api/all-upcoming-issues?category=ipo", "upcoming"),
    ]
    # try/finally + manual close instead of `with _nse_session() as c:` —
    # a `with` block calls __exit__ (closing the client) once; if anything
    # upstream ever re-enters this same client object (e.g. a retry
    # wrapper, or this function being invoked twice concurrently on a
    # cached/shared instance), httpx raises "Cannot open a client instance
    # more than once". _nse_session() already returns a fresh client per
    # call so that shouldn't happen here, but try/finally is strictly
    # safer than `with` for this exact failure mode and costs nothing.
    c = _nse_session()
    try:
        for url, kind in candidates:
            try:
                r = c.get(url, timeout=15)
                if r.status_code != 200:
                    logger.info("NSE IPO calendar %s -> HTTP %s (blocked/unavailable)", kind, r.status_code)
                    continue
                data = r.json()
                rows = data if isinstance(data, list) else data.get("data") or []
                for row in rows:
                    norm = _normalize_nse_row(row, kind)
                    if norm:
                        out.append(norm)
            except Exception as e:
                logger.info("NSE IPO calendar %s fetch failed: %s", kind, e)
    except Exception as e:
        logger.warning("NSE IPO session failed entirely: %s", e)
    finally:
        try:
            c.close()
        except Exception:
            pass

    return out


def _extract_price_band_upper(raw: Any) -> Optional[float]:
    """
    Extracts the upper bound of an issue-price band regardless of the
    separator/format NSE's two endpoints use — both of these need to work:
      - public-past-issues:      "    97"            (plain number, padded)
      - all-upcoming-issues:     "Rs.750 to Rs.788"  ("Rs." prefix, "to" sep)
    The previous implementation only handled a bare number or an "X-Y"
    dash-separated band; it silently produced None (via the ValueError on
    float("Rs.750 to Rs.788")) for every "Rs.X to Rs.Y" row, which is the
    only format all-upcoming-issues ever returns — so every currently-open
    or forthcoming IPO's issue_price came back None and got dropped by
    _merged_ipo_universe's `and m.get("issue_price")` filter even though
    the row itself was fetched successfully (this is the "returns the
    correct record ... but it's not taking it" bug).
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    nums = re.findall(r"[\d,]+(?:\.\d+)?", str(raw))
    if not nums:
        return None
    try:
        return float(nums[-1].replace(",", ""))
    except ValueError:
        return None


def _normalize_nse_row(row: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    try:
        symbol = str(
            row.get("symbol") or row.get("scriptCode") or row.get("companyName") or row.get("company") or ""
        ).upper().strip()
        if not symbol:
            return None
        issue_price = _extract_price_band_upper(
            row.get("issuePrice") or row.get("priceRange") or row.get("cutOffPrice") or row.get("finalIssuePrice")
        )
        listing_date = row.get("listingDate") or row.get("dateOfListing")
        issue_end = row.get("issueEndDate") or row.get("ipoEndDate")
        status_raw = str(row.get("status") or "").strip().lower()
        listing_estimated = False
        if not listing_date:
            # all-upcoming-issues never carries a listingDate field at all —
            # it only exists once the IPO has actually listed (i.e. it would
            # then show up on public-past-issues instead). Previously this
            # meant every still-open/forthcoming IPO got silently dropped by
            # _merged_ipo_universe's "must have listing_date" filter. NSE's
            # own norm is T+3 listing after the issue closes (T+6 pre-2023);
            # use issueEndDate + 3 working days as a placeholder so these
            # rows survive the recency filter and reach the scorer/UI as
            # "upcoming" instead of vanishing — the estimate is corrected
            # automatically once the real listingDate appears (the symbol
            # then also starts showing up on public-past-issues and
            # overwrites this row via _merged_ipo_universe's by_symbol map).
            end_dt = _parse_date(issue_end)
            if end_dt is not None:
                listing_date = (end_dt + timedelta(days=3)).strftime("%Y-%m-%d")
                listing_estimated = True
        stage = (
            "listed" if (listing_date and not listing_estimated)
            else "upcoming" if (listing_estimated or status_raw in ("active", "forthcoming") or issue_end)
            else kind
        )
        return {
            "symbol": symbol,
            "company_name": row.get("companyName") or row.get("company") or symbol,
            "issue_price": issue_price,
            "listing_date": listing_date,
            "listing_date_estimated": listing_estimated,
            "issue_start_date": row.get("issueStartDate") or row.get("ipoStartDate"),
            "issue_end_date": issue_end,
            "nse_status": row.get("status"),
            "stage": stage,
            "status": kind,
            "source": "nse_auto",
        }
    except Exception:
        return None


def add_manual_ipo(
    symbol: str,
    issue_price: float,
    listing_date: str,
    company_name: Optional[str] = None,
    subscription_times: Optional[float] = None,
    gmp: Optional[float] = None,
) -> dict:
    """Register an IPO by hand — the reliable path when NSE's auto-discovery
    is blocked, or to get a symbol in front of the scorer before NSE's own
    listing feed updates. listing_date format: YYYY-MM-DD."""
    sym = symbol.upper().replace(".NS", "").replace(".BO", "").strip()
    entry = {
        "symbol": sym,
        "company_name": company_name or sym,
        "issue_price": float(issue_price),
        "listing_date": listing_date,
        "subscription_times": subscription_times,
        "gmp": gmp,
        "status": "manual",
        "source": "manual",
        "added_at": datetime.now(IST).isoformat(),
    }
    try:
        existing = _kv().kv_get(IPO_MANUAL_KEY)
        entries = existing if isinstance(existing, list) else []
    except Exception:
        entries = []
    entries = [e for e in entries if e.get("symbol") != sym]
    entries.append(entry)
    try:
        _kv().kv_set(IPO_MANUAL_KEY, entries, ttl=90 * 86400)
    except Exception as e:
        logger.warning("could not persist manual IPO %s: %s", sym, e)
    return entry


def _manual_ipos() -> List[Dict[str, Any]]:
    try:
        existing = _kv().kv_get(IPO_MANUAL_KEY)
        return existing if isinstance(existing, list) else []
    except Exception:
        return []


def _merged_ipo_universe() -> List[Dict[str, Any]]:
    """
    Merge order, most-trusted-wins on symbol collision:
      1. manual entries       — you told the system directly, always wins
      2. ipoalerts.in         — real confirmed schema, needs a free API key
      3. NSE unofficial API   — best-effort, unverified/frequently blocked

    Recency filter: ipoalerts' "listed"/"closed" status buckets return
    every IPO in their database with no date bound — this is why the scan
    was processing 1000+ candidates (most listed years ago) instead of the
    handful that are actually "recent". This is a short-term listing-
    momentum scanner (see module docstring), so anything outside the
    window below is dropped before analysis even starts:
      - already listed: keep only the last IPO_LOOKBACK_DAYS_MAX days
        (default 30, hard-capped at IPO_LOOKBACK_DAYS_HARD_CAP = 60 —
        "max 2 months")
      - not yet listed: keep only the next IPO_UPCOMING_WINDOW_DAYS days
        (default 7) so "listing today/tomorrow" IPOs are still included
    Manual entries always pass through regardless of date — if you added
    it by hand, you meant to track it.
    """
    ipoalerts = fetch_ipoalerts_calendar()
    nse = fetch_nse_ipo_calendar()
    manual = _manual_ipos()

    by_symbol: Dict[str, Dict[str, Any]] = {}
    for a in nse:
        by_symbol[a["symbol"]] = a
    for a in ipoalerts:
        by_symbol[a["symbol"]] = a  # ipoalerts overrides NSE guess
    manual_symbols = {m["symbol"] for m in manual if m.get("symbol")}
    for m in manual:
        by_symbol[m["symbol"]] = m  # manual overrides everything

    merged = list(by_symbol.values())
    # Drop anything without a listing date/issue price — can't score it
    dated = [m for m in merged if m.get("listing_date") and m.get("issue_price")]

    lookback = min(IPO_LOOKBACK_DAYS_MAX, IPO_LOOKBACK_DAYS_HARD_CAP)
    now = datetime.now(IST)
    recent: List[Dict[str, Any]] = []
    for m in dated:
        if m["symbol"] in manual_symbols:
            recent.append(m)
            continue
        if m.get("stage") == "upcoming":
            # Still open/forthcoming per NSE's own status field — always
            # relevant regardless of the estimated listing date's exact
            # day-count (see _normalize_nse_row); this is what fixes
            # "the correct record comes back from NSE but never makes it
            # into the scan" for every currently-open or forthcoming IPO.
            recent.append(m)
            continue
        dt = _parse_date(m.get("listing_date"))
        if dt is None:
            continue  # unparseable date — can't tell if it's recent, skip
        dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt
        age_days = (now - dt).days
        if -IPO_UPCOMING_WINDOW_DAYS <= age_days <= lookback:
            recent.append(m)

    if len(dated) != len(recent):
        logger.info(
            "ipo universe: %s candidates -> %s within last %sd/next %sd window",
            len(dated), len(recent), lookback, IPO_UPCOMING_WINDOW_DAYS,
        )
    return recent


# ── Per-symbol analysis ────────────────────────────────────────────────────

def _parse_date(d: Any) -> Optional[datetime]:
    if not d:
        return None
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(str(d)[:11].strip(), fmt)
        except Exception:
            continue
    try:
        return datetime.fromisoformat(str(d)[:10])
    except Exception:
        return None


def _fetch_history(symbol: str, days: int) -> Optional[Any]:
    """Bulk-safe single-symbol history pull.

    Routed through market-data-service's /history/{symbol} endpoint instead
    of calling yfinance directly here. That endpoint already has retries,
    caching and index-fallback candidates, and (as of this patch) will grow
    the same non-Yahoo waterfall the /quote endpoint has — so IPO analysis
    stops going straight to "error" the moment Yahoo's crumb/cookie flow
    hiccups. Falls back to a direct yfinance call only if market-data-service
    itself is unreachable.

    Passes `days` straight through to /history's exact start=/end= window
    instead of snapping to a named period bucket (1mo/3mo/6mo/...). A stock
    that listed 6 days ago asking for a fixed "1mo"/"3mo" period doesn't
    gain anything — there's no more history to return either way — but it
    does add unnecessary Yahoo request weight for some tickers, and for
    tickers Yahoo has thin coverage on (common for NSE SME-platform
    listings) the exact short window is more likely to come back with
    *something* than a request shaped for a month of data that doesn't
    exist yet.
    """
    try:
        r = httpx.get(
            f"{MARKET_DATA_URL}/history/{symbol}",
            params={"days": max(1, int(days)), "interval": "1d"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json() or {}
            candles = data.get("candles") or data.get("data") or []
            if candles:
                import pandas as pd
                df = pd.DataFrame(candles)
                # Normalize column names to what analyze_ipo expects (Close/High/Low/Volume)
                rename = {
                    "close": "Close", "high": "High", "low": "Low",
                    "open": "Open", "volume": "Volume", "date": "Date",
                }
                df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})
                if "Date" in df.columns:
                    df["Date"] = pd.to_datetime(df["Date"])
                    df = df.set_index("Date")
                if not df.empty and "Close" in df.columns:
                    return df
    except Exception as e:
        logger.debug("ipo history via market-data-service %s: %s", symbol, e)

    # Fallback: direct yfinance (only reached if market-data-service is down/unreachable)
    try:
        import yfinance as yf
        from symbol_aliases import resolve_ns_ticker
        yf_ticker = resolve_ns_ticker(symbol)
        if not yf_ticker:
            return None  # known non-NSE / renamed-unresolvable — don't waste the call
        t = yf.Ticker(yf_ticker)
        hist = t.history(period=f"{max(5, days)}d", interval="1d", auto_adjust=True)
        if hist is None or hist.empty:
            return None
        return hist
    except Exception as e:
        logger.debug("ipo history direct-yfinance fallback %s: %s", symbol, e)
        return None


def _quote_now(symbol: str) -> Optional[float]:
    try:
        r = httpx.get(f"{MARKET_DATA_URL}/quote/{symbol}", timeout=8)
        if r.status_code == 200:
            data = r.json()
            px = data.get("price") or data.get("regularMarketPrice") or data.get("close") or data.get("last")
            if px:
                return float(px)
    except Exception as e:
        logger.debug("ipo quote_now %s: %s", symbol, e)
    return None


def _fetch_ipo_fundamentals(symbol: str) -> Optional[Dict[str, Any]]:
    """
    Best-effort profit/loss snapshot for a freshly-listed company, reused
    from the same analysis-intelligence-service /analyze/{symbol} endpoint
    the rest of the app already calls (fundamental fill, Data Feed repair).
    A stock that IPO'd days ago often has thin/no analyst coverage yet, so
    this frequently comes back empty for very fresh SME listings — that's
    expected, not an error, and callers should treat a None/empty result
    as "not available yet" rather than retry aggressively.
    """
    try:
        from rate_limiter import acquire as rl_acquire
        rl_acquire("analysis", weight=1)
    except Exception:
        pass
    try:
        r = httpx.get(f"{FUNDAMENTAL_URL}/analyze/{symbol}", timeout=15)
        if r.status_code != 200:
            return None
        data = r.json() or {}
        metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else data
        if not isinstance(metrics, dict):
            return None
        # Just the P&L-relevant fields — analyze_ipo doesn't need the full
        # fundamental blob, and a smaller snapshot keeps the IPO list
        # payload light (see the /api/scan/find-buys 413 fix for why that
        # matters).
        keys = (
            "revenue", "net_profit", "profit", "pat", "eps",
            "revenue_growth_pct", "profit_growth_pct", "pe_ratio", "roce",
            "debt_to_equity",
        )
        snapshot = {k: metrics.get(k) for k in keys if metrics.get(k) is not None}
        return snapshot or None
    except Exception as e:
        logger.debug("ipo fundamentals %s: %s", symbol, e)
        return None


def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def analyze_ipo(entry: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """Full analysis for one IPO entry — returns a dict with the score
    breakdown plus a BuySuggestion-shaped block (see buy_sniper.py) so the
    frontend can feed it straight into the existing BuySniperModal."""
    now = now or datetime.now(IST)
    symbol = entry["symbol"]
    issue_price = float(entry["issue_price"])
    listing_dt = _parse_date(entry.get("listing_date"))
    company_name = entry.get("company_name") or symbol

    result: Dict[str, Any] = {
        "symbol": symbol,
        "company_name": company_name,
        "issue_price": issue_price,
        "listing_date": entry.get("listing_date"),
        "source": entry.get("source", "manual"),
        "subscription_times": entry.get("subscription_times"),
        "gmp": entry.get("gmp"),
    }

    if listing_dt is None:
        result["stage"] = "unknown"
        result["error"] = "Could not parse listing_date"
        return result

    days_since_listing = (now.replace(tzinfo=None) - listing_dt).days
    result["days_since_listing"] = days_since_listing

    # ── Pre-listing stage: nothing has traded yet ──
    if days_since_listing < 0:
        result["stage"] = "upcoming"
        result["message"] = f"Lists on {entry.get('listing_date')} — not yet tradable."
        return result

    if days_since_listing == 0:
        current_px = _quote_now(symbol)
        if current_px is None:
            result["stage"] = "pre_listing"
            result["message"] = "Lists today — no trade printed yet (check again after 10:00 AM IST)."
            # Early advisory from subscription strength and/or GMP, if we have them.
            sub = entry.get("subscription_times")
            gmp = entry.get("gmp")
            parts = []
            if sub is not None:
                sub_score = _clamp(50 + min(sub, 50) * 1.0)  # 1x=50 (neutral), 50x+=~100
                parts.append(sub_score)
            if gmp is not None and issue_price:
                # GMP as % of issue price is a rough proxy for expected listing pop —
                # same 50=neutral / saturating shape as the subscription score above.
                gmp_pct = (gmp / issue_price) * 100.0
                gmp_score = _clamp(50 + min(max(gmp_pct, -50), 100) * 0.5)
                parts.append(gmp_score)
                result["gmp_pct_of_issue"] = round(gmp_pct, 1)
            if parts:
                sub_score = sum(parts) / len(parts)
                result["pre_listing_advisory_score"] = round(sub_score, 1)
                result["pre_listing_advisory"] = (
                    "Strong subscription/GMP — historically correlates with a firm listing pop."
                    if sub_score >= 66 else
                    "Moderate/weak subscription/GMP — listing pop uncertain, watch first 15 minutes of trade."
                )
            return result
        # First trade of the day is in — treat like "listing day, live"
        result["stage"] = "listing_day"
        listing_pop_pct = (current_px - issue_price) / issue_price * 100.0
        result["current_price"] = round(current_px, 2)
        result["listing_pop_pct"] = round(listing_pop_pct, 2)
        # Not enough history yet for momentum/volume trend — score off pop + subscription only
        listing_strength = _clamp(50 + min(max(listing_pop_pct, -50), 150) * 0.4)
        sub = entry.get("subscription_times")
        if sub is not None:
            listing_strength = _clamp(0.6 * listing_strength + 0.4 * _clamp(50 + min(sub, 50)))
        score = listing_strength  # single-signal score on listing day itself
        result["ipo_score"] = round(score, 1)
        result["score_breakdown"] = {"listing_strength": round(listing_strength, 1)}
        decision = _decision_for_score(score)
        result["decision"] = decision
        result["buy_suggestion"] = _build_ipo_suggestion(
            symbol, company_name, current_px, decision, score,
            atr_pct=0.05, rationale=f"Listing day, pop {listing_pop_pct:+.1f}% vs issue price ₹{issue_price}.",
        )
        return result

    # ── Post-listing: we have real history ──
    # days_since_listing + 2 (not +5): request only the days that could
    # actually exist, not a padded window — a 6-day-old listing asking for
    # 11 days of history gains nothing and is exactly the mismatch that
    # was producing "possibly delisted" noise for names that simply don't
    # have that much trading history yet.
    hist = _fetch_history(symbol, min(days_since_listing + 2, LOOKBACK_DAYS_MAX))
    if hist is None or hist.empty:
        result["stage"] = "error"
        result["message"] = "Could not fetch post-listing price history"
        return result

    # Best-effort P&L snapshot — most freshly-listed names won't have
    # analyst coverage yet, so this is allowed to come back empty.
    fundamentals = _fetch_ipo_fundamentals(symbol)
    if fundamentals:
        result["fundamentals_snapshot"] = fundamentals

    closes = hist["Close"].astype("float64")
    highs = hist["High"].astype("float64")
    lows = hist["Low"].astype("float64")
    volumes = hist["Volume"].astype("float64")

    listing_day_close = float(closes.iloc[0])
    listing_day_low = float(lows.iloc[0])
    current_price = float(closes.iloc[-1])
    post_listing_high = float(highs.max())

    listing_pop_pct = (listing_day_close - issue_price) / issue_price * 100.0
    current_vs_issue_pct = (current_price - issue_price) / issue_price * 100.0
    current_vs_high_pct = (current_price - post_listing_high) / post_listing_high * 100.0  # <=0

    n = len(closes)
    momentum_5d = 0.0
    if n >= 2:
        window = min(5, n)
        recent = closes.iloc[-window:]
        momentum_5d = (float(recent.iloc[-1]) - float(recent.iloc[0])) / float(recent.iloc[0]) * 100.0

    vol_trend = 1.0
    if n >= 4:
        early = float(volumes.iloc[: max(1, n // 3)].mean())
        late = float(volumes.iloc[-max(1, n // 3):].mean())
        if early > 0:
            vol_trend = late / early

    atr_pct = float((highs - lows).tail(min(10, n)).mean() / max(current_price, 1e-6))

    # ── Composite score ──
    # momentum: +10%/5d -> ~100, -10%/5d -> ~0, 0 -> 50
    s_momentum = _clamp(50 + momentum_5d * 5)
    # pullback_quality: sweet spot is 5-20% below post-listing high, still
    # above issue price. At the high (0% pullback) = extended/risky. Below
    # issue price = broken listing.
    if current_vs_issue_pct < 0:
        s_pullback = _clamp(30 + current_vs_issue_pct)  # already negative, pushes lower
    else:
        pullback_from_high = abs(current_vs_high_pct)
        if pullback_from_high <= 2:
            s_pullback = 55.0  # right at the high — fine, but not the safest entry
        elif pullback_from_high <= 20:
            s_pullback = 85.0  # healthy consolidation off the high — best zone
        else:
            s_pullback = _clamp(85 - (pullback_from_high - 20) * 1.5)  # faded too far
    # listing_strength: saturating function of the Day-1 pop
    s_listing = _clamp(50 + math.copysign(min(abs(listing_pop_pct), 150) ** 0.6 * 4.0, listing_pop_pct))
    # volume_trend: rising volume is good, up to a point
    s_volume = _clamp(50 + (vol_trend - 1.0) * 40)
    # recency: full marks inside FRESH_WINDOW_DAYS, decays after
    s_recency = _clamp(100 - max(0, days_since_listing - 5) * (50.0 / FRESH_WINDOW_DAYS))

    breakdown = {
        "momentum": round(s_momentum, 1),
        "pullback_quality": round(s_pullback, 1),
        "listing_strength": round(s_listing, 1),
        "volume_trend": round(s_volume, 1),
        "recency": round(s_recency, 1),
    }
    score = sum(breakdown[k] * w for k, w in IPO_WEIGHTS.items())
    score = _clamp(score)

    decision = _decision_for_score(score, current_vs_issue_pct=current_vs_issue_pct)

    result.update({
        "stage": "listed",
        "current_price": round(current_price, 2),
        "listing_day_close": round(listing_day_close, 2),
        "post_listing_high": round(post_listing_high, 2),
        "listing_pop_pct": round(listing_pop_pct, 2),
        "current_vs_issue_pct": round(current_vs_issue_pct, 2),
        "current_vs_high_pct": round(current_vs_high_pct, 2),
        "momentum_5d_pct": round(momentum_5d, 2),
        "volume_trend_ratio": round(vol_trend, 2),
        "atr_pct": round(atr_pct * 100, 2),
        "ipo_score": round(score, 1),
        "score_breakdown": breakdown,
        "decision": decision,
    })

    rationale = (
        f"IPO score {round(score)}/100 — {days_since_listing}d since listing, "
        f"{current_vs_issue_pct:+.1f}% vs issue price, "
        f"{current_vs_high_pct:.1f}% off post-listing high, "
        f"5d momentum {momentum_5d:+.1f}%."
    )
    result["buy_suggestion"] = _build_ipo_suggestion(
        symbol, company_name, current_price, decision, score,
        atr_pct=max(0.02, atr_pct), rationale=rationale,
        listing_day_low=listing_day_low, post_listing_high=post_listing_high,
    )
    return result


def _decision_for_score(score: float, current_vs_issue_pct: Optional[float] = None) -> str:
    # SELL only makes sense if you already hold it and the listing has
    # broken down hard below issue price with a weak score — otherwise
    # "not worth buying" is DO NOT BUY, not SELL (this pipeline doesn't know
    # if the user actually holds the stock).
    if current_vs_issue_pct is not None and current_vs_issue_pct < -15 and score < DO_NOT_BUY_BAR:
        return "SELL"
    if score >= BUY_NOW_BAR:
        return "BUY NOW"
    if score >= PREPARE_BAR:
        return "PREPARE TO BUY"
    if score >= DO_NOT_BUY_BAR:
        return "HOLD"
    return "DO NOT BUY"


def _build_ipo_suggestion(
    symbol: str,
    company_name: str,
    current_price: float,
    decision: str,
    score: float,
    atr_pct: float,
    rationale: str,
    listing_day_low: Optional[float] = None,
    post_listing_high: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Same shape as buy_sniper.build_suggestion() so the frontend can feed
    this straight into the existing BuySniperModal — no new modal needed."""
    if decision not in ("BUY NOW", "PREPARE TO BUY"):
        return None

    # Expected move scales with conviction and the stock's own volatility —
    # a jumpy small-cap IPO can realistically move more in a few days than
    # a steady large-cap, so this isn't a flat percentage for every IPO.
    conviction_mult = 0.4 + (score - PREPARE_BAR) / (100 - PREPARE_BAR) * 1.1  # ~0.4-1.5
    expected_move_pct = _clamp(atr_pct * 100 * 2.2 * conviction_mult, 3.0, 25.0)
    target = round(current_price * (1 + expected_move_pct / 100.0), 2)

    # Stop: below listing-day low (structural support) if available and
    # tighter than a flat ATR-based stop, else fall back to ATR-based.
    atr_stop = round(current_price * (1 - max(0.04, atr_pct * 1.5)), 2)
    stop = atr_stop
    if listing_day_low and listing_day_low < current_price:
        structural_stop = round(listing_day_low * 0.99, 2)
        if structural_stop > atr_stop:
            stop = structural_stop

    buy_low = round(current_price * 0.995, 2)
    buy_high = round(current_price * 1.01, 2)
    profit_abs = round(target - current_price, 2)

    action = "BUY NOW" if decision == "BUY NOW" else "BUY ON 15M BREAKOUT"
    holding_duration = "2 to 7 Trading Days"  # IPO momentum plays run a bit longer than a normal surprise setup

    return {
        "symbol": symbol,
        "action": action,
        "buy_price_range": f"₹{buy_low} - ₹{buy_high}",
        "buy_price_low": buy_low,
        "buy_price_high": buy_high,
        "entry_time": "Next Trading Session (09:25 AM - 09:45 AM)" if action != "BUY NOW" else "Now",
        "entry_window": "09:25 AM - 09:45 AM IST",
        "target_price": target,
        "stop_loss": stop,
        "estimated_profit": f"+{expected_move_pct:.1f}% (₹{profit_abs}/share)",
        "estimated_profit_pct": round(expected_move_pct, 1),
        "holding_duration": holding_duration,
        "holding_period": holding_duration,
        "conviction_score": int(round(score)),
        "price": current_price,
        "sector": None,
        "rationale": rationale,
        "decision": decision,
    }


# ── Background scan job ────────────────────────────────────────────────────
_IPO_SCAN_LOCK = threading.Lock()


def run_ipo_scan(force: bool = False) -> dict:
    # Guard against overlapping scans (e.g. the UI's poll + a manual
    # "Run Premarket Feed" click landing close together, or a page refresh
    # re-firing the trigger before the previous run finished). Two scans
    # running at once share no state but do double the NSE/IndianAPI call
    # volume for no benefit, and made "Cannot open a client instance more
    # than once"-style races far more likely to surface under load even
    # though each call site creates its own client. non_blocking acquire:
    # if a scan is already running, just report that instead of queuing.
    if not _IPO_SCAN_LOCK.acquire(blocking=False):
        logger.info("run_ipo_scan: scan already in progress, skipping duplicate trigger")
        return {**get_ipo_scan_progress(), "skipped": True, "reason": "scan already running"}
    try:
        return _run_ipo_scan_locked(force=force)
    finally:
        _IPO_SCAN_LOCK.release()


def _run_ipo_scan_locked(force: bool = False) -> dict:
    clear_ipo_stop()
    try:
        import ipo_schema
        ipo_schema.ensure_ipo_schema()
    except Exception as e:
        logger.info("ipo schema ensure skipped/failed (non-fatal): %s", e)

    if not force:
        age_h = _ipo_db_freshness_hours()
        if age_h is not None and age_h < IPO_DB_FRESH_HOURS:
            cached = None
            try:
                cached = _kv().kv_get(IPO_LIST_KEY)
            except Exception:
                cached = None
            if isinstance(cached, dict) and cached.get("results"):
                return _set_job(
                    status="skipped_fresh",
                    message=(
                        f"ipo_static_feed is {age_h:.1f}h old (< {IPO_DB_FRESH_HOURS:.0f}h) — "
                        f"reused stored data instead of re-hitting NSE (force=true to override)"
                    ),
                    processed=len(cached.get("results") or []),
                    total=len(cached.get("results") or []),
                )

    universe = _merged_ipo_universe()
    total = len(universe)
    _set_job(status="running", message=f"Scanning {total} IPO(s)…", processed=0, total=total,
              started_at=datetime.now(IST).isoformat())

    results: List[Dict[str, Any]] = []
    errors = 0
    stopped = False
    for i, entry in enumerate(universe):
        if ipo_stop_requested():
            stopped = True
            logger.info("run_ipo_scan: stop requested at %s/%s", i, total)
            break
        try:
            r = analyze_ipo(entry)
            results.append(r)
        except Exception as e:
            errors += 1
            logger.warning("ipo analyze failed for %s: %s", entry.get("symbol"), e)
        processed = i + 1
        _set_job(
            status="running",
            processed=processed,
            total=total,
            message=f"{processed}/{total} · {entry.get('symbol')}",
        )

    if stopped:
        # Persist whatever was analyzed before Stop was pressed instead of
        # discarding it — same "partial results are still useful" behaviour
        # as the Surprise premarket job.
        try:
            _kv().kv_set(IPO_LIST_KEY, {"results": results, "generated_at": datetime.now(IST).isoformat()}, ttl=4 * 3600)
        except Exception as e:
            logger.warning("could not persist partial ipo list: %s", e)
        clear_ipo_stop()
        return _set_job(
            status="stopped",
            message=f"Stopped at {len(results)}/{total} — {errors} errors before stop",
            processed=len(results),
            total=total,
            results_count=len(results),
        )

    # Sort: listing today / pre-listing first, then by score desc
    def _sort_key(r: dict):
        stage_rank = {"pre_listing": 0, "listing_day": 1, "upcoming": 2, "listed": 3}.get(r.get("stage"), 4)
        return (stage_rank, -(r.get("ipo_score") or r.get("pre_listing_advisory_score") or 0))

    results.sort(key=_sort_key)

    try:
        _kv().kv_set(IPO_LIST_KEY, {"results": results, "generated_at": datetime.now(IST).isoformat()}, ttl=4 * 3600)
    except Exception as e:
        logger.warning("could not persist ipo list: %s", e)

    # Durable copy — Neon on Render, Oracle on the Oracle deployment (see
    # ipo_schema.py). This is what lets the next scan within
    # IPO_DB_FRESH_HOURS skip re-hitting NSE entirely, and gives Database
    # Feed Health-style visibility into exactly what the last scan saw.
    try:
        n = _ipo_db_upsert(results)
        logger.info("ipo_static_feed: upserted %s/%s rows", n, len(results))
    except Exception as e:
        logger.warning("ipo db persist failed (non-fatal): %s", e)

    return _set_job(status="done", message=f"Done: {len(results)} analyzed, {errors} errors",
                      processed=total, total=total, results_count=len(results))


def get_ipo_list(display_days: Optional[int] = None) -> dict:
    """
    display_days filters the already-scanned list down to listings within
    the last N days without re-scanning (default IPO_CHECKER_DEFAULT_DISPLAY_DAYS
    = ~1 month). Pass display_days=0 or a large number (e.g. 365) to see the
    full up-to-a-year scan window the backend actually discovered. Pre-listing/
    listing-day/upcoming entries always pass through regardless of display_days
    since "days since listing" doesn't apply to them yet.
    """
    try:
        cached = _kv().kv_get(IPO_LIST_KEY)
    except Exception:
        cached = None
    if not isinstance(cached, dict):
        return {"results": [], "generated_at": None, "display_days": display_days}

    results = cached.get("results") or []
    window = IPO_CHECKER_DEFAULT_DISPLAY_DAYS if display_days is None else int(display_days)
    if not window or window <= 0 or window >= IPO_LOOKBACK_DAYS_HARD_CAP:
        return {**cached, "display_days": window}

    now = datetime.now(IST)
    filtered = []
    for r in results:
        if r.get("stage") in ("pre_listing", "listing_day", "upcoming"):
            filtered.append(r)
            continue
        dt = _parse_date(r.get("listing_date"))
        if dt is None:
            filtered.append(r)  # can't tell age — don't hide it
            continue
        dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt
        if (now - dt).days <= window:
            filtered.append(r)

    return {**cached, "results": filtered, "display_days": window, "total_scanned": len(results)}
