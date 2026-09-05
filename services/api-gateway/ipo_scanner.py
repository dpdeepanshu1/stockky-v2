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

BUY_NOW_BAR = float(os.getenv("IPO_BUY_NOW_BAR", "70"))  # raised 66→70: weak market, higher bar
PREPARE_BAR = float(os.getenv("IPO_PREPARE_BAR", "58"))  # raised 54→58: choppy market, fewer marginal signals
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

# Headers for the two HTML *navigation* requests that bootstrap the cookie jar.
#
# This is the fix for the `GET https://www.nseindia.com "HTTP/1.1 403 Forbidden"`
# line in the logs. The bootstrap was reusing NSE_HEADERS, which declares
# `Accept: application/json` — but www.nseindia.com is an HTML document, and
# NSE's WAF rejects a browser-shaped request that claims to accept only JSON.
# The 403 was NOT harmless: it meant the homepage never issued the real
# nsit/nseappid session cookies, so the subsequent /api/ calls went out with a
# weak anonymous cookie set. Those still return 200 for a while, but NSE
# rate-limits and blocks them far more aggressively — a plausible contributor to
# the intermittent "not found"/empty-payload behaviour. A real browser sends an
# HTML Accept plus Sec-Fetch-* navigation hints, so we do the same here and keep
# the JSON Accept exclusively for the /api/ calls.
NSE_BOOTSTRAP_HEADERS = {
    "User-Agent": NSE_HEADERS["User-Agent"],
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
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

# ── ipoalerts quota management ──────────────────────────────────────────────
# The free ipoalerts.in tier is capped at 25 requests/day. We budget well
# under that (default 20, leaving headroom for manual testing) and spend it
# on the smallest useful slice: only "open" (currently subscribing — always
# genuinely current) and "listed" (filtered down to the last
# IPOALERTS_RECENT_WINDOW_DAYS days below) — never "upcoming"/"closed",
# which NSE's own free unofficial API already covers for the wider window.
# A successful fetch is cached for IPOALERTS_CACHE_HOURS so repeated Scan
# IPOs clicks (or the daily premarket job on a day nothing changed) reuse it
# instead of re-spending quota; only a cache MISS counts against the quota.
IPOALERTS_DAILY_LIMIT = int(os.getenv("IPOALERTS_DAILY_LIMIT", "20") or 20)
IPOALERTS_RECENT_WINDOW_DAYS = int(os.getenv("IPOALERTS_RECENT_WINDOW_DAYS", "7") or 7)
IPOALERTS_CACHE_HOURS = float(os.getenv("IPOALERTS_CACHE_HOURS", "6") or 6)
IPOALERTS_STATUSES = ("open", "listed")  # NOT "upcoming"/"closed" — see above
IPOALERTS_CACHE_KEY = "stockky:ipoalerts:cache"


def _ipoalerts_quota_key() -> str:
    return f"stockky:ipoalerts:quota:{datetime.now(IST).strftime('%Y-%m-%d')}"


def _ipoalerts_quota_used() -> int:
    try:
        n = _kv().kv_get(_ipoalerts_quota_key())
        return int(n) if n else 0
    except Exception:
        return 0


def _ipoalerts_quota_spend(n: int) -> None:
    try:
        used = _ipoalerts_quota_used() + n
        # TTL a little over 24h so a slow day doesn't get stuck with a
        # stale non-zero counter if kv_cache's clock drifts slightly.
        _kv().kv_set(_ipoalerts_quota_key(), used, ttl=90000)
    except Exception:
        pass

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


def _iso_listing_date(d: Any) -> Optional[str]:
    """Defense-in-depth normalization to ISO (YYYY-MM-DD), applied right at
    the DB-write choke point regardless of which source the row came from
    (NSE auto-scan already normalizes in _normalize_nse_row; this covers
    ipoalerts rows and hand-typed add_manual_ipo() callers too, so the DB
    writer never sees a non-ISO date no matter the source)."""
    if not d:
        return None
    s = str(d).strip()
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]  # already ISO — fast path, no reparse needed
    dt = _parse_date(s)
    return dt.strftime("%Y-%m-%d") if dt is not None else None


def _ipo_db_upsert(rows: List[Dict[str, Any]]) -> int:
    """Persist raw/scored IPO rows to ipo_static_feed (Neon on Render, Oracle
    on the Oracle deployment — same ensure_ipo_schema()/dialect() selection
    as surprise_static_feed). Best-effort: a DB write failure here must never
    break the scan — kv_cache remains the fast-path source the endpoints
    actually serve from; this table is the durable "what did we last see"
    record for debugging + the 24h freshness check.

    Fix (ipo tracker "always shows 0 in DB" bug): two things were wrong
    together here.
      1. `listing_date` could reach this function in NSE's raw, non-ISO
         format (e.g. "06-AUG-2026"). The Oracle upsert's
         TO_DATE(SUBSTR(:listing_date,1,10),'YYYY-MM-DD') requires ISO and
         raised ORA-01858 on anything else (see _iso_listing_date() above —
         also applied at the source in _normalize_nse_row, this is the
         defensive second layer for ipoalerts/manual-entry rows).
      2. The Oracle branch executed every row inside ONE shared
         `eng.begin()` transaction, so a single bad row's exception
         propagated out of the loop and rolled back the ENTIRE batch —
         exactly matching the production log "ipo_static_feed: upserted
         0/278 rows" and the DB Feed Health panel's permanent 0/0%. Each
         row now gets its own transaction so one bad row can never zero
         out the rest of a scan again.
    """
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
            row["listing_date"] = _iso_listing_date(row.get("listing_date")) or ""
            bs = r.get("buy_suggestion")
            row["buy_suggestion_json"] = _json.dumps(bs) if bs else None
            payload.append(row)
        payload = ipo_schema.adapt_rows(payload, dial)
        stmt = text(ipo_schema.upsert_sql(dial))
        n = 0
        if dial == "oracle":
            # One transaction PER ROW — a bad row must never roll back rows
            # that already succeeded (see docstring fix #2 above).
            for row in payload:
                try:
                    with eng.begin() as conn:
                        conn.execute(stmt, row)
                    n += 1
                except Exception as e:
                    logger.warning(
                        "ipo db upsert: row for %s failed (skipped, other "
                        "rows unaffected): %s",
                        row.get("symbol"), str(e)[:200],
                    )
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

