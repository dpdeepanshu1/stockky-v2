"""event/event_feed.py - typed, timestamped news-and-event feed (task 1 of the opening / entry / risk rework, 31 Oct 2026).

One feed both trading services can read: every item is a typed event for a KNOWN symbol with a polarity (+1 / 0 / -1), a strength (0..1) and the age of
the item, so an entry setup can say "S1 catalyst up, results beat 14 min ago" instead of guessing from a headline.

Pure part (no network, no fastapi): classify(), extract_symbol(), parse_published(), dedupe, strength(), build_response().
Impure part: FeedCollector (polls the RSS sources through an injected fetch function; a source that answers 401/403/406/429/451 is left alone for
EVENT_FEED_BLOCK_COOLDOWN_S) and start_collector() (one daemon thread).

Rules that matter:
  * A symbol is NEVER guessed: only the event service's own subscription list plus EVENT_FEED_EXTRA_SYMBOLS can match.
  * An item with no readable publish time is dropped (it cannot be shown to be fresh).
  * The same headline for the same symbol inside 6 hours is one event.
  * A regulatory negative beats win words in the same headline ("wins order, SEBI probe" is negative); "probe dismissed" is neutral;
    a negative word about a COST ("fall in input costs") is not negative; "loss narrows" is positive.
  * With no healthy source the routes answer ok:false with no events. Nothing here blocks a trade by itself.

Env: EVENT_FEED_ENABLED (1), EVENT_FEED_BLOCK_COOLDOWN_S (1800), EVENT_FEED_EXTRA_SYMBOLS (comma list, optional), EVENT_FEED_POLL_S (300),
EVENT_FEED_MAX_AGE_H (24).
"""
from __future__ import annotations

import logging
import math
import os
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger("event-feed")

# (source name, url). Exchange filings are added only after scripts/check_event_sources.py proves one of these reads OK from the VM.
FEEDS: List[Tuple[str, str]] = [
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Moneycontrol-business", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol-google", "https://news.google.com/rss/search?q=site:moneycontrol.com+when:1d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("LiveMint", "https://www.livemint.com/rss/markets"),
    ("NDTVProfit", "https://www.ndtvprofit.com/rss/markets"),
    ("NDTVProfit-google", "https://news.google.com/rss/search?q=site:ndtvprofit.com+when:1d&hl=en-IN&gl=IN&ceid=IN:en"),
    ("BusinessStandard", "https://www.business-standard.com/rss/markets-106.rss"),
]

BLOCK_STATUSES = frozenset({401, 403, 406, 429, 451})
DEDUPE_WINDOW_S = 6 * 3600
HALF_LIFE_S = 3600.0

# ── keyword tables (lower case; matched on word boundaries) ──────────────────────────────────────────────────────────────────────
POSITIVE = (
    "wins order", "bags order", "order win", "secures order", "bags contract", "wins contract", "secures contract", "profit jumps", "profit rises",
    "profit surges", "profit up", "profit climbs", "record profit", "beats estimates", "beats", "upgrade", "upgrades", "raises target", "buyback",
    "dividend", "acquires", "approval", "nod", "lifts", "lifted", "boosts", "boosted", "supports", "surges", "rallies", "jumps", "soars", "record",
    "narrows", "turns profit", "swings to profit", "launches", "wins",
)
NEGATIVE = (
    "probe", "fraud", "downgrade", "downgrades", "cuts target", "loss widens", "net loss", "profit falls", "profit drops", "profit slumps",
    "profit declines", "misses estimates", "misses", "resigns", "resignation", "raid", "raids", "default", "penalty", "fine", "fined", "ban", "banned",
    "delist", "delisting", "insolvency", "nclt", "recall", "warning letter", "plunges", "plunge", "slumps", "falls", "drops", "tanks", "crashes",
    "cuts", "shuts", "shutdown", "show cause", "notice", "investigation", "pledge", "widens",
)
REGULATORY = ("sebi", "probe", "fraud", "raid", "raids", "penalty", "fine", "fined", "ban", "banned", "nclt", "insolvency", "delist", "delisting",
              "warning letter", "investigation", "show cause", "notice", "cbi", "enforcement directorate", "usfda")
