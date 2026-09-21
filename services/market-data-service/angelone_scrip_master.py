"""
services/market-data-service/angelone_scrip_master.py  §1 — symbol-to-token lookup.

AngelOne's REST and WebSocket APIs address every instrument by a numeric
`token` plus its exchange segment — never by trading symbol directly.
angelone_ws_feed.py cannot fetch a single quote without this mapping.
Mirrors the same problem execution/dhan_client.py::get_security_id()
already solves for Dhan, using AngelOne's own official, publicly
documented scrip master file (no auth required, refreshed daily):
https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json

VERIFY this URL still resolves in your environment before relying on it —
AngelOne has moved this file's path in the past. Override via
ANGELONE_SCRIP_MASTER_URL if it has changed.

2026-09-21 rewrite (post-redeploy incident): the previous ensure_loaded()
was `if not _token_map: _load_sync()` with the lock only guarding the final
assignment. After a restart the map is empty, so EVERY concurrent caller
(dozens of /quote + /history request threads, the WS feed thread, the
startup preload) each independently downloaded and json-parsed the whole
multi-tens-of-MB scrip master at the same instant. That produced the wall
of "AngelOne scrip master fetch failed: The read operation timed out"
lines, pinned the container's 0.55-CPU quota, and — because every parse
transiently holds the entire file as Python objects — is the most plausible
reason the container blew through its 1200 MB limit and was restarted.
Now:
  * single-flight     exactly one thread fetches; everyone else waits a short,
                      bounded time (or, if a map already exists, keeps using it)
  * failure backoff   a failed fetch is not retried until a backoff (30s,
                      doubling to 10min) has elapsed — no retry storms
  * streaming parse   the file is parsed object-by-object off the network
                      stream and non-NSE-EQ rows are dropped immediately, so
                      peak memory is ~one chunk, not the whole file
  * stale-while-revalidate  a day-old map keeps serving while ONE background
                      thread refreshes it; a request thread never blocks on it
  * disk snapshot     the resulting ~2.7k-entry map is saved to disk so an
                      in-place container restart (restart: unless-stopped)
                      warms instantly instead of re-downloading under load
"""
from __future__ import annotations
import json
import logging
import os
import tempfile
import threading
import time
from typing import Dict, Iterable, Iterator, List, Optional

import httpx

logger = logging.getLogger("angelone-scrip-master")

SCRIP_MASTER_URL = os.getenv(
    "ANGELONE_SCRIP_MASTER_URL",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
)
# The file itself is only republished once a day — no point re-fetching more often.
REFRESH_INTERVAL_S = float(os.getenv("ANGELONE_SCRIP_MASTER_REFRESH_S", str(24 * 3600)))

# How long a request thread that finds NO map at all (cold start) will wait for
# the single in-flight fetch before giving up and letting its caller fall back
# to its non-AngelOne path. Deliberately short: these are request threads.
DEFAULT_WAIT_S = float(os.getenv("ANGELONE_SCRIP_MASTER_WAIT_S", "3"))
# Failure backoff: first retry after BASE seconds, doubling up to MAX.
_FAIL_BACKOFF_BASE_S = float(os.getenv("ANGELONE_SCRIP_MASTER_FAIL_BACKOFF_S", "30"))
_FAIL_BACKOFF_MAX_S = float(os.getenv("ANGELONE_SCRIP_MASTER_FAIL_BACKOFF_MAX_S", "600"))
# Network: per-read timeout (httpx applies it between chunks, not to the whole
# download) plus a hard wall-clock cap on the entire download.
_READ_TIMEOUT_S = float(os.getenv("ANGELONE_SCRIP_MASTER_READ_TIMEOUT_S", "30"))
_MAX_DOWNLOAD_S = float(os.getenv("ANGELONE_SCRIP_MASTER_MAX_DOWNLOAD_S", "180"))
# Disk snapshot of the parsed map (tiny). A snapshot up to CACHE_MAX_AGE old is
# accepted for a warm start; anything older than REFRESH_INTERVAL_S is then
# refreshed in the background right away.
_CACHE_PATH = os.getenv("ANGELONE_SCRIP_MASTER_CACHE_PATH", "/tmp/angelone_scrip_master_cache.json")
_CACHE_MAX_AGE_S = float(os.getenv("ANGELONE_SCRIP_MASTER_CACHE_MAX_AGE_S", str(7 * 24 * 3600)))
# Streaming-parse safety valve: if this much data accumulates without a single
# complete JSON object being decodable, the payload is malformed — bail out.
_MAX_PENDING_CHARS = 8 * 1024 * 1024

_lock = threading.Lock()        # guards the map swap + failure bookkeeping (held for microseconds)
_load_lock = threading.Lock()   # single-flight: held for the whole duration of a network fetch
_token_map: Dict[str, str] = {}   # e.g. "SBIN" -> "3045"
_loaded_at: float = 0.0
_fail_count: int = 0
_next_retry_at: float = 0.0
_disk_tried: bool = False