# The KV cache backing IPO_LIST_KEY used to live for a flat 4h regardless of
# IPO_DB_FRESH_HOURS (default 24h). That mismatch was the actual cause of
# "Scan IPOs" silently hitting NSE/ipoalerts far more than intended: once
# the 4h KV entry expired but the durable ipo_static_feed table was still
# within its 24h freshness window, _run_ipo_scan_locked's "is it fresh?"
# check would say yes (age_h < IPO_DB_FRESH_HOURS) but then find no cached
# results to actually return, fall through, and run a real upstream scan
# anyway — for up to 20 of every 24 hours. The cache now always outlives
# the freshness window it's guarding.
IPO_LIST_CACHE_TTL_SEC = max(4 * 3600, int(IPO_DB_FRESH_HOURS * 3600) + 3600)


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

def fetch_ipoalerts_calendar(_bypass_cache: bool = False) -> List[Dict[str, Any]]:
    """
    Discovery source for VERY RECENT IPOs when IPOALERTS_API_KEY is set —
    confirmed live schema: {symbol, name, type, listingDate, priceRange,
    startDate, endDate, issueSize, schedule[...]}. priceRange is a band
    like "95-99"; we take the upper bound as issue_price, matching NSE
    convention (retail investors pay the cap price on allotment).

    Quota-aware by design (free tier = 25 req/day, see IPOALERTS_DAILY_LIMIT
    above): only queries "open" and "listed" (2 requests, not 4), caches the
    combined result for IPOALERTS_CACHE_HOURS, and — since this account is
    meant for VERY RECENT listings only — drops any "listed" row older than
    IPOALERTS_RECENT_WINDOW_DAYS (default 7). "open" rows are inherently
    current (a currently-subscribing issue) so they pass through unfiltered.
    Anything older than that window is NSE's unofficial-API's job instead
    (free, unlimited, already covers the full IPO_LOOKBACK_DAYS_MAX window).
    """
    if not IPOALERTS_API_KEY:
        return []

    if not _bypass_cache:
        try:
            cached = _kv().kv_get(IPOALERTS_CACHE_KEY)
            if isinstance(cached, list):
                return cached
        except Exception:
            pass

    used = _ipoalerts_quota_used()
    if used + len(IPOALERTS_STATUSES) > IPOALERTS_DAILY_LIMIT:
        logger.warning(
            "ipoalerts: daily quota reached (%s/%s used) — skipping fetch, "
            "reusing whatever's cached (or NSE-only for this scan)",
            used, IPOALERTS_DAILY_LIMIT,
        )
        try:
            cached = _kv().kv_get(IPOALERTS_CACHE_KEY)
            return cached if isinstance(cached, list) else []
        except Exception:
            return []

    try:
        from rate_limiter import acquire as rl_acquire
        rl_acquire("indianapi", weight=1)  # shares the conservative IndianAPI-style bucket
    except Exception:
        pass

    out: List[Dict[str, Any]] = []
    headers = {"X-API-KEY": IPOALERTS_API_KEY, "Accept": "application/json"}
    spent = 0
    for status in IPOALERTS_STATUSES:
        try:
            r = httpx.get(IPOALERTS_BASE, params={"status": status}, headers=headers, timeout=15)
            spent += 1
            if r.status_code != 200:
                # Log the response body too, not just the code — a 400 here
                # usually means this account's plan doesn't accept this
                # status value (ipoalerts' valid enum isn't publicly
                # documented and can vary by plan), and the body normally
                # says exactly which. Confirmed real case: "listed" -> 400
                # on at least one account while "open" -> 200 on the same
                # key, so IPOALERTS_STATUSES may need trimming per-account;
                # this makes that visible in logs instead of guessing.
                logger.info(
                    "ipoalerts status=%s -> HTTP %s: %s",
                    status, r.status_code, r.text[:300],
                )
                continue
            data = r.json()
            for row in data.get("ipos") or []:
                norm = _normalize_ipoalerts_row(row, status)
                if norm:
                    out.append(norm)
        except Exception as e:
            spent += 1
            logger.info("ipoalerts status=%s fetch failed: %s", status, e)
    _ipoalerts_quota_spend(spent)

    # Recency filter — "listed" only for the last IPOALERTS_RECENT_WINDOW_DAYS
    # days (today/yesterday through ~1 week back); "open" passes through as-is.
    now = datetime.now(IST)
    filtered: List[Dict[str, Any]] = []
    for r in out:
        if r.get("status") != "listed":
            filtered.append(r)
            continue
        dt = _parse_date(r.get("listing_date"))
        if dt is None:
            continue
        dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt
        if 0 <= (now - dt).days <= IPOALERTS_RECENT_WINDOW_DAYS:
            filtered.append(r)

    # De-dupe by symbol, keep first occurrence
    seen = set()
    deduped = []
    for r in filtered:
        if r["symbol"] in seen:
            continue
        seen.add(r["symbol"])
        deduped.append(r)

    try:
        _kv().kv_set(IPOALERTS_CACHE_KEY, deduped, ttl=int(IPOALERTS_CACHE_HOURS * 3600))
    except Exception:
        pass
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