NEUTRALISERS = ("dismissed", "dismisses", "cleared", "clears", "no action", "relief", "quashes", "quashed", "withdrawn", "drops probe", "closes probe")
RESULT_WORDS = ("results", "q1", "q2", "q3", "q4", "profit", "loss", "revenue", "earnings", "ebitda", "net profit", "pat")
DEAL_WORDS = ("stake", "acquires", "acquisition", "merger", "block deal", "bulk deal", "buyback", "takeover", "open offer", "demerger")
ORDER_WORDS = ("order", "contract", "bags", "secures", "wins", "tender")
RATING_WORDS = ("upgrade", "upgrades", "downgrade", "downgrades", "target price", "rating", "initiates coverage", "target")
MGMT_WORDS = ("resigns", "resignation", "appoints", "appointed", "ceo", "cfo", "managing director", "md ")
COST_WORDS = ("cost", "costs", "crude", "raw material", "input", "expenses", "interest rate", "interest rates", "freight", "commodity", "oil prices")
COST_CONNECTORS = ("in", "of", "on", "to")

# base weight of an event type for strength()
TYPE_WEIGHT = {"regulatory": 0.9, "results": 0.85, "deal": 0.7, "order": 0.65, "rating": 0.45, "management": 0.4, "other": 0.2}
TYPE_PRIORITY = ("regulatory", "results", "deal", "order", "rating", "management", "other")

_WORD_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _has(text: str, phrase: str) -> bool:
    pat = _WORD_CACHE.get(phrase)
    if pat is None:
        pat = _WORD_CACHE[phrase] = re.compile(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])")
    return bool(pat.search(text))


def _any(text: str, words: Iterable[str]) -> List[str]:
    return [w for w in words if _has(text, w)]


def _f(v: Any, default: float = 0.0) -> float:
    """Safe float: a dict / object / junk never raises (the 'float() argument must be ... not object' bug)."""
    try:
        if isinstance(v, bool):
            return float(v)
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


# ── classification ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _negative_hits(h: str) -> List[str]:
    """Negative words that count: a negative word followed within three words by a cost word ('fall in input costs') is a good thing, not bad."""
    hits = []
    toks = re.findall(r"[a-z0-9]+", h)
    for w in _any(h, NEGATIVE):
        first = w.split()[0]
        ok = True
        for i, t in enumerate(toks):
            if t == first:
                tail = toks[i + 1:i + 4]
                if tail and tail[0] in COST_CONNECTORS and any(c.split()[0] in tail for c in COST_WORDS):
                    ok = False
                elif tail and any(c.split()[0] in tail[:2] for c in COST_WORDS):
                    ok = False
                break
        if ok:
            hits.append(w)
    return hits


def classify(headline: str) -> Dict[str, Any]:
    """-> {type, polarity, hits}. polarity is +1 / 0 / -1. Never raises."""
    h = " ".join(str(headline or "").lower().split())
    if not h:
        return {"type": "other", "polarity": 0, "hits": []}
    pos = _any(h, POSITIVE)
    neg = _negative_hits(h)
    neutral = bool(_any(h, NEUTRALISERS))
    reg = _any(h, REGULATORY)

    if reg:
        etype = "regulatory"
        # a regulatory word wins over any "win" words in the same headline, unless the headline says it was dismissed / cleared
        polarity = 0 if neutral else -1
        return {"type": etype, "polarity": polarity, "hits": reg}

    etype = "other"
    if _any(h, RESULT_WORDS):
        etype = "results"
    elif _any(h, DEAL_WORDS):
        etype = "deal"
    elif _any(h, ORDER_WORDS):
        etype = "order"
    elif _any(h, RATING_WORDS):
        etype = "rating"
    elif _any(h, MGMT_WORDS):
        etype = "management"

    # "loss narrows" / "loss shrinks" is good news, "loss widens" is bad
    if _has(h, "loss") and (_has(h, "narrows") or _has(h, "narrowed") or _has(h, "shrinks") or _has(h, "shrank")):
        return {"type": "results" if etype in ("results", "other") else etype, "polarity": 1, "hits": ["loss narrows"]}
    if neutral and not pos:
        return {"type": etype, "polarity": 0, "hits": neg}
    if neg and not pos:
        polarity = -1
    elif pos and not neg:
        polarity = 1
    elif pos and neg:
        polarity = -1 if len(neg) > len(pos) else (1 if len(pos) > len(neg) else 0)
    else:
        polarity = 0
    return {"type": etype, "polarity": polarity, "hits": pos if polarity > 0 else neg}


