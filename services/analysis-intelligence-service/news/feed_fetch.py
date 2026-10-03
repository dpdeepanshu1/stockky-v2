"""
news/feed_fetch.py - shared RSS/Atom downloader for the news service (2026-10-04, item 9 "News pillar looks dead").

Why this exists: both news/main.py (legacy path) and news/news_quality.py called ``feedparser.parse(url)``
directly. feedparser fetches with urllib, a "feedparser/x.y" User-Agent and NO timeout, which

  * is exactly what bot-gated feeds (Moneycontrol, Business Standard, ...) answer with an empty/HTML body -
    feedparser never raises on that, it just yields zero entries, so every source logged "Fetched 0 items"
    with nothing to say WHY (blocked / dead URL / genuinely no match);
  * can hang a worker thread forever on a stalled feed;
  * re-downloaded the same general "latest news" feeds once per symbol per request (a hot-picks scan of 25
    symbols = ~175 identical downloads).

This helper downloads with httpx (browser User-Agent - the same headers real-trade-service's after-hours scan
already uses successfully, 10 s timeout), logs a WARNING that names the HTTP status / "HTML instead of a feed" /
"200 but zero entries" so a blocked or retired source is visible at a glance, and caches the parsed result per
URL (success: NEWS_FEED_CACHE_TTL_SEC, default 300 s; Google News searches at most 120 s; a failed download is
remembered for 60 s so a blocked source is not hammered). Exceptions raised by feedparser.parse itself still
propagate to the caller exactly as before.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from types import SimpleNamespace
from typing import Any, Dict, Optional, Tuple

import feedparser
import httpx

logger = logging.getLogger("news_feed_fetch")

FEED_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
}


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """Blank / garbage / out-of-range env values fall back to the default (never crash on import)."""
    try:
        v = int(((os.getenv(name) or "").strip() or str(default)))
    except (TypeError, ValueError):
        return default
    return v if lo <= v <= hi else default


FEED_TIMEOUT_SEC = float(_env_int("NEWS_FEED_TIMEOUT_SEC", 10, 2, 30))
CACHE_TTL_SEC = _env_int("NEWS_FEED_CACHE_TTL_SEC", 300, 0, 3600)    # 0 disables caching
_SEARCH_TTL_CAP_SEC = 120
_FAIL_TTL_SEC = 60

_cache: Dict[str, Tuple[float, Any, Dict[str, Any]]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _ttl_for(url: str) -> int:
    if CACHE_TTL_SEC <= 0:
        return 0
    return min(CACHE_TTL_SEC, _SEARCH_TTL_CAP_SEC) if "news.google.com" in url else CACHE_TTL_SEC


def _empty_parsed() -> Any:
    return SimpleNamespace(entries=[], bozo=True)


def _download(url: str) -> Tuple[Optional[bytes], Dict[str, Any]]:
    """GET `url`. -> (body, info). body is None when the download failed or the body is clearly not a feed."""
    info: Dict[str, Any] = {"status": None, "error": None}
    try:
        with httpx.Client(timeout=FEED_TIMEOUT_SEC, follow_redirects=True, headers=FEED_HEADERS) as client:
            resp = client.get(url)
        info["status"] = resp.status_code
        if resp.status_code != 200:
            info["error"] = f"http_{resp.status_code}"
            logger.warning("news feed %s -> HTTP %s (blocked or retired?)", _short(url), resp.status_code)
            return None, info
        body = resp.content or b""
        ctype = (resp.headers.get("content-type") or "").lower()
        head = body[:200].lstrip().lower()
        if "text/html" in ctype or head.startswith(b"<!doctype html") or head.startswith(b"<html"):
            info["error"] = "html_instead_of_feed"
            logger.warning("news feed %s -> HTTP 200 but HTML, not a feed (bot page or retired URL): %r",
                           _short(url), body[:80])
            return None, info
        return body, info
    except Exception as exc:   # transport error / timeout: never crash a request over one source
        info["error"] = type(exc).__name__
        logger.warning("news feed %s download failed: %s", _short(url), type(exc).__name__)
        return None, info


def _short(url: str) -> str:
    return url if len(url) <= 90 else url[:87] + "..."


def fetch_feed_ex(url: str, source: str = "feed") -> Tuple[Any, Dict[str, Any]]:
    """-> (parsed, info). info: status / error / entries / cached. parsed always has an `.entries` list."""
    ttl = _ttl_for(url)
    now = time.monotonic()
    if ttl > 0:
        with _lock:
            hit = _cache.get(url)
        if hit and hit[0] > now:
            return hit[1], dict(hit[2], cached=True)

    raw, info = _download(url)
    if raw is None:
        parsed, keep = _empty_parsed(), min(ttl, _FAIL_TTL_SEC)
        info["entries"] = 0
    else:
        parsed = feedparser.parse(raw)          # exceptions propagate, as with the old direct call
        info["entries"] = len(getattr(parsed, "entries", None) or [])
        if info["entries"] == 0:
            info["error"] = "zero_entries"
            logger.warning("news feed %s (%s) -> HTTP 200 but 0 entries (dead URL or bot page)", source, _short(url))
        keep = ttl
    if keep > 0:
        with _lock:
            _cache[url] = (now + keep, parsed, dict(info))
            if len(_cache) > 400:               # bounded: drop expired, then oldest
                for k in [k for k, v in _cache.items() if v[0] <= now]:
                    _cache.pop(k, None)
                while len(_cache) > 400:
                    _cache.pop(next(iter(_cache)), None)
    return parsed, dict(info, cached=False)


def fetch_feed(url: str, source: str = "feed") -> Any:
    return fetch_feed_ex(url, source)[0]