# NSE's cookie handshake is 2 network round trips (bootstrap root + referer
# page) before a single payload GET even happens. fetch_nse_ipo_calendar()
# used to call _nse_session() fresh on every invocation — so a single Scan
# IPOs click, which calls it once, still cost 2 bootstrap hops + 2 payload
# hops = 4 requests to nseindia.com; but the log shows Scan IPOs (and the
# 2s status-poll loop re-checking it) firing in rapid succession, each
# minting a brand-new session and re-doing the 403-then-200 bootstrap dance
# from scratch — needless request volume against a host that's already
# rate-limiting/blocking the anonymous session it hands back (see the
# 'cookies present but weak' warning below). NSE's own session cookies are
# typically valid for several minutes; reusing one cached client for a
# short window cuts the bootstrap-hop count roughly in half across a burst
# of calls without changing behavior when the cache is empty/expired.
_NSE_SESSION_CACHE: Dict[str, Any] = {"client": None, "ts": 0.0}
_NSE_SESSION_TTL_SECONDS = 300  # 5 minutes — comfortably inside NSE's own cookie lifetime
_NSE_SESSION_LOCK = threading.Lock()


def _nse_session(force_new: bool = False) -> httpx.Client:
    """Return a client that has completed NSE's cookie handshake, reusing a
    recently-bootstrapped one when available instead of re-doing the 2-hop
    dance on every call (see module note above). Root cause of the '403
    Forbidden' seen on the bootstrap GET itself: the client was constructed
    with headers=NSE_HEADERS, which sets Accept: application/json, and then
    used to request https://www.nseindia.com — an HTML document. NSE sits
    behind an Akamai WAF that treats "JSON Accept on a document URL, no
    Sec-Fetch-* navigation hints" as a bot signature and answers 403. A 403
    body still carries a Set-Cookie, so nothing raised and nothing looked
    broken — but the cookies handed back are the weak anonymous pair, not
    the real nsit/nseappid pair a browser gets. Sending browser-shaped
    navigation headers for the two HTML hops (and keeping the JSON Accept
    for the /api/ calls afterwards) is the actual fix, not a cosmetic one.
    """
    with _NSE_SESSION_LOCK:
        cached = _NSE_SESSION_CACHE.get("client")
        age = time.time() - _NSE_SESSION_CACHE.get("ts", 0.0)
        if not force_new and cached is not None and age < _NSE_SESSION_TTL_SECONDS:
            return cached

        c = httpx.Client(timeout=20, headers=NSE_HEADERS, follow_redirects=True)
        try:
            # Per-request headers override the client defaults for these two hops only.
            r1 = c.get("https://www.nseindia.com", headers=NSE_BOOTSTRAP_HEADERS)
            r2 = c.get(
                "https://www.nseindia.com/market-data/all-upcoming-issues-ipo",
                headers=NSE_BOOTSTRAP_HEADERS,
            )
            names = {k for k in c.cookies.keys()}
            if not names:
                logger.warning(
                    "nse bootstrap got no cookies (status %s/%s) — /api calls will run "
                    "on an anonymous session and may return empty payloads",
                    r1.status_code, r2.status_code,
                )
            elif not (names & {"nsit", "nseappid"}):
                logger.info(
                    "nse bootstrap cookies present but weak (%s; status %s/%s) — "
                    "payloads may be rate-limited",
                    ",".join(sorted(names))[:120], r1.status_code, r2.status_code,
                )
            else:
                logger.debug("nse bootstrap ok (%s)", ",".join(sorted(names))[:120])
        except Exception as e:
            logger.debug("nse session bootstrap: %s", e)

        old = _NSE_SESSION_CACHE.get("client")
        if old is not None and old is not c:
            try:
                old.close()
            except Exception:
                pass
        _NSE_SESSION_CACHE["client"] = c
        _NSE_SESSION_CACHE["ts"] = time.time()
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
    # _nse_session() now returns a short-lived CACHED client shared across
    # calls (see its docstring) so back-to-back Scan IPOs invocations don't
    # each pay the 2-hop bootstrap cost. That means this function must NOT
    # close it — closing a shared client here would force every subsequent
    # caller straight back into a fresh bootstrap, defeating the cache
    # entirely (and closing it while genuinely concurrent callers still
    # hold a reference would break their in-flight requests, which is the
    # exact failure mode the removed try/finally close was written to
    # avoid in the first place — the fix is to simply never close a client
    # this function didn't create for itself alone).
    c = _nse_session()
    try:
        for url, kind in candidates:
            try:
                r = c.get(url, timeout=15)
                if r.status_code != 200:
                    logger.info("NSE IPO calendar %s -> HTTP %s (blocked/unavailable)", kind, r.status_code)
                    if r.status_code in (401, 403):
                        # The cached session itself has gone bad (NSE started
                        # blocking it mid-TTL) — don't keep handing it out to
                        # the next N callers for the rest of the 5-minute
                        # window. Force a fresh bootstrap next call.
                        with _NSE_SESSION_LOCK:
                            if _NSE_SESSION_CACHE.get("client") is c:
                                _NSE_SESSION_CACHE["ts"] = 0.0
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


def _nse_placeholder_none(v: Any) -> Any:
    """
    NSE's IPO JSON uses the literal string "-" (and occasionally "--" or
    "N/A") as a placeholder for "not available yet" on issuePrice and
    listingDate — e.g. a live public-past-issues row for an IPO whose issue
    just closed: {"issuePrice": "-", "listingDate": "-", "priceRange":
    "Rs.342 to Rs.360", ...}. That placeholder is a non-empty STRING, so
    Python's `or` chain (`row.get("issuePrice") or row.get("priceRange")`)
    treats it as truthy and never falls through to priceRange — the row
    silently ends up with issue_price=None / listing_date="-" (unparseable)
    and gets dropped by _merged_ipo_universe's "must have listing_date and
    issue_price" filter, even though good data (priceRange) was sitting
    right there in the same row. This normalizes the placeholder to a real
    None so the `or` chain behaves as intended.
    """
    if v is None:
        return None
    s = str(v).strip()
    if not s or s in ("-", "--", "N/A", "NA", "null", "None"):
        return None
    return v


