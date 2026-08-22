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
LOOKBACK_DAYS_MAX = int(os.getenv("IPO_LOOKBACK_DAYS_MAX", "45"))

MARKET_DATA_URL = os.getenv("MARKET_DATA_URL", "https://market-data-service-r6d7.onrender.com").rstrip("/")

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
        return {
            "symbol": symbol,
            "company_name": row.get("name") or symbol,
            "issue_price": issue_price,
            "listing_date": row.get("listingDate"),
            "status": status,
            "source": "ipoalerts",
            "subscription_times": row.get("subscriptionTimes") or (row.get("subscription") or {}).get("total"),
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
    try:
        with _nse_session() as c:
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

    return out


def _normalize_nse_row(row: Dict[str, Any], kind: str) -> Optional[Dict[str, Any]]:
    try:
        symbol = str(
            row.get("symbol") or row.get("scriptCode") or row.get("companyName") or ""
        ).upper().strip()
        if not symbol:
            return None
        issue_price = row.get("issuePrice") or row.get("cutOffPrice") or row.get("finalIssuePrice")
        if isinstance(issue_price, str):
            issue_price = issue_price.replace(",", "").split("-")[-1].strip()
        try:
            issue_price = float(issue_price)
        except (TypeError, ValueError):
            issue_price = None
        listing_date = row.get("listingDate") or row.get("dateOfListing")
        return {
            "symbol": symbol,
            "company_name": row.get("companyName") or symbol,
            "issue_price": issue_price,
            "listing_date": listing_date,
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
    """
    ipoalerts = fetch_ipoalerts_calendar()
    nse = fetch_nse_ipo_calendar()
    manual = _manual_ipos()

    by_symbol: Dict[str, Dict[str, Any]] = {}
    for a in nse:
        by_symbol[a["symbol"]] = a
    for a in ipoalerts:
        by_symbol[a["symbol"]] = a  # ipoalerts overrides NSE guess
    for m in manual:
        by_symbol[m["symbol"]] = m  # manual overrides everything

    merged = list(by_symbol.values())
    # Drop anything without a listing date/issue price — can't score it
    return [m for m in merged if m.get("listing_date") and m.get("issue_price")]


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
    """
    try:
        # /history only accepts named periods (1mo/3mo/6mo/1y/2y/5y); map the
        # day-count IPO analysis asks for onto the closest one so the
        # earlier-days cap in get_history doesn't silently override us.
        if days <= 30:
            period = "1mo"
        elif days <= 90:
            period = "3mo"
        elif days <= 180:
            period = "6mo"
        else:
            period = "1y"
        r = httpx.get(
            f"{MARKET_DATA_URL}/history/{symbol}",
            params={"period": period, "interval": "1d"},
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
        t = yf.Ticker(f"{symbol}.NS")
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
            # Early advisory from subscription strength alone, if we have it
            sub = entry.get("subscription_times")
            if sub is not None:
                sub_score = _clamp(50 + min(sub, 50) * 1.0)  # 1x=50 (neutral), 50x+=~100
                result["pre_listing_advisory_score"] = round(sub_score, 1)
                result["pre_listing_advisory"] = (
                    "Strong subscription — historically correlates with a firm listing pop."
                    if sub_score >= 66 else
                    "Moderate/weak subscription — listing pop uncertain, watch first 15 minutes of trade."
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
    hist = _fetch_history(symbol, min(days_since_listing + 5, LOOKBACK_DAYS_MAX))
    if hist is None or hist.empty:
        result["stage"] = "error"
        result["message"] = "Could not fetch post-listing price history"
        return result

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

def run_ipo_scan(force: bool = False) -> dict:
    universe = _merged_ipo_universe()
    total = len(universe)
    _set_job(status="running", message=f"Scanning {total} IPO(s)…", processed=0, total=total,
              started_at=datetime.now(IST).isoformat())

    results: List[Dict[str, Any]] = []
    errors = 0
    for i, entry in enumerate(universe):
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

    # Sort: listing today / pre-listing first, then by score desc
    def _sort_key(r: dict):
        stage_rank = {"pre_listing": 0, "listing_day": 1, "upcoming": 2, "listed": 3}.get(r.get("stage"), 4)
        return (stage_rank, -(r.get("ipo_score") or r.get("pre_listing_advisory_score") or 0))

    results.sort(key=_sort_key)

    try:
        _kv().kv_set(IPO_LIST_KEY, {"results": results, "generated_at": datetime.now(IST).isoformat()}, ttl=4 * 3600)
    except Exception as e:
        logger.warning("could not persist ipo list: %s", e)

    return _set_job(status="done", message=f"Done: {len(results)} analyzed, {errors} errors",
                      processed=total, total=total, results_count=len(results))


def get_ipo_list() -> dict:
    try:
        cached = _kv().kv_get(IPO_LIST_KEY)
        if isinstance(cached, dict):
            return cached
    except Exception:
        pass
    return {"results": [], "generated_at": None}
