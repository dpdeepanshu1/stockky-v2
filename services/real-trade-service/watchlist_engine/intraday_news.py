"""
watchlist_engine/intraday_news.py - lightweight MARKET-HOURS news check (2026-10-04, user request)

The after-hours scan (afterhours_scan.py) sleeps through 09:00-15:45 IST, but news still breaks
during the session. This module is the small sibling that runs then. Design goals, in order:

  1. LIGHT. One HTTP GET per RSS feed every INTRADAY_NEWS_INTERVAL_SECONDS (default 15 min), run on a
     worker thread (see auto_pilot._intraday_news_loop). No quote-API calls, no api-gateway bulk-deal
     call, no per-symbol network lookups. Only headlines that are NEW since the previous tick and
     published within INTRADAY_NEWS_MAX_AGE_MINUTES are scored (an in-memory seen-set makes every
     later tick almost free). One small snapshot write per tick, only when something changed.
  2. SAFE. It never places, injects or removes a candidate and is never a gate. It stores a CAPPED
     per-symbol score nudge; entry_engine's Gate 6 ranking adds it to a candidate that is already in
     the queue (same "ranking nudge, not a gate bypass" posture as ENTRY_OVERNIGHT_PRIORITY_BONUS and
     the US-sector bonus). Positive news -> +bonus (cap INTRADAY_NEWS_BONUS_CAP); negative news
     (probe, downgrade, fraud, falls...) -> -penalty (cap INTRADAY_NEWS_PENALTY_CAP), so fresh bad
     news can push a marginal candidate under the Gate 6 floor.
  3. REUSES the after-hours scorer: same feeds, same symbol extraction against the real NSE universe,
     same keyword scoring and negative-keyword veto (with the cost-context exception).

Differences from the after-hours scan on purpose:
  - A headline with no parseable publish time is SKIPPED (after-hours degrades open; during the
    session an undated item cannot be shown to be fresh).
  - If the symbol master is unavailable the pass is skipped (the after-hours whitelist/heuristic
    fallback is too noisy to act on intraday).
  - Generic tokens that are listed tickers but are really words/indices in a headline (IT, ENERGY,
    SILVER, GOLD, BSE, ...) are ignored for the nudge.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import models
import symbol_master
from event_depth_local import classify_text
from watchlist_engine import afterhours_scan as _ah

logger = logging.getLogger("real-trade-intraday-news")

SNAPSHOT_KEY = "intraday_news:v1"

# Listed tickers that are overwhelmingly used as plain words / sector or index names in headlines.
_GENERIC_TOKENS = frozenset({
    "IT", "ENERGY", "SILVER", "GOLD", "BSE", "NSE", "DIVIDEND", "CRISIL", "INDIA", "BANK", "AUTO",
    "PHARMA", "METAL", "REALTY", "FMCG", "NIFTY", "SENSEX", "RESULTS", "STOCKS", "SHARES",
})

_SEEN_MAX = 3000
_seen: "OrderedDict[str, float]" = OrderedDict()   # headline hash -> first-seen monotonic time
_MAX_SNAPSHOT_SYMBOLS = 80

_BONUS_CACHE_TTL_S = 60.0
_bonus_cache: dict = {"at": 0.0, "data": None}


def _headline_key(headline: str) -> str:
    return hashlib.md5(" ".join(headline.lower().split()).encode("utf-8", errors="replace")).hexdigest()  # noqa: S324


def _mark_seen(key: str) -> bool:
    """True if `key` is new (and now remembered), False if already seen."""
    if key in _seen:
        _seen.move_to_end(key)
        return False
    _seen[key] = time.monotonic()
    while len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return True


def reset_state_for_tests() -> None:
    _seen.clear()
    _bonus_cache.update({"at": 0.0, "data": None})


def positive_bonus_for_score(score: float) -> float:
    """Map a 0-100 headline score to a 0..INTRADAY_NEWS_BONUS_CAP nudge (score 60+ gets the full cap)."""
    cap = max(0.0, float(config.INTRADAY_NEWS_BONUS_CAP))
    return round(max(0.0, min(cap, cap * float(score) / 60.0)), 1)


def _classify(headline: str, source_bonus: float) -> tuple[str, float, str, float]:
    """-> (kind, nudge, catalyst_type, score): kind is 'pos', 'neg' or '' (ignore)."""
    h = headline.lower()
    if _ah._has_uncontextualized_negative(h):
        return "neg", -abs(float(config.INTRADAY_NEWS_PENALTY_CAP)), "negative", 0.0
    catalyst_types = classify_text(headline) or ["news"]
    score = _ah._score_headline(headline, catalyst_types, source_bonus)
    if score < float(config.INTRADAY_NEWS_MIN_SCORE):
        return "", 0.0, "", 0.0
    return "pos", positive_bonus_for_score(score), (catalyst_types[0] if catalyst_types else "news"), round(score, 1)


def _entry_ts(entry: dict) -> Optional[datetime]:
    return _ah._parse_item_datetime(entry.get("ts"))


def _merge(old: dict, new: dict, cutoff: datetime) -> dict:
    """Merge new hits into the stored map: drop expired, newest-wins on conflicting kind,
    strongest-wins on same kind, then keep the strongest _MAX_SNAPSHOT_SYMBOLS."""
    out = {}
    for sym, e in (old or {}).items():
        ts = _entry_ts(e)
        if ts is not None and ts >= cutoff:
            out[sym] = e
    for sym, e in new.items():
        cur = out.get(sym)
        if cur is None:
            out[sym] = e
        elif cur.get("kind") != e.get("kind"):
            cts, ets = _entry_ts(cur), _entry_ts(e)
            if ets is not None and (cts is None or ets >= cts):
                out[sym] = e
        elif abs(e.get("bonus", 0.0)) > abs(cur.get("bonus", 0.0)):
            out[sym] = e
    if len(out) > _MAX_SNAPSHOT_SYMBOLS:
        keep = sorted(out.items(), key=lambda kv: abs(kv[1].get("bonus", 0.0)), reverse=True)[:_MAX_SNAPSHOT_SYMBOLS]
        out = dict(keep)
    return out


async def run_intraday_news_check(db, now: Optional[datetime] = None) -> dict:
    """One lightweight pass. Returns a small result dict (also logged). Never raises on feed errors."""
    from resilience.local_cache import load_snapshot, save_snapshot
    from tz_utils import ist_today_str
    from notifier import notify_async

    now = now or datetime.now(timezone.utc)
    result = {"ran": False, "reason": None, "fetched": 0, "new": 0, "pos": 0, "neg": 0, "symbols": 0, "alerted": 0}

    known = await symbol_master.get_all_symbols(db)
    if not known:
        result["reason"] = "symbol_master_unavailable"
        return result

    cutoff = now - timedelta(minutes=int(config.INTRADAY_NEWS_MAX_AGE_MINUTES))
    fresh: dict = {}
    for feed in _ah._RSS_FEEDS:
        items = await _ah._fetch_rss_items(feed)
        for item in items:
            result["fetched"] += 1
            headline = (item.get("title") or "").strip()
            if not headline:
                continue
            dt = _ah._parse_item_datetime(item.get("pubDate"))
            if dt is None or dt < cutoff:
                continue
            if not _mark_seen(_headline_key(headline)):
                continue
            result["new"] += 1
            symbol = _ah._extract_symbol(headline, known)
            if not symbol or symbol in _GENERIC_TOKENS:
                continue
            kind, nudge, ctype, score = _classify(headline, float(feed["source_bonus"]))
            if not kind:
                continue
            entry = {
                "kind": kind, "bonus": nudge, "catalyst": ctype,
                "headline": headline[:200], "source": feed["source"],
                "ts": dt.astimezone(timezone.utc).isoformat(), "score": score,
            }
            cur = fresh.get(symbol)
            if cur is None or abs(entry["bonus"]) > abs(cur["bonus"]):
                fresh[symbol] = entry
            result["pos" if kind == "pos" else "neg"] += 1

    result["ran"] = True
    if not fresh:
        return result

    today = ist_today_str()
    snap = load_snapshot(db, SNAPSHOT_KEY) or {}
    old = snap.get("symbols") if snap.get("date") == today else {}
    merged = _merge(old or {}, fresh, cutoff)
    save_snapshot(db, SNAPSHOT_KEY, {"date": today, "updated": now.isoformat(), "symbols": merged})
    _bonus_cache.update({"at": 0.0, "data": None})   # next Gate 6 read sees it immediately
    result["symbols"] = len(merged)

    # Compact alert: hits on queued candidates always; other symbols only when strongly positive.
    try:
        queued = {r[0] for r in db.query(models.TradeCandidate.symbol).filter_by(consumed=False).all()}
    except Exception:
        queued = set()
    lines = []
    shown = 0
    for sym, e in sorted(fresh.items(), key=lambda kv: abs(kv[1]["bonus"]), reverse=True):
        in_queue = sym in queued
        strong_pos = e["kind"] == "pos" and e.get("score", 0.0) >= float(config.INTRADAY_NEWS_ALERT_MIN_SCORE)
        if not (in_queue or strong_pos):
            continue
        icon = "🟢" if e["kind"] == "pos" else "🔴"
        tag = " (in queue)" if in_queue else ""
        sign = "+" if e["bonus"] >= 0 else ""
        lines.append(f"{icon} *{sym}*{tag} · {sign}{e['bonus']:.1f} · {e['source']}")
        lines.append(f"   _{e['headline'][:100]}_")
        shown += 1
        if shown >= 6:
            break
    if lines:
        try:
            await notify_async("📰 *Market-hours news*\n" + "\n".join(lines))
            result["alerted"] = shown
        except Exception:
            logger.debug("intraday-news: alert failed (non-fatal)", exc_info=True)
    logger.info("intraday-news: fetched=%d new=%d pos=%d neg=%d tracked=%d alerted=%d",
                result["fetched"], result["new"], result["pos"], result["neg"], result["symbols"], result["alerted"])
    return result


# ── Gate 6 reader (called from entry_engine, once per approved candidate) ───────────────────────

def _load_cached(db) -> Optional[dict]:
    now = time.monotonic()
    if now - _bonus_cache["at"] < _BONUS_CACHE_TTL_S and _bonus_cache["at"] > 0.0:
        return _bonus_cache["data"]
    from resilience.local_cache import load_snapshot
    data = load_snapshot(db, SNAPSHOT_KEY)
    _bonus_cache.update({"at": now, "data": data})
    return data


def intraday_news_bonus(db, symbol: str, now: Optional[datetime] = None) -> float:
    """Signed ranking nudge for `symbol` from fresh market-hours news; 0.0 when none/expired/unreadable.
    Always within [-INTRADAY_NEWS_PENALTY_CAP, +INTRADAY_NEWS_BONUS_CAP]. Never raises."""
    try:
        from tz_utils import ist_today_str
        data = _load_cached(db)
        if not data or data.get("date") != ist_today_str():
            return 0.0
        e = (data.get("symbols") or {}).get(symbol)
        if not e:
            return 0.0
        ts = _entry_ts(e)
        now = now or datetime.now(timezone.utc)
        if ts is None or (now - ts) > timedelta(minutes=int(config.INTRADAY_NEWS_MAX_AGE_MINUTES)):
            return 0.0
        v = float(e.get("bonus", 0.0))
        return max(-abs(float(config.INTRADAY_NEWS_PENALTY_CAP)), min(abs(float(config.INTRADAY_NEWS_BONUS_CAP)), v))
    except Exception:
        return 0.0