def _normalize_nse_row(row: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    try:
        symbol = str(
            row.get("symbol") or row.get("scriptCode") or row.get("companyName") or row.get("company") or ""
        ).upper().strip()
        if not symbol:
            return None
        # Reject non-equity public issues (NCDs/bonds, partly-paid rights
        # issues) that NSE's public-past-issues endpoint returns alongside
        # genuine equity IPOs with no instrument-type field to tell them
        # apart. Real NSE equity tickers never start with a digit; NCD/bond
        # series tickers do — they're coded as <coupon><issuer><maturity-year>
        # (e.g. "1150VIES30" = 11.50% coupon, VIES issuer, matures 2030;
        # "925ECL28" = 9.25% coupon, ECL, matures 2028). Without this check
        # these fed straight into the IPO tracker's price-fetch pipeline as
        # if they were newly-listed stocks, where yfinance/NSE quote lookups
        # can only ever fail for them (they're debt instruments, not
        # equities, so they were never going to have an equity quote) —
        # pure wasted "possibly delisted" noise on every prefeed cycle,
        # never actually delisted because they were never equities.
        if symbol[0].isdigit():
            return None
        issue_price = _extract_price_band_upper(
            _nse_placeholder_none(row.get("issuePrice"))
            or _nse_placeholder_none(row.get("priceRange"))
            or _nse_placeholder_none(row.get("cutOffPrice"))
            or _nse_placeholder_none(row.get("finalIssuePrice"))
        )
        listing_date = _nse_placeholder_none(row.get("listingDate")) or _nse_placeholder_none(row.get("dateOfListing"))
        issue_end = _nse_placeholder_none(row.get("issueEndDate")) or _nse_placeholder_none(row.get("ipoEndDate"))
        # ── Fix: normalize to ISO (YYYY-MM-DD) here, at the source ─────────────
        # NSE returns listingDate in its own raw format (commonly "06-Aug-2026"
        # / "06-AUG-2026", sometimes "06-08-2026"), NOT ISO. That raw string
        # used to flow straight through to the DB layer, where the Oracle
        # writer's TO_DATE(SUBSTR(:listing_date,1,10),'YYYY-MM-DD') strictly
        # expects YYYY-MM-DD and raises ORA-01858 on anything else — and
        # because _ipo_db_upsert() writes the whole batch in one Oracle
        # transaction, a single bad date used to zero out the ENTIRE scan
        # (confirmed in production logs: "ipo_static_feed: upserted 0/278
        # rows"), which is exactly why the IPO Tracker's DB Feed Health panel
        # always showed 0 tracked / 0% health. _parse_date() already handles
        # every format NSE uses, so normalize once, right here, and every
        # downstream consumer (DB writer on both dialects, the UI, the
        # freshness/estimate logic below) can assume ISO from this point on.
        if listing_date:
            _dt = _parse_date(listing_date)
            listing_date = _dt.strftime("%Y-%m-%d") if _dt is not None else None
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
    """Register an IPO by hand with every field known already. Kept for any
    caller that already has the full record; the '+ Add IPO' UI itself now
    only asks for a company name and resolves the rest — see
    add_manual_ipo_by_name below."""
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


def _name_match_score(query: str, candidate: str) -> float:
    """Fuzzy-ish match for 'the user typed a company name by hand' against
    a candidate's company_name/symbol — deliberately simple (no external
    fuzzy-match dependency): exact match, substring either direction, then
    token overlap. Good enough to tell 'Tempsens Instruments (India)
    Limited' apart from 'Tempsens' vs a totally unrelated name; not meant
    to be bulletproof against typos beyond that."""
    q = re.sub(r"[^a-z0-9]+", " ", (query or "").lower()).strip()
    c = re.sub(r"[^a-z0-9]+", " ", (candidate or "").lower()).strip()
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    if q in c or c in q:
        return 0.9
    q_tokens = set(q.split())
    c_tokens = set(c.split())
    if not q_tokens or not c_tokens:
        return 0.0
    overlap = len(q_tokens & c_tokens)
    return (overlap / max(len(q_tokens), 1)) * 0.8


def resolve_ipo_by_name(company_name: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Find the best-matching IPO for a hand-typed company name.

    Checks NSE's calendar first (free, unlimited, already fetched for every
    scan) before ever touching ipoalerts — a hand-typed lookup is rare
    enough not to matter for the daily quota, but there's no reason to
    spend it when NSE already has the answer. Returns (best_match,
    near_misses) — near_misses (score >= 0.35 but below the accept
    threshold) let the caller say 'did you mean X?' instead of a bare
    'not found' when the name almost-but-not-quite matched.
    """
    name = (company_name or "").strip()
    if not name:
        return None, []

    candidates = list(fetch_nse_ipo_calendar())
    scored = [
        (c, max(
            _name_match_score(name, c.get("company_name") or ""),
            _name_match_score(name, c.get("symbol") or ""),
        ))
        for c in candidates
    ]

    best, best_score = None, 0.0
    for c, score in scored:
        if score > best_score:
            best, best_score = c, score

    if best_score < 0.7 and IPOALERTS_API_KEY:
        # NSE had nothing confident — spend one ipoalerts round-trip as a
        # last resort (still quota-gated inside fetch_ipoalerts_calendar).
        ia_candidates = list(fetch_ipoalerts_calendar())
        for c in ia_candidates:
            score = max(
                _name_match_score(name, c.get("company_name") or ""),
                _name_match_score(name, c.get("symbol") or ""),
            )
            scored.append((c, score))
            if score > best_score:
                best, best_score = c, score

    if best is not None and best_score >= 0.7:
        return best, []

    near_misses = sorted(
        (c for c, score in scored if 0.35 <= score < 0.7),
        key=lambda c: -max(
            _name_match_score(name, c.get("company_name") or ""),
            _name_match_score(name, c.get("symbol") or ""),
        ),
    )[:5]
    return None, near_misses


def add_manual_ipo_by_name(company_name: str) -> Dict[str, Any]:
    """The '+ Add IPO' entry point — name only. Resolves symbol, issue
    price, and listing date automatically against NSE's calendar (and
    ipoalerts as a fallback) and persists a manual entry exactly like
    add_manual_ipo, so the result shows up in the upcoming/recent list the
    same way and via the same code path.

    Returns {"resolved": True, "entry": {...}} on success, or
    {"resolved": False, "message": ..., "suggestions": [...]} when no
    confident match was found (the message tells the user what to try;
    suggestions are near-miss company names, if any, so they can correct
    the spelling instead of guessing blind)."""
    name = (company_name or "").strip()
    if not name:
        return {"resolved": False, "message": "Company name is required.", "suggestions": []}

    match, near_misses = resolve_ipo_by_name(name)
    if match is None:
        suggestions = [m.get("company_name") for m in near_misses if m.get("company_name")]
        msg = (
            f"No IPO found matching \"{name}\" in NSE's calendar"
            + (" or ipoalerts" if IPOALERTS_API_KEY else "")
            + ". Use the exact registered company name (e.g. \"Tempsens "
              "Instruments (India) Limited\", not \"Tempsens\")."
        )
        return {"resolved": False, "message": msg, "suggestions": suggestions}

    entry = add_manual_ipo(
        symbol=match["symbol"],
        issue_price=float(match["issue_price"]),
        listing_date=match["listing_date"],
        company_name=match.get("company_name") or match["symbol"],
        subscription_times=match.get("subscription_times"),
        gmp=match.get("gmp"),
    )
    return {"resolved": True, "entry": entry}


def _manual_ipos() -> List[Dict[str, Any]]:
    try:
        existing = _kv().kv_get(IPO_MANUAL_KEY)
        return existing if isinstance(existing, list) else []
    except Exception:
        return []


def _merged_ipo_universe() -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Merge order, most-trusted-wins on symbol collision:
      1. manual entries       — you told the system directly, always wins
      2. ipoalerts.in         — real confirmed schema, needs a free API key,
                                 VERY RECENT only (see fetch_ipoalerts_calendar)
      3. NSE unofficial API   — best-effort, unverified/frequently blocked

    Recency filter: ipoalerts' "listed"/"closed" status buckets return
    every IPO in their database with no date bound — this is why the scan
    was processing 1000+ candidates (most listed years ago) instead of the
    handful that are actually "recent". This is a short-term listing-
    momentum scanner (see module docstring), so anything outside the
    window below is dropped before analysis even starts:
      - already listed: keep only the last IPO_LOOKBACK_DAYS_MAX days
        (default 365, hard-capped at IPO_LOOKBACK_DAYS_HARD_CAP)
      - not yet listed: keep only the next IPO_UPCOMING_WINDOW_DAYS days
        (default 21) so "listing today/tomorrow" IPOs are still included
    Manual entries always pass through regardless of date — if you added
    it by hand, you meant to track it.

    Returns (candidates, diagnostics) — diagnostics records how many rows
    each source actually returned, so a 0-candidate scan can say WHY
    instead of just silently finishing with nothing (NSE blocked, no
    ipoalerts key, no manual entries — the three most common causes)
    instead of looking like the click did nothing at all.
    """
    ipoalerts = fetch_ipoalerts_calendar()
    nse = fetch_nse_ipo_calendar()
    manual = _manual_ipos()

    diag = {
        "nse_candidates": len(nse),
        "ipoalerts_candidates": len(ipoalerts),
        "ipoalerts_configured": bool(IPOALERTS_API_KEY),
        "manual_candidates": len(manual),
    }

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
    diag["merged_total"] = len(merged)
    diag["merged_dated"] = len(dated)

    lookback = min(LOOKBACK_DAYS_MAX, IPO_LOOKBACK_DAYS_HARD_CAP)
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
    diag["recent_after_window"] = len(recent)
    return recent, diag


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


