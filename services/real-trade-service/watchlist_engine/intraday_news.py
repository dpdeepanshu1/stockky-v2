"""
watchlist_engine/intraday_news.py - lightweight MARKET-HOURS news check (2026-10-04, user request)

E272 round 7 (31 Oct 2026): this module is now a READER over the typed event feed (analysis-intelligence-service GET {EVENT_URL}/events/active, the same feed
Position Stocks and the after-hours scan read) and the ranking nudge is gone for typed events:
  * every minute 09:00-09:20 and every 3 minutes 09:20-15:30 (auto_pilot cadence, follows AFTERHOURS_CADENCE_V2);
  * VETO: a fresh typed NEGATIVE event is stored with its announce-time strength; entry Gate 5.9 (`event_veto_reason`) refuses a long on that symbol. A veto
    outlives the 3 h news window (INTRADAY_EVENT_VETO_MAX_AGE_MINUTES) and also reads today's veto rows in the next-day watchlist (after-hours negative news);
  * TRIGGER: from 09:20 a fresh (<= INTRADAY_EVENT_TRIGGER_MAX_AGE_MINUTES), strong (>= INTRADAY_EVENT_TRIGGER_MIN_STRENGTH) POSITIVE event of a trigger type
    queues a TradeCandidate (source_tab intraday_event), at most INTRADAY_EVENT_TRIGGER_MAX_PER_DAY per mode per day. It then meets every normal gate and the risk engine;
  * 09:05 PRE-OPEN REBUILD: once per day the pre-open list (today's NextDayWatchlistEntry rows) is rebuilt from the feed: new positives are added and injected, a new
    negative turns an existing row into a veto row;
  * unknown polarity never vetoes or triggers; the feed down / ok:false falls back to the old RSS keyword pass below (an outage changes nothing).

Everything below this note is the original (2026-10-04) design, still used as the fallback path.

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

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import config
import models
import symbol_master
from event_depth_local import classify_text
from watchlist_engine import afterhours_scan as _ah

logger = logging.getLogger("real-trade-intraday-news")
IST_TZ = timezone(timedelta(hours=5, minutes=30))

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
    _neg_cache.clear()
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


def _weight(e: dict) -> float:
    """Sort weight of a stored entry: the legacy nudge size / 10, or the typed strength (0..1)."""
    try:
        return max(abs(float(e.get("bonus", 0.0))) / 10.0, float(e.get("strength", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _merge(old: dict, new: dict, cutoff: datetime, veto_cutoff: Optional[datetime] = None) -> dict:
    """Merge new hits into the stored map: drop expired (a typed NEGATIVE entry lives until `veto_cutoff`, the rest until `cutoff`), a typed negative beats a
    typed positive for the same symbol, otherwise newest-wins on conflicting kind and strongest-wins on the same kind, then keep the strongest
    _MAX_SNAPSHOT_SYMBOLS."""
    veto_cutoff = veto_cutoff or cutoff
    out = {}
    for sym, e in (old or {}).items():
        ts = _entry_ts(e)
        limit = veto_cutoff if (e.get("typed") and e.get("kind") == "neg") else cutoff
        if ts is not None and ts >= limit:
            out[sym] = e
    for sym, e in new.items():
        cur = out.get(sym)
        if cur is None:
            out[sym] = e
        elif cur.get("kind") != e.get("kind"):
            if cur.get("typed") and e.get("typed"):
                out[sym] = cur if cur.get("kind") == "neg" else e          # negative news wins
                continue
            cts, ets = _entry_ts(cur), _entry_ts(e)
            if ets is not None and (cts is None or ets >= cts):
                out[sym] = e
        elif _weight(e) > _weight(cur):
            out[sym] = e
    if len(out) > _MAX_SNAPSHOT_SYMBOLS:
        keep = sorted(out.items(), key=lambda kv: _weight(kv[1]), reverse=True)[:_MAX_SNAPSHOT_SYMBOLS]
        out = dict(keep)
    return out


def _f(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x and abs(x) != float("inf") else default
    except (TypeError, ValueError):
        return default


def entries_from_hits(hits: dict) -> dict:
    """{symbol: stored entry} from event_reader.hits_from_events output. The entry carries type / polarity / strength / announce time; `bonus` is 0.0 (the
    ranking nudge is replaced by the veto and the trigger). A hit with no parseable announce time is skipped (it cannot be shown to be fresh). Never raises."""
    out: dict = {}
    try:
        for sym, h in (hits or {}).items():
            sym = str(sym).upper()
            if sym in _GENERIC_TOKENS:
                continue
            pol = int(_f(h.get("polarity")))
            if pol == 0:
                continue
            dt = _ah._parse_item_datetime(h.get("published"))
            if dt is None:
                continue
            out[sym] = {
                "kind": "pos" if pol > 0 else "neg", "typed": True, "polarity": 1 if pol > 0 else -1,
                "type": str(h.get("event_type") or "other"), "strength": round(_f(h.get("strength")), 3), "bonus": 0.0,
                "catalyst": str(h.get("catalyst_type") or "news"), "headline": str(h.get("headline") or "")[:200],
                "source": str(h.get("source") or "feed"), "ts": dt.astimezone(timezone.utc).isoformat(),
                "score": round(_f(h.get("score")), 1),
            }
    except Exception:  # noqa: BLE001
        logger.warning("intraday-news: entries_from_hits failed", exc_info=True)
    return out


def _changed(old: dict, fresh: dict) -> dict:
    """The part of `fresh` that is new or different from what is stored (symbol absent, other kind / type / headline)."""
    out = {}
    for sym, e in fresh.items():
        cur = (old or {}).get(sym)
        if cur is None or (cur.get("kind"), cur.get("type"), cur.get("headline")) != (e.get("kind"), e.get("type"), e.get("headline")):
            out[sym] = e
    return out


def _veto_strength_ok(e: dict) -> bool:
    return _f(e.get("strength")) >= float(config.INTRADAY_EVENT_VETO_MIN_STRENGTH)


def _modes_enabled(db) -> list:
    try:
        rows = (db.query(models.TradeGateState).filter(models.TradeGateState.afterhours_news_scan_enabled == True).all())  # noqa: E712  (Oracle: `= 1`, not IS)
        return sorted({str(r.mode) for r in rows if getattr(r, "mode", None)})
    except Exception:  # noqa: BLE001
        return []


def _snapshot_today(db, today: str) -> dict:
    from resilience.local_cache import load_snapshot
    snap = load_snapshot(db, SNAPSHOT_KEY) or {}
    return dict(snap) if snap.get("date") == today else {"date": today}


def _save(db, snap: dict, now: datetime) -> None:
    from resilience.local_cache import save_snapshot
    snap["updated"] = now.isoformat()
    save_snapshot(db, SNAPSHOT_KEY, snap)
    _bonus_cache.update({"at": 0.0, "data": None})   # the next Gate read sees it immediately


def _ist_time(now: datetime):
    return now.astimezone(IST_TZ).time()


def _hhmm(value: str, h: int, m: int):
    from tz_utils import parse_hhmm
    return parse_hhmm(value, h, m)


# ── typed-feed pass ─────────────────────────────────────────────────────────────

async def _fetch_feed() -> Optional[dict]:
    """The feed's parsed /events/active body, or None when the feed is down / ok:false / unreadable (the caller falls back to the RSS keyword pass)."""
    from watchlist_engine import event_reader
    return await asyncio.to_thread(event_reader.fetch_active, config.EVENT_URL, float(getattr(config, "AFTERHOURS_EVENT_FEED_TIMEOUT_S", 4.0)))