def _clean(symbol: str) -> str:
    return (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


# ── Streaming JSON-array parser ──────────────────────────────────────────────
def _iter_json_array(chunks: Iterable[str]) -> Iterator[dict]:
    """Yield the elements of a top-level JSON array one at a time from an
    iterable of text chunks, without ever materialising the whole array.
    Uses json.JSONDecoder.raw_decode (C-accelerated) per element. An element
    that straddles a chunk boundary simply fails to decode until the next
    chunk arrives, then decodes."""
    dec = json.JSONDecoder()
    buf = ""
    pos = 0
    in_array = False
    first = True
    for chunk in chunks:
        if not chunk:
            continue
        buf = buf[pos:] + chunk
        pos = 0
        if first:
            buf = buf.lstrip("\ufeff")   # tolerate a UTF-8 BOM
            first = False
        while True:
            n = len(buf)
            while pos < n and buf[pos] in " \t\r\n,":
                pos += 1
            if pos >= n:
                break
            if not in_array:
                if buf[pos] != "[":
                    raise ValueError("scrip master payload is not a JSON array")
                in_array = True
                pos += 1
                continue
            if buf[pos] == "]":
                return
            try:
                obj, end = dec.raw_decode(buf, pos)
            except json.JSONDecodeError:
                # Incomplete element at the end of the buffer — need more data.
                if n - pos > _MAX_PENDING_CHARS:
                    raise ValueError("scrip master payload: undecodable element (malformed JSON)")
                break
            yield obj
            pos = end


def _rows_to_map(rows: Iterable[dict]) -> Dict[str, str]:
    """NSE cash-equity rows are suffixed "-EQ" (e.g. "SBIN-EQ") — strip it to
    match the plain symbols used everywhere else in this codebase
    (symbol_master, candidate_engine, etc)."""
    new_map: Dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym_field = str(row.get("symbol", ""))
        if row.get("exch_seg") == "NSE" and sym_field.endswith("-EQ") and row.get("token"):
            new_map[sym_field[:-3].upper()] = str(row["token"])
    return new_map


def _fetch_map() -> Dict[str, str]:
    """Download + stream-parse the scrip master. Raises on any failure."""
    deadline = time.monotonic() + _MAX_DOWNLOAD_S
    timeout = httpx.Timeout(connect=10.0, read=_READ_TIMEOUT_S, write=10.0, pool=10.0)

    def _text_chunks(resp: httpx.Response) -> Iterator[str]:
        for text in resp.iter_text():
            if time.monotonic() > deadline:
                raise TimeoutError(f"scrip master download exceeded {_MAX_DOWNLOAD_S:.0f}s wall-clock cap")
            yield text

    with httpx.stream("GET", SCRIP_MASTER_URL, timeout=timeout) as resp:
        resp.raise_for_status()
        return _rows_to_map(_iter_json_array(_text_chunks(resp)))


# ── Disk snapshot ────────────────────────────────────────────────────────────
def _save_disk(new_map: Dict[str, str]) -> None:
    try:
        d = os.path.dirname(_CACHE_PATH) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".scrip_master_", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"saved_at": time.time(), "map": new_map}, f)
            os.replace(tmp, _CACHE_PATH)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:  # never let a cache-write problem affect the live map
        logger.warning("scrip master disk snapshot not saved (non-fatal): %s", e)


def _warm_from_disk() -> None:
    """One-shot per process: seed the in-memory map from the disk snapshot if
    a recent enough one exists. Cheap (tens of KB) so it runs inline."""
    global _token_map, _loaded_at, _disk_tried
    with _lock:
        if _disk_tried:
            return
        _disk_tried = True
        if _token_map:
            return
    try:
        with open(_CACHE_PATH, "r") as f:
            blob = json.load(f)
        saved_at = float(blob.get("saved_at") or 0.0)
        snap = blob.get("map") or {}
        age = time.time() - saved_at
        if not snap or not isinstance(snap, dict) or age > _CACHE_MAX_AGE_S or saved_at <= 0:
            return
        snap = {str(k): str(v) for k, v in snap.items()}
        with _lock:
            if not _token_map:
                _token_map = snap
                _loaded_at = saved_at
        logger.info(
            "AngelOne scrip master warm-started from disk snapshot: %d NSE-EQ symbols (%.1fh old)",
            len(snap), age / 3600.0,
        )
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("scrip master disk snapshot unreadable (ignored): %s", e)


# ── Load orchestration ───────────────────────────────────────────────────────
def _is_stale() -> bool:
    return (not _token_map) or (time.time() - _loaded_at) > REFRESH_INTERVAL_S