def _fetch_gmp(symbol: str, company_name: str = "", issue_price: float = 0.0) -> Optional[float]:
    """Best-effort Grey Market Premium (GMP) scrape from investorgain.com — a
    freely accessible IPO data aggregator that doesn't require an API key.

    GMP = the unofficial pre-listing / post-listing premium over issue price
    that the grey market is pricing in. A positive GMP means grey-market demand
    above issue price; negative means the stock is trading below issue in the
    grey market — a breakdown signal.

    Implementation is intentionally conservative: match GMP only within a
    window of text that also contains the company name or symbol, so we never
    return another company's GMP when the page lists many rows. Fails silently
    on any error — never blocks IPO scoring.
    """
    try:
        base = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
        name_key = (company_name or base).strip()
        # Search keyword: try symbol first (exact), fall back to first two words
        # of company name (avoids "Limited"/"Ltd" noise in URL params).
        name_words = name_key.split()
        search_term = base if len(base) >= 4 else " ".join(name_words[:2])

        url = "https://www.investorgain.com/report/ipo-performance-tracker/272/"
        r = httpx.get(
            url,
            params={"company": search_term[:40]},
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            },
            timeout=8,
            follow_redirects=True,
        )
        if r.status_code != 200:
            return None

        page = r.text
        # Anchor the GMP search to a window of ~400 chars around where the
        # company name / symbol appears on the page. Without this anchoring the
        # first regex match anywhere on the page would return whatever GMP
        # happens to appear first — probably a different company.
        anchor = name_key[:20].lower()
        idx = page.lower().find(anchor)
        if idx < 0:
            # Try symbol as fallback anchor
            idx = page.lower().find(base.lower())
        if idx < 0:
            return None  # company not found on this page — don't guess

        # Extract a 600-char window centred on the match and scan for GMP
        window = page[max(0, idx - 100): idx + 500]
        gmp_match = re.search(
            r"GMP\s*[:\-]?\s*[₹]?\s*([-+]?\d+(?:\.\d+)?)",
            window, re.IGNORECASE
        )
        if not gmp_match:
            return None
        gmp_val = float(gmp_match.group(1))
        # Sanity gate: GMP above 3× issue price or a round suspicious zero
        # on a high-issue-price stock is almost certainly a parse artefact.
        if issue_price > 0:
            if abs(gmp_val) > issue_price * 3:
                return None
        return gmp_val
    except Exception as e:
        logger.debug("gmp fetch %s: %s", symbol, e)
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

    # ── Auto-fetch GMP when not provided by the discovery source ─────────
    # ipoalerts only provides GMP on paid plans; NSE never does. For pre-
    # listing and recently-listed stocks where GMP matters most, attempt a
    # best-effort scrape so the UI always has something to show.
    # Gate: only scrape for upcoming/pre-listing/listing-day and stocks within
    # 5 days of listing — for older stocks GMP is stale and the 8s timeout
    # would block every repair/scan cycle needlessly.
    _days_approx = (now.replace(tzinfo=None) - listing_dt).days if listing_dt else 999
    _gmp_relevant = _days_approx <= 5  # upcoming (negative) counts as relevant too
    if result.get("gmp") is None and _gmp_relevant:
        scraped_gmp = _fetch_gmp(symbol, company_name, issue_price)
        if scraped_gmp is not None:
            result["gmp"] = scraped_gmp
            logger.debug("gmp scraped for %s: ₹%.2f", symbol, scraped_gmp)

    # Pre-compute gmp_pct_of_issue once here so every stage (pre_listing,
    # listing_day, listed) gets it without duplicate logic.
    _gmp = result.get("gmp")
    if _gmp is not None and issue_price > 0:
        result["gmp_pct_of_issue"] = round((_gmp / issue_price) * 100, 1)
        result["gmp_implied_listing"] = round(issue_price + _gmp, 2)

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
            # Use result["gmp"] not entry.get("gmp") — result["gmp"] has already
            # been enriched by _fetch_gmp() above; entry.get("gmp") would miss
            # scraped GMP and leave the advisory blank for freshly-listed stocks
            # where the discovery source (NSE/ipoalerts free) didn't supply it.
            gmp = result.get("gmp")
            parts = []
            if sub is not None:
                sub_score = _clamp(50 + min(sub, 50) * 1.0)  # 1x=50 (neutral), 50x+=~100
                parts.append(sub_score)
            if gmp is not None and issue_price:
                # GMP as % of issue price is a rough proxy for expected listing pop —
                # same 50=neutral / saturating shape as the subscription score above.
                # gmp_pct_of_issue already set upfront (line ~1292) — just use it for score.
                gmp_pct = result.get("gmp_pct_of_issue") or ((gmp / issue_price) * 100.0)
                gmp_score = _clamp(50 + min(max(gmp_pct, -50), 100) * 0.5)
                parts.append(gmp_score)
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
        # ── Fix (Bug 2): Yahoo Finance frequently has no data for freshly-listed
        # NSE names — SME-platform stocks in particular can take days to weeks to
        # appear in Yahoo's index, and some thin-float names never do. This is NOT
        # an error in our pipeline; it's a data-availability gap at the source.
        # Calling it "error" caused two downstream problems:
        #   a) the audit counted these rows as "missing data" needing repair, making
        #      the health % look worse than reality and the missing-count misleading.
        #   b) the repair button would analyze_ipo() them again, get the same empty
        #      response, and report them as "Repaired" — a lie (Bug 1's root cause).
        # "no_data_yet" is the honest label: not broken, just not available yet.
        # The audit now separates these out so the health % reflects genuinely
        # fixable gaps, and the repair batch skips them (see ipo_repair_batch).
        result["stage"] = "no_data_yet"
        result["message"] = (
            "No price history from Yahoo Finance yet — this is common for freshly-listed "
            "NSE names (esp. SME-platform). Yahoo's index typically catches up in a few days; "
            "try Auto-Repair again then. No action needed now."
        )
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
        f"5d momentum {momentum_5d:+.1f}%. "
        f"Market context (Aug-2026): Nifty -7% in 6m, FII net-short — "
        f"bars raised to BUY_NOW≥{BUY_NOW_BAR:.0f}, PREPARE≥{PREPARE_BAR:.0f}."
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

    # Market context (Aug-2026): Nifty in correction, IPO listings need extra scrutiny.
    # Only surface IPO suggestions with score > PREPARE_BAR + 2 cushion in weak market.
    # (BUY_NOW_BAR and PREPARE_BAR are already raised; this is belt-and-suspenders.)

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

    universe, diag = _merged_ipo_universe()
    total = len(universe)

    if total == 0:
        # Silent-zero is exactly the "click Scan, nothing happens" bug
        # report — make the actual cause visible instead of just finishing
        # instantly with an empty list. The three real causes, in the
        # order they're actually hit: NSE blocked this deployment's IP
        # (very common on Render/AWS), IPOALERTS_API_KEY isn't set (or its
        # daily quota is exhausted), and there are no manual entries.
        reasons = []
        if diag["nse_candidates"] == 0:
            reasons.append("NSE returned 0 (likely IP-blocked from this deployment)")
        if not diag["ipoalerts_configured"]:
            reasons.append("IPOALERTS_API_KEY not set")
        elif diag["ipoalerts_candidates"] == 0:
            reasons.append("ipoalerts returned 0 (quota exhausted or no very-recent listings)")
        if diag["manual_candidates"] == 0:
            reasons.append("no manual entries")
        why = "; ".join(reasons) if reasons else "all sources returned candidates, but none had both a listing date and issue price"
        logger.warning("ipo scan: 0 candidates — %s (diag=%s)", why, diag)
        return _set_job(
            status="done",
            message=f"0 IPOs found — {why}. Use '+ Add IPO' to add one by name.",
            processed=0,
            total=0,
            results_count=0,
            diagnostics=diag,
        )

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
            _kv().kv_set(IPO_LIST_KEY, {"results": results, "generated_at": datetime.now(IST).isoformat()}, ttl=IPO_LIST_CACHE_TTL_SEC)
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
        _kv().kv_set(IPO_LIST_KEY, {"results": results, "generated_at": datetime.now(IST).isoformat()}, ttl=IPO_LIST_CACHE_TTL_SEC)
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