# ── symbols ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
_SYM_RE = re.compile(r"\b([A-Z][A-Z0-9&\-]{1,19})\b")


def extract_symbol(headline: str, known: Iterable[str]) -> Optional[str]:
    """The first KNOWN symbol written as an upper-case token in the headline. Never guesses: an unknown token returns None."""
    kn = {str(s).strip().upper() for s in (known or []) if s}
    if not kn:
        return None
    for m in _SYM_RE.finditer(str(headline or "")):
        tok = m.group(1)
        if tok in kn:
            return tok
    return None


# ── time ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def parse_published(entry: Any) -> Optional[datetime]:
    """UTC datetime of a feed entry (struct_time or a date string), or None when unreadable."""
    try:
        get = entry.get if hasattr(entry, "get") else (lambda k, d=None: getattr(entry, k, d))
        st = get("published_parsed") or get("updated_parsed")
        if st:
            return datetime(*list(st)[:6], tzinfo=timezone.utc)
        raw = get("published") or get("updated") or ""
        if isinstance(raw, datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
        raw = str(raw).strip()
        if not raw:
            return None
        try:
            dt = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None


def strength(etype: str, polarity: int, age_s: float, source_weight: float = 1.0) -> float:
    """0..1: the type's weight, halved every hour of age, zero for a neutral item. A source weight above 1 never pushes it past 1."""
    if not polarity:
        return 0.0
    base = TYPE_WEIGHT.get(etype, 0.2)
    decay = 0.5 ** (max(0.0, _f(age_s)) / HALF_LIFE_S)
    return round(max(0.0, min(1.0, base * decay * max(0.0, _f(source_weight, 1.0)))), 3)


def _key(symbol: str, headline: str) -> str:
    return symbol + "|" + re.sub(r"[^a-z0-9]+", " ", str(headline).lower()).strip()[:160]


def dedupe(events: List[Dict[str, Any]], window_s: float = DEDUPE_WINDOW_S) -> List[Dict[str, Any]]:
    """One event per (symbol, headline) inside the window; the earliest-published item is kept, outside the window the same headline counts again."""
    out: List[Dict[str, Any]] = []
    last: Dict[str, float] = {}
    for e in sorted(events, key=lambda x: _f(x.get("published_ts"))):
        k = e.get("key") or _key(e.get("symbol", ""), e.get("headline", ""))
        t = _f(e.get("published_ts"))
        if k in last and t - last[k] < window_s:
            continue
        last[k] = t
        out.append(e)
    return out


def make_event(symbol: str, headline: str, published: datetime, source: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    c = classify(headline)
    age = max(0.0, (now - published).total_seconds())
    return {
        "symbol": symbol, "type": c["type"], "polarity": c["polarity"], "strength": strength(c["type"], c["polarity"], age),
        "headline": str(headline)[:300], "source": source, "published": published.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "published_ts": published.timestamp(), "age_s": int(age), "key": _key(symbol, headline),
    }


# ── response ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def build_response(events: List[Dict[str, Any]], sources: Dict[str, Dict[str, Any]], symbol: Optional[str] = None,
                   now: Optional[datetime] = None, max_age_h: float = 24.0, limit: int = 200) -> Dict[str, Any]:
    """The body of GET /events/active (all symbols) or /events/{symbol}/active. Ages are re-worked at read time. Never raises."""
    try:
        now = now or datetime.now(timezone.utc)
        healthy = [n for n, s in (sources or {}).items() if isinstance(s, dict) and s.get("ok")]
        out = []
        for e in events or []:
            if symbol and str(e.get("symbol")).upper() != symbol.strip().upper():
                continue
            age = max(0.0, now.timestamp() - _f(e.get("published_ts")))
            if age > max_age_h * 3600.0:
                continue
            d = {k: v for k, v in e.items() if k not in ("published_ts", "key")}
            d["age_s"] = int(age)
            d["strength"] = strength(e.get("type", "other"), int(_f(e.get("polarity"))), age)
            out.append(d)
        out.sort(key=lambda d: (-_f(d.get("strength")), _f(d.get("age_s"))))
        delays = [_f(s.get("last_delay_s"), -1.0) for s in (sources or {}).values() if isinstance(s, dict) and s.get("ok")]
        delays = [d for d in delays if d >= 0]
        return {
            "ok": bool(healthy), "symbol": symbol.strip().upper() if symbol else None, "count": len(out[:limit]), "events": out[:limit],
            "healthy_sources": sorted(healthy), "last_delay_s": min(delays) if delays else None,
            "sources": {n: {"ok": bool(s.get("ok")), "status": s.get("status"), "items": s.get("items"), "blocked_until": s.get("blocked_until")}
                        for n, s in (sources or {}).items() if isinstance(s, dict)},
            "generated_at": now.isoformat(timespec="seconds"),
        }
    except Exception as e:  # noqa: BLE001 - fail safe: ok:false, no events
        logger.warning("event feed: build_response failed (%s)", e)
        return {"ok": False, "symbol": symbol, "count": 0, "events": [], "healthy_sources": [], "last_delay_s": None, "sources": {}, "error": str(e)[:120]}


# ── collector ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
FetchFn = Callable[[str], Tuple[List[Any], Optional[int]]]


def _default_fetch(url: str) -> Tuple[List[Any], Optional[int]]:
    """(entries, http status) through the service's shared downloader (news/feed_fetch.py, loaded by path); (list, None) when it is unavailable."""
    import importlib.util as ilu
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "news", "feed_fetch.py")
    spec = ilu.spec_from_file_location("event_feed_shared_fetch", path)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parsed, info = mod.fetch_feed_ex(url, "event-feed")
    return list(getattr(parsed, "entries", []) or []), (info or {}).get("status")