def _record_failure(reason: str) -> None:
    global _fail_count, _next_retry_at
    with _lock:
        _fail_count += 1
        backoff = min(_FAIL_BACKOFF_MAX_S, _FAIL_BACKOFF_BASE_S * (2 ** (_fail_count - 1)))
        _next_retry_at = time.time() + backoff
        n = _fail_count
    logger.error(
        "AngelOne scrip master fetch failed (attempt #%d): %s — not retrying for %.0fs",
        n, reason, backoff,
    )


def _refresh_locked(force: bool = False) -> None:
    """Fetch + swap. Caller MUST hold _load_lock. Re-checks staleness/backoff
    first because another thread may have completed a fetch while we waited."""
    global _token_map, _loaded_at, _fail_count, _next_retry_at
    if not force and (not _is_stale() or time.time() < _next_retry_at):
        return
    try:
        new_map = _fetch_map()
    except Exception as e:
        # httpx timeouts often stringify to "" — always include the class name.
        _record_failure(f"{type(e).__name__}: {e}")
        return
    if not new_map:
        _record_failure(
            "0 usable NSE-EQ rows — check ANGELONE_SCRIP_MASTER_URL / the file's schema hasn't changed"
        )
        return
    with _lock:
        _token_map = new_map      # atomic reference swap; readers never see a partial map
        _loaded_at = time.time()
        _fail_count = 0
        _next_retry_at = 0.0
    logger.info("AngelOne scrip master loaded: %d NSE-EQ symbols", len(new_map))
    _save_disk(new_map)


def _bg_refresh_and_release() -> None:
    try:
        _refresh_locked()
    finally:
        _load_lock.release()


def _load_sync() -> None:
    """Forced, blocking, single-flight refresh (ignores staleness and backoff).
    Kept for callers/tools that want an explicit reload."""
    with _load_lock:
        _refresh_locked(force=True)


def ensure_loaded(wait_s: Optional[float] = None) -> None:
    """Make sure a usable map exists, without ever letting a burst of callers
    stampede the download.

      * fresh map           → return immediately (the hot path, no locks)
      * stale map           → keep serving it; kick off ONE background refresh
      * no map (cold start) → exactly one caller fetches; the others wait up to
                              `wait_s` (default ANGELONE_SCRIP_MASTER_WAIT_S)
                              for it, then return with the map still empty so
                              their caller falls back exactly as it does today
      * recent failure      → return immediately until the backoff elapses

    A failed refresh keeps serving the last good map rather than clearing it.
    """
    if not _token_map and not _disk_tried:
        _warm_from_disk()
    if not _is_stale():
        return
    if time.time() < _next_retry_at:
        return
    if _token_map:
        # stale-while-revalidate: never block a request thread on a refresh
        if _load_lock.acquire(blocking=False):
            try:
                threading.Thread(
                    target=_bg_refresh_and_release, name="scrip-master-refresh", daemon=True
                ).start()
            except Exception:
                _load_lock.release()
        return
    # Cold: nothing to serve. Single-flight; waiters get a bounded wait.
    timeout = DEFAULT_WAIT_S if wait_s is None else wait_s
    if not _load_lock.acquire(timeout=max(0.0, timeout)):
        return
    try:
        _refresh_locked()
    finally:
        _load_lock.release()


def get_token(symbol: str) -> Optional[str]:
    ensure_loaded()
    return _token_map.get(_clean(symbol))


def get_tokens_bulk(symbols: List[str], wait_s: Optional[float] = None) -> Dict[str, str]:
    """Returns {clean_symbol: token} — only for symbols actually resolved.
    Silently drops anything not found; callers should log the gap between
    requested and resolved counts if they need visibility into misses.
    `wait_s`: how long to wait for a cold-start load (long-lived background
    callers like the WS feed pass a generous value; request threads use the
    short default)."""
    ensure_loaded(wait_s=wait_s)
    tmap = _token_map
    out: Dict[str, str] = {}
    for s in symbols:
        clean = _clean(s)
        tok = tmap.get(clean)
        if tok:
            out[clean] = tok
    return out


def get_all_symbols() -> Dict[str, str]:
    """Returns the FULL {clean_symbol: token} map for every NSE-EQ symbol
    in the scrip master (~2000 symbols) — not scoped to any pre-selected
    seed/watchlist. Added 2026-09-04 to support a whole-market AngelOne
    LTP sweep (see market-data-service main.py's /angelone/movers), which
    needs every symbol's token to compute day_change_pct itself instead of
    depending on NSE's (blockable) gainers/losers boards or a sampled
    yfinance seed."""
    ensure_loaded()
    return dict(_token_map)


def status() -> dict:
    now = time.time()
    return {
        "loaded_symbols": len(_token_map),
        "loaded_at": _loaded_at or None,
        "age_seconds": (now - _loaded_at) if _loaded_at else None,
        "source_url": SCRIP_MASTER_URL,
        "loading": _load_lock.locked(),
        "consecutive_failures": _fail_count,
        "next_retry_in_s": max(0.0, _next_retry_at - now) if _next_retry_at else 0.0,
    }