def ipo_repair_batch(limit: int = 15, symbol: Optional[str] = None) -> Dict[str, Any]:
    """Targeted repair for ipo_static_feed rows missing issue_price/ipo_score/
    decision — re-runs analyze_ipo() ONLY for the specific missing symbols
    (bounded by `limit`), not a full-universe re-scan. Mirrors
    hotpicks_repair_batch/repair_surprise_batch's "small bounded batch, not
    a blanket re-scan" shape and its /repair-batch route naming; the
    underlying fix has to be a real analyze_ipo() call rather than a
    price-only patch because an IPO's price/GMP/decision all come from ONE
    upstream analysis pass, not a separate quote endpoint the way a listed
    stock's price does (there is no standalone 'price' column on
    ipo_static_feed to patch — see ipo_schema.SELECT_COLUMNS).
    """
    out: Dict[str, Any] = {"status": "no_data", "repaired": [], "attempted": 0}
    audit = get_ipo_feed_audit()
    if not audit.get("ok"):
        out["status"] = "error"
        out["error"] = audit.get("error") or "audit failed"
        return out

    missing = audit.get("missing_ipos") or []
    force_sym = (symbol or "").upper().strip() or None
    if force_sym:
        missing = [m for m in missing if str(m.get("symbol") or "").upper() == force_sym]
        if not missing:
            out["status"] = "not_found"
            out["message"] = f"{force_sym} is not currently missing any fields."
            return out
    missing = missing[: max(1, min(int(limit or 15), 100))]
    if not missing:
        out["status"] = "completed"
        out["message"] = "Nothing missing — every tracked IPO is fully scored."
        return out
    out["attempted"] = len(missing)

    universe, _diag = _merged_ipo_universe()
    by_symbol = {u.get("symbol"): u for u in universe if u.get("symbol")}
    manual_by_symbol = {m.get("symbol"): m for m in _manual_ipos() if m.get("symbol")}

    repaired: List[str] = []
    still_no_data: List[str] = []
    results: List[Dict[str, Any]] = []
    # Bug 3 fix (2026-09-05): two paths used to drop a symbol on the floor
    # with NO record anywhere the user could see — not in `repaired`, not in
    # `still_no_data`, nothing in the response at all, just a server log line.
    # A symbol stuck in that gap (entry purged from the upstream universe, or
    # analyze_ipo() raising) looked identical to "repair worked, nothing left
    # to do" from the frontend's point of view — it silently vanished from
    # subsequent "missing" audits (see get_ipo_feed_audit note below) while
    # its card kept showing stage="error" forever, with no way for anyone to
    # tell why. `failed` now names every such symbol with a reason.
    failed: List[Dict[str, str]] = []
    for m in missing:
        sym = m.get("symbol")
        # Skip no_data_yet rows — Yahoo still doesn't have them; hammering
        # them again wastes quota and produces a misleading "Repaired" count
        # (Bug 1 fix: only add to repaired[] if we actually got a score back).
        if (m.get("stage") or "") == "no_data_yet":
            still_no_data.append(sym)
            continue
        # Prefer the live upstream universe entry (freshest listing/issue
        # data); fall back to the manual-entry record so a hand-added IPO
        # can still be repaired even after it ages out of the
        # auto-discovered NSE/ipoalerts window.
        entry = by_symbol.get(sym) or manual_by_symbol.get(sym)
        if entry is None:
            failed.append({"symbol": sym, "reason": "not_found_upstream"})
            continue
        try:
            r = analyze_ipo(entry)
            results.append(r)
            # ── Bug 1 fix: only claim "repaired" if analyze_ipo actually
            # produced a score. Previously any non-exception return was
            # counted — so a result with stage=no_data_yet (no ipo_score,
            # no decision) was reported as "Repaired" even though it wrote
            # the exact same stuck row right back to the DB. The health %
            # never moved but the button kept lying about it.
            if r.get("ipo_score") is not None and r.get("decision"):
                repaired.append(sym)
            else:
                logger.info(
                    "ipo_repair_batch: %s analyzed but still unscored (stage=%s) — not counted as repaired",
                    sym, r.get("stage"),
                )
        except Exception as e:
            logger.warning("ipo_repair_batch: analyze failed for %s: %s", sym, e)
            failed.append({"symbol": sym, "reason": str(e)[:160]})
        time.sleep(0.3)  # same cooldown discipline as every other waterfall repair in this codebase

    if results:
        try:
            n = _ipo_db_upsert(results)
            logger.info("ipo_repair_batch: upserted %s/%s row(s)", n, len(results))
        except Exception as e:
            logger.warning("ipo_repair_batch: db persist failed (non-fatal): %s", e)
        # Fold the repaired rows into the cached list too, so the tab's
        # table reflects the repair immediately instead of waiting for the
        # next scheduled/manual scan to overwrite it.
        try:
            cached = _kv().kv_get(IPO_LIST_KEY)
            cached = cached if isinstance(cached, dict) else {}
            existing = list(cached.get("results") or [])
            by_sym_repaired = {r.get("symbol"): r for r in results}
            merged = [by_sym_repaired.pop(r.get("symbol"), r) for r in existing]
            merged.extend(by_sym_repaired.values())  # any repaired symbol not already in the cached list
            cached["results"] = merged
            cached["generated_at"] = cached.get("generated_at") or datetime.now(IST).isoformat()
            _kv().kv_set(IPO_LIST_KEY, cached, ttl=IPO_LIST_CACHE_TTL_SEC)
        except Exception as e:
            logger.debug("ipo_repair_batch: cache sync skipped: %s", e)

    out["status"] = "completed"
    out["repaired"] = repaired
    out["still_no_data"] = still_no_data
    out["failed"] = failed  # [{symbol, reason}] — see Bug 3 fix note above
    # Build an honest summary: distinguish "not found upstream" from
    # "Yahoo doesn't have price data yet" — the latter is expected and
    # resolves on its own; the former means the symbol was purged from
    # NSE's registry and won't ever be fixable by re-scanning.
    actionable = len(missing) - len(still_no_data)
    if still_no_data and len(repaired) >= actionable:
        out["message"] = (
            f"Repaired {len(repaired)}/{actionable} actionable symbol(s). "
            f"{len(still_no_data)} symbol(s) still waiting on Yahoo price data "
            f"({', '.join(still_no_data[:5])}{'…' if len(still_no_data) > 5 else ''}) "
            f"— try again in a day or two."
        )
    elif len(repaired) < actionable:
        skipped = actionable - len(repaired)
        parts = [f"Repaired {len(repaired)}/{actionable} actionable symbol(s)"]
        if skipped and failed:
            names = ", ".join(f"{f['symbol']} ({f['reason']})" for f in failed[:5])
            parts.append(f"{skipped} failed — {names}{'…' if len(failed) > 5 else ''}")
        elif skipped:
            parts.append(f"{skipped} no longer found upstream")
        if still_no_data:
            parts.append(f"{len(still_no_data)} waiting on Yahoo data")
        out["message"] = " — ".join(parts) + "."
    return out