class FeedCollector:
    def __init__(self, symbols_fn: Callable[[], Iterable[str]], fetch_fn: Optional[FetchFn] = None, feeds: Optional[List[Tuple[str, str]]] = None,
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc), cooldown_s: Optional[float] = None,
                 max_age_h: float = 24.0, snapshot_fn: Optional[Callable[[dict], None]] = None):
        self.symbols_fn = symbols_fn
        self.fetch_fn = fetch_fn or _default_fetch
        self.feeds = list(feeds if feeds is not None else FEEDS)
        self.now_fn = now_fn
        self.cooldown_s = _f(os.getenv("EVENT_FEED_BLOCK_COOLDOWN_S"), 1800.0) if cooldown_s is None else float(cooldown_s)
        self.max_age_h = max_age_h
        self.snapshot_fn = snapshot_fn
        self.events: List[Dict[str, Any]] = []
        self.sources: Dict[str, Dict[str, Any]] = {}
        self._blocked_until: Dict[str, float] = {}
        self._lock = threading.Lock()

    def known_symbols(self) -> set:
        syms = {str(s).strip().upper() for s in (self.symbols_fn() or []) if s}
        extra = (os.getenv("EVENT_FEED_EXTRA_SYMBOLS") or "").replace(";", ",").split(",")
        syms |= {x.strip().upper() for x in extra if x.strip()}
        return syms

    def poll_once(self) -> Dict[str, Any]:
        now = self.now_fn()
        mono = time.monotonic()
        known = self.known_symbols()
        fresh: List[Dict[str, Any]] = []
        sources: Dict[str, Dict[str, Any]] = {}
        for name, url in self.feeds:
            if self._blocked_until.get(name, 0.0) > mono:
                sources[name] = {"ok": False, "status": "blocked_cooldown", "items": 0,
                                 "blocked_until": int(self._blocked_until[name] - mono)}
                continue
            try:
                entries, status = self.fetch_fn(url)
            except Exception as e:  # noqa: BLE001
                sources[name] = {"ok": False, "status": f"error:{type(e).__name__}", "items": 0}
                continue
            if status in BLOCK_STATUSES:
                self._blocked_until[name] = mono + self.cooldown_s
                sources[name] = {"ok": False, "status": status, "items": 0, "blocked_until": int(self.cooldown_s)}
                continue
            n_items, newest, n_old = 0, None, 0
            for en in entries or []:
                try:
                    get = en.get if hasattr(en, "get") else (lambda k, d=None, _e=en: getattr(_e, k, d))
                    title = str(get("title") or "").strip()
                    pub = parse_published(en)
                    if not title or pub is None:
                        continue
                    if (now - pub).total_seconds() > self.max_age_h * 3600.0 or pub > now + timedelta(minutes=5):
                        n_old += 1 if pub <= now else 0
                        continue
                    n_items += 1
                    newest = pub if newest is None or pub > newest else newest
                    sym = extract_symbol(title, known)
                    if sym:
                        fresh.append(make_event(sym, title, pub, name, now))
                except Exception:  # noqa: BLE001 - one bad item never drops the poll
                    continue
            ok = bool(entries) and (status is None or status == 200 or isinstance(status, int) and 200 <= status < 300)
            if ok and n_items == 0 and n_old > 0:
                # E284: a feed whose every readable item is older than the age limit (a dead RSS URL still answers 200 with years-old items) is NOT a healthy
                # source, and is polled again only after the block cooldown instead of every cycle.
                self._blocked_until[name] = mono + self.cooldown_s
                sources[name] = {"ok": False, "status": "stale", "items": 0, "last_delay_s": None, "blocked_until": int(self.cooldown_s)}
                continue
            sources[name] = {"ok": ok, "status": status if status is not None else ("ok" if ok else "empty"), "items": n_items,
                             "last_delay_s": int((now - newest).total_seconds()) if newest else None}
        with self._lock:
            merged = dedupe(self.events + fresh)
            cutoff = now.timestamp() - self.max_age_h * 3600.0
            self.events = [e for e in merged if _f(e.get("published_ts")) >= cutoff]
            self.sources = sources
        if self.snapshot_fn:
            try:
                self.snapshot_fn({"events": self.events[-500:], "sources": sources, "at": now.isoformat()})
            except Exception:  # noqa: BLE001
                pass
        return {"sources": sources, "events": len(self.events)}

    def response(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        with self._lock:
            ev, src = list(self.events), dict(self.sources)
        return build_response(ev, src, symbol, self.now_fn(), self.max_age_h)


_collector: Optional[FeedCollector] = None


def enabled() -> bool:
    return (os.getenv("EVENT_FEED_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def get_collector() -> Optional[FeedCollector]:
    return _collector


def start_collector(symbols_fn: Callable[[], Iterable[str]], fetch_fn: Optional[FetchFn] = None, interval_s: Optional[float] = None,
                    snapshot_fn: Optional[Callable[[dict], None]] = None) -> Optional[FeedCollector]:
    """Start the one polling thread (daemon). None when EVENT_FEED_ENABLED=0. Calling it twice returns the running collector."""
    global _collector
    if not enabled():
        return None
    if _collector is not None:
        return _collector
    _collector = FeedCollector(symbols_fn, fetch_fn, snapshot_fn=snapshot_fn)
    every = max(60.0, _f(os.getenv("EVENT_FEED_POLL_S"), 300.0) if interval_s is None else float(interval_s))

    def _loop() -> None:
        while True:
            try:
                _collector.poll_once()
            except Exception as e:  # noqa: BLE001
                logger.warning("event feed: poll failed (%s)", e)
            time.sleep(every)

    threading.Thread(target=_loop, name="event-feed-collector", daemon=True).start()
    return _collector