def _fresh_events(doc: dict, now: datetime) -> list:
    """Feed records published inside the longest window we act on (the veto window); the age is re-worked from the announce time, not trusted from the feed."""
    max_s = max(int(config.INTRADAY_NEWS_MAX_AGE_MINUTES), int(config.INTRADAY_EVENT_VETO_MAX_AGE_MINUTES)) * 60.0
    out = []
    for e in doc.get("events") or []:
        if not isinstance(e, dict):
            continue
        ts = e.get("published_ts")
        pub = _ah._parse_item_datetime(e.get("published")) if ts in (None, "") else datetime.fromtimestamp(_f(ts), tz=timezone.utc)
        if pub is None or (now - pub).total_seconds() > max_s or (now - pub).total_seconds() < -300:
            continue
        out.append(e)
    return out


async def _inject_triggers(db, snap: dict, now: datetime, modes: list) -> list:
    """Queue a candidate for each fresh, strong, positive event of a trigger type (see the module docstring). Returns [(mode, symbol)]. Never raises."""
    done: list = []
    try:
        if not config.INTRADAY_EVENT_TRIGGER or int(config.INTRADAY_EVENT_TRIGGER_MAX_PER_DAY) <= 0 or not modes:
            return done
        if _ist_time(now) < _hhmm(config.INTRADAY_EVENT_TRIGGER_START_IST, 9, 20):
            return done
        triggered = snap.setdefault("triggered", {})
        types = set(config.INTRADAY_EVENT_TRIGGER_TYPES)
        max_age = timedelta(minutes=int(config.INTRADAY_EVENT_TRIGGER_MAX_AGE_MINUTES))
        cands = []
        for sym, e in (snap.get("symbols") or {}).items():
            if not (e.get("typed") and e.get("kind") == "pos" and e.get("type") in types):
                continue
            ts = _entry_ts(e)
            if ts is None or (now - ts) > max_age or _f(e.get("strength")) < float(config.INTRADAY_EVENT_TRIGGER_MIN_STRENGTH):
                continue
            cands.append((sym, e))
        if not cands:
            return done
        cands.sort(key=lambda kv: _f(kv[1].get("strength")), reverse=True)
        preview: dict = {}
        try:
            from market_feed.feed import get_preview_quotes
            q = await get_preview_quotes([c[0] for c in cands])
            preview = {k: t.price for k, t in (q or {}).items() if t and t.price}
        except Exception:  # noqa: BLE001
            logger.info("intraday-news: preview-price lookup failed - no trigger this tick", exc_info=True)
        for mode in modes:
            used = list(triggered.get(mode) or [])
            queued = {r[0] for r in db.query(models.TradeCandidate.symbol).filter_by(mode=mode, consumed=False).all()}
            held = {r[0] for r in db.query(models.TradePosition.symbol).filter(models.TradePosition.mode == mode, models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED"))).all()}
            for sym, e in cands:
                if len(used) >= int(config.INTRADAY_EVENT_TRIGGER_MAX_PER_DAY):
                    break
                if sym in used or sym in queued or sym in held:
                    continue
                price = preview.get(sym)
                if not price:        # the drift gate needs a reference price: without one a chased headline could be bought at any level
                    continue
                db.add(models.TradeCandidate(
                    mode=mode, symbol=sym, source_tab="intraday_event", decision_label="BUY NOW",
                    conviction_score=_f(e.get("score")), signal_price=float(price),
                    raw_payload=json.dumps({"catalyst_type": e.get("catalyst"), "source": e.get("source"), "headline": e.get("headline"),
                                            "event": {"type": e.get("type"), "polarity": 1, "strength": e.get("strength")}}),
                    overnight_priority=False,
                ))
                db.commit()
                used.append(sym)
                done.append((mode, sym))
            triggered[mode] = used
    except Exception:  # noqa: BLE001
        db.rollback()
        logger.exception("intraday-news: trigger injection failed (non-fatal)")
    return done


async def _feed_pass(db, now: datetime, known, doc: dict, result: dict) -> dict:
    """One typed-feed tick: store entries, then queue triggers. Returns the result dict."""
    from tz_utils import ist_today_str
    from notifier import notify_async
    events = _fresh_events(doc, now)
    result["fetched"] = len(events)
    from watchlist_engine import event_reader
    hits = event_reader.hits_from_events(events, known)
    fresh = entries_from_hits(hits)
    today = ist_today_str()
    snap = _snapshot_today(db, today)
    old = snap.get("symbols") or {}
    cutoff = now - timedelta(minutes=int(config.INTRADAY_NEWS_MAX_AGE_MINUTES))
    veto_cutoff = now - timedelta(minutes=int(config.INTRADAY_EVENT_VETO_MAX_AGE_MINUTES))
    changed = _changed(old, fresh)
    result["new"] = len(changed)
    result["pos"] = sum(1 for e in changed.values() if e["kind"] == "pos")
    result["neg"] = sum(1 for e in changed.values() if e["kind"] == "neg")
    result["source"] = "feed"
    result["ran"] = True
    modes = _modes_enabled(db)
    merged = _merge(old, fresh, cutoff, veto_cutoff)
    dirty = bool(changed) or merged.keys() != old.keys()
    snap["symbols"] = merged
    snap["source"] = "feed"
    triggers = await _inject_triggers(db, snap, now, modes)
    result["triggered"] = len(triggers)
    if dirty or triggers:
        _save(db, snap, now)
    result["symbols"] = len(merged) if (dirty or triggers) else 0

    # Compact alert: new vetoes on a queued or held symbol, every trigger, and strong new positives are not alerted (the trigger itself is).
    try:
        queued = {r[0] for r in db.query(models.TradeCandidate.symbol).filter_by(consumed=False).all()}
        held = {r[0] for r in db.query(models.TradePosition.symbol).filter(models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED"))).all()}
    except Exception:  # noqa: BLE001
        queued, held = set(), set()
    lines = []
    for mode, sym in triggers:
        e = merged.get(sym, {})
        lines.append(f"🟢 *{sym}* · TRIGGER {mode} · {e.get('type')} +, strength {_f(e.get('strength')):.2f} · queued")
        lines.append(f"   _{str(e.get('headline') or '')[:100]}_")
    for sym, e in sorted(changed.items(), key=lambda kv: _weight(kv[1]), reverse=True):
        if e["kind"] == "neg" and _veto_strength_ok(e) and (sym in queued or sym in held):
            tag = "HELD" if sym in held else "in queue"
            lines.append(f"⛔ *{sym}* ({tag}) · VETO · negative {e.get('type')}, strength {_f(e.get('strength')):.2f}")
            lines.append(f"   _{str(e.get('headline') or '')[:100]}_")
    if lines:
        try:
            await notify_async("📰 *Market-hours events*\n" + "\n".join(lines[:16]))
            result["alerted"] = len(lines) // 2
        except Exception:  # noqa: BLE001
            logger.debug("intraday-news: alert failed (non-fatal)", exc_info=True)
    if changed or triggers:
        logger.info("intraday-news[feed]: events=%d new=%d pos=%d neg=%d tracked=%d triggered=%d",
                    result["fetched"], result["new"], result["pos"], result["neg"], len(merged), len(triggers))
    return result


# ── 09:05 pre-open rebuild ──────────────────────────────────────────────────────

async def rebuild_preopen_list(db, doc: dict, now: datetime, modes: list, known=None) -> dict:
    """Rebuild today's pre-open list (NextDayWatchlistEntry rows for today) from the feed: a symbol with no row yet is added (positive hits at or above the
    inject score) and the new rows are injected as overnight-priority candidates; a negative hit turns an existing row into a VETO row (and adds a veto row
    when there is none). A symbol already holding a row keeps it unless a veto replaces it (`afterhours_scan._upsert_wins`). Never raises."""
    out = {"added": 0, "vetoed": 0, "injected": 0}
    try:
        from watchlist_engine import event_reader
        from tz_utils import ist_today_str
        today = ist_today_str()
        hits = event_reader.hits_from_events(doc.get("events") or [], known or None)
        for mode in modes:
            rows = {r.symbol: r for r in db.query(models.NextDayWatchlistEntry).filter_by(mode=mode, market_date=today).all()}
            for sym, hit in hits.items():
                row = rows.get(sym)
                try:
                    if row is None:
                        if hit["polarity"] > 0 and hit["score"] < float(config.AFTERHOURS_SCAN_MIN_INJECT_SCORE):
                            continue
                        db.add(models.NextDayWatchlistEntry(
                            mode=mode, symbol=sym, catalyst_type=hit["catalyst_type"], catalyst_source=hit["source"], headline=hit["headline"],
                            priority_score=hit["score"], market_date=today, collected_at=now, consumed=False))
                        db.commit()
                        out["vetoed" if hit["polarity"] < 0 else "added"] += 1
                    elif hit["polarity"] < 0 and not event_reader.is_veto(row.catalyst_source):
                        row.priority_score = 0.0
                        row.catalyst_type = hit["catalyst_type"]
                        row.catalyst_source = hit["source"]
                        row.headline = hit["headline"]
                        row.updated_at = now
                        db.commit()
                        out["vetoed"] += 1
                    elif not row.consumed and _ah._upsert_wins(hit, row):
                        row.priority_score = hit["score"]
                        row.catalyst_type = hit["catalyst_type"]
                        row.catalyst_source = hit["source"]
                        row.headline = hit["headline"]
                        row.updated_at = now
                        db.commit()
                        out["added"] += 1
                except Exception:  # noqa: BLE001
                    db.rollback()
                    logger.warning("intraday-news: pre-open rebuild could not store %s %s", mode, sym, exc_info=True)
            if out["added"]:
                from execution import auto_pilot
                out["injected"] += await auto_pilot._inject_nextday_watchlist_candidates(db, mode)
    except Exception:  # noqa: BLE001
        logger.exception("intraday-news: pre-open rebuild failed (non-fatal)")
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

    if config.INTRADAY_EVENT_SOURCE == "feed":
        doc = await _fetch_feed()
        if doc is not None:
            try:
                from tz_utils import ist_today_str
                _today = ist_today_str()
                _t = _ist_time(now)
                _snap = _snapshot_today(db, _today)
                if (_hhmm(config.INTRADAY_PREOPEN_REBUILD_IST, 9, 5) <= _t < _hhmm(config.INTRADAY_EVENT_TRIGGER_START_IST, 9, 20)
                        and _snap.get("rebuild_done") != _today):
                    res = await rebuild_preopen_list(db, doc, now, _modes_enabled(db), known)
                    _snap["rebuild_done"] = _today
                    _snap["rebuild"] = res
                    _save(db, _snap, now)
                    result["rebuild"] = res
                    logger.info("intraday-news: 09:05 pre-open list rebuilt: %s", res)
                return await _feed_pass(db, now, known, doc, result)
            except Exception:  # noqa: BLE001
                db.rollback()
                logger.exception("intraday-news: feed pass failed - falling back to the RSS keyword pass")
        else:
            logger.info("intraday-news: event feed not available - RSS keyword pass this tick")

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
    snap = _snapshot_today(db, today)
    old = snap.get("symbols")
    merged = _merge(old or {}, fresh, cutoff, now - timedelta(minutes=int(config.INTRADAY_EVENT_VETO_MAX_AGE_MINUTES)))
    snap["symbols"] = merged
    snap["updated"] = now.isoformat()
    save_snapshot(db, SNAPSHOT_KEY, snap)
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
        if not e or e.get("typed"):
            return 0.0           # E272 round 7: a typed event is a veto / trigger, not a ranking nudge
        ts = _entry_ts(e)
        now = now or datetime.now(timezone.utc)
        if ts is None or (now - ts) > timedelta(minutes=int(config.INTRADAY_NEWS_MAX_AGE_MINUTES)):
            return 0.0
        v = float(e.get("bonus", 0.0))
        return max(-abs(float(config.INTRADAY_NEWS_PENALTY_CAP)), min(abs(float(config.INTRADAY_NEWS_BONUS_CAP)), v))
    except Exception:
        return 0.0


def positive_event(db, symbol: str, now: Optional[datetime] = None) -> Optional[dict]:
    """E273 round 19: the fresh typed POSITIVE event for `symbol` from today's market-hours snapshot, as {"polarity": 1, "type", "strength", "age_min"}, or None.
    Typed entries only (a keyword-scan hit is not an event), announced within INTRADAY_NEWS_MAX_AGE_MINUTES. Unknown polarity is never positive. Never raises."""
    try:
        from tz_utils import ist_today_str
        now = now or datetime.now(timezone.utc)
        data = _load_cached(db)
        if not data or data.get("date") != ist_today_str():
            return None
        e = (data.get("symbols") or {}).get(symbol)
        if not e or not e.get("typed") or e.get("kind") != "pos":
            return None
        ts = _entry_ts(e)
        if ts is None or (now - ts) > timedelta(minutes=int(config.INTRADAY_NEWS_MAX_AGE_MINUTES)):
            return None
        return {"polarity": 1, "type": str(e.get("type") or "news"), "strength": _f(e.get("strength")), "age_min": int((now - ts).total_seconds() // 60)}
    except Exception:  # noqa: BLE001
        return None


_neg_cache: dict = {}


def positive_event_any(db, symbol: str, now: Optional[datetime] = None) -> Optional[dict]:
    """E274 round 47: the typed POSITIVE event that backs a swing pick on `symbol` today, as {"polarity": 1, "type", "strength", "source"}, or None. Two sources, fail-open (None):
    today's market-hours snapshot (positive_event), else today's after-hours / pre-open row of the next-day watchlist (any mode) tagged p=+1. Unknown polarity is never positive. Never raises."""
    try:
        sym = str(symbol or "").upper()
        e = positive_event(db, sym, now)
        if e:
            return {**e, "source": "intraday"}
        from tz_utils import ist_today_str
        from watchlist_engine import event_reader
        best = None
        for r in db.query(models.NextDayWatchlistEntry).filter_by(symbol=sym, market_date=ist_today_str()).all():
            tag = event_reader.decode_tag(r.catalyst_source)
            if tag.get("polarity") == 1 and (best is None or _f(tag.get("strength")) > best["strength"]):
                best = {"polarity": 1, "type": str(tag.get("type") or "news"), "strength": _f(tag.get("strength")), "source": "afterhours"}
        return best
    except Exception:  # noqa: BLE001
        return None


def negative_event(db, symbol: str, opened_at: Any, now: Optional[datetime] = None) -> Optional[dict]:
    """E274 round 45: the strongest NEGATIVE typed event that is news to a position on `symbol` opened at `opened_at`, as {"polarity": -1, "type", "strength", "age_min", "source",
    "headline"}, or None. Two sources, fail-open (None) on anything unreadable, cached 60 s per symbol (the exit loop calls it every cycle):
      1. today's market-hours snapshot: a typed negative entry announced AFTER the position was opened, within EVENT_EXIT_MAX_AGE_MINUTES;
      2. today's after-hours / pre-open veto row (any mode), only for a position opened on an EARLIER day (a position opened today was subject to the entry veto).
    Unknown polarity is never negative. Never raises."""
    try:
        sym = str(symbol or "").upper()
        hit = _neg_cache.get(sym)
        key = str(opened_at)
        if hit and hit[1] == key and time.monotonic() - hit[0] < 60.0:
            return hit[2]
        from tz_utils import as_aware, ist_today_str
        now = now or datetime.now(timezone.utc)
        opened = as_aware(opened_at) if opened_at is not None else None
        best: Optional[dict] = None
        data = _load_cached(db)
        if data and data.get("date") == ist_today_str():
            e = (data.get("symbols") or {}).get(sym)
            if e and e.get("typed") and e.get("kind") == "neg":
                ts = _entry_ts(e)
                if ts is not None and (opened is None or ts > opened) and (now - ts) <= timedelta(minutes=int(config.EVENT_EXIT_MAX_AGE_MINUTES)):
                    best = {"polarity": -1, "type": str(e.get("type") or "news"), "strength": _f(e.get("strength")), "age_min": int((now - ts).total_seconds() // 60),
                            "source": "intraday", "headline": str(e.get("headline") or "")[:120]}
        opened_before_today = opened is not None and opened.astimezone(timezone(timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d") < ist_today_str()
        if opened_before_today:
            from watchlist_engine import event_reader
            for r in db.query(models.NextDayWatchlistEntry).filter_by(symbol=sym, market_date=ist_today_str()).all():
                tag = event_reader.decode_tag(r.catalyst_source)
                if tag.get("polarity") == -1 and (best is None or _f(tag.get("strength")) > _f(best.get("strength"))):
                    best = {"polarity": -1, "type": str(tag.get("type") or "news"), "strength": _f(tag.get("strength")), "age_min": None, "source": "afterhours",
                            "headline": str(r.headline or "")[:120]}
        _neg_cache[sym] = (time.monotonic(), key, best)
        return best
    except Exception:  # noqa: BLE001
        return None


def event_veto_reason(db, mode: str, symbol: str, now: Optional[datetime] = None) -> Optional[str]:
    """Gate 5.9 reader: why a long on `symbol` is refused because of a fresh NEGATIVE typed event, or None. Two sources, both fail-open on anything unreadable:
      1. today's market-hours snapshot (typed negative entry, strength >= INTRADAY_EVENT_VETO_MIN_STRENGTH, announced within INTRADAY_EVENT_VETO_MAX_AGE_MINUTES);
      2. today's veto row in the next-day watchlist for `mode` (negative after-hours news kept by the 09:05 rebuild / finalize), consumed or not.
    Unknown polarity never vetoes. Never raises."""
    try:
        if not config.INTRADAY_EVENT_VETO:
            return None
        from tz_utils import ist_today_str
        now = now or datetime.now(timezone.utc)
        data = _load_cached(db)
        if data and data.get("date") == ist_today_str():
            e = (data.get("symbols") or {}).get(symbol)
            if e and e.get("typed") and e.get("kind") == "neg" and _veto_strength_ok(e):
                ts = _entry_ts(e)
                if ts is not None and (now - ts) <= timedelta(minutes=int(config.INTRADAY_EVENT_VETO_MAX_AGE_MINUTES)):
                    return (f"Fresh negative {e.get('type', 'news')} event on {symbol} (strength {_f(e.get('strength')):.2f}, "
                            f"{int((now - ts).total_seconds() // 60)} min ago): no new long - \"{str(e.get('headline') or '')[:80]}\"")
        from watchlist_engine import event_reader
        for r in db.query(models.NextDayWatchlistEntry).filter_by(mode=mode, symbol=symbol, market_date=ist_today_str()).all():
            tag = event_reader.decode_tag(r.catalyst_source)
            if tag.get("polarity") == -1 and _f(tag.get("strength")) >= float(config.INTRADAY_EVENT_VETO_MIN_STRENGTH):
                return (f"Negative {tag.get('type') or 'news'} event on {symbol} from the after-hours / pre-open scan "
                        f"(strength {_f(tag.get('strength')):.2f}): no new long today - \"{str(r.headline or '')[:80]}\"")
    except Exception:  # noqa: BLE001
        return None
    return None