def get_ipo_feed_audit() -> dict:
    """
    IPO Tracker's OWN feed-health audit — reads ipo_static_feed (the IPO
    Tracker's dedicated table), not the general stock universe's
    stockky_kv feed. Previously the IPO Tracker tab embedded the shared
    <DataHealthAudit /> frontend component, which calls /api/feed/audit-
    missing — that endpoint audits the general per-stock data feed
    (RSI/PE/ROCE/sentiment for the ~300-symbol scan universe), which is why
    the IPO tab was showing unrelated symbols like AMBER/APOLLOHOSP instead
    of IPO rows. This function/endpoint gives the IPO tab its own,
    correctly-scoped health view.
    """
    try:
        import ipo_schema
        from sqlalchemy import text
    except Exception as e:
        return {"ok": False, "error": f"ipo_schema import failed: {e}"[:200], "rows": []}

    url = ipo_schema.database_url()
    if not url:
        return {
            "ok": True,
            "total_tracked": 0,
            "fully_scored": 0,
            "missing_count": 0,
            "missing_ipos": [],
            "message": "No IPO database configured — scan results are cache-only.",
        }

    eng = None
    try:
        eng = ipo_schema.make_engine("stockky-ipo-audit")
        if eng is None:
            return {"ok": False, "error": "engine unavailable", "rows": []}
        ipo_schema.ensure_ipo_schema()
        with eng.connect() as conn:
            result = conn.execute(text(
                "SELECT symbol, company_name, issue_price, listing_date, stage, "
                "nse_status, subscription_times, gmp, ipo_score, decision, updated_at "
                f"FROM {ipo_schema.TABLE_NAME}"
            ))
            rows = [dict(r._mapping) for r in result]
    except Exception as e:
        logger.warning("ipo audit query failed: %s", e)
        return {"ok": False, "error": str(e)[:200], "rows": []}
    finally:
        if eng is not None:
            try:
                eng.dispose()
            except Exception:
                pass

    total = len(rows)
    fully_scored = 0
    missing = []
    no_data_yet = []
    for r in rows:
        missing_fields = [
            f for f in ("issue_price", "ipo_score", "decision")
            if r.get(f) in (None, "")
        ]
        if not missing_fields:
            fully_scored += 1
            continue
        row_entry = {
            "symbol": r.get("symbol"),
            "company_name": r.get("company_name"),
            "stage": r.get("stage"),
            "missing_fields": missing_fields,
            "updated_at": str(r.get("updated_at") or ""),
        }
        # ── Bug 2 fix: no_data_yet rows (Yahoo not yet indexed) are a
        # separate category from genuinely broken rows. They're not fixable
        # by Auto-Repair today, so including them in missing_count inflates
        # the "health gap" and makes the repair button lie. Show them in
        # their own section so the user knows they exist but aren't stuck —
        # just waiting on Yahoo's crawl schedule.
        if (r.get("stage") or "") == "no_data_yet":
            no_data_yet.append(row_entry)
        else:
            missing.append(row_entry)

    # Health % counts only rows that are fixable: fully_scored out of
    # (total − no_data_yet), since no_data_yet is not "broken", just waiting.
    actionable_total = total - len(no_data_yet)
    health = round((fully_scored / max(actionable_total, 1)) * 100, 1) if actionable_total > 0 else 0.0
    return {
        "ok": True,
        "total_tracked": total,
        "fully_scored": fully_scored,
        "missing_count": len(missing),
        "missing_ipos": missing[:200],
        "no_data_yet_count": len(no_data_yet),
        "no_data_yet_ipos": no_data_yet[:50],
        "health_score": health,
        "message": (
            "No IPO rows tracked yet — run Scan IPOs first."
            if total == 0
            else (
                f"IPO feed health {health}% · {fully_scored}/{actionable_total} scored"
                + (f" · {len(no_data_yet)} waiting on Yahoo data" if no_data_yet else "")
            )
        ),
    }


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
        if r.get("stage") in ("pre_listing", "listing_day", "upcoming", "no_data_yet"):
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
