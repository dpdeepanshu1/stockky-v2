"""
feed/scrip_master.py — AngelOne symbol→token map for position-stocks-service.

Duplicated from market-data-service/angelone_scrip_master.py (same
isolation rationale — see config.py docstring). Provides both:
  - get_token(symbol) → AngelOne numeric token (for WS subscribe/data)
  - get_all_nse_eq() → full {symbol: token} dict (for building the
    subscription list at WS startup)

No auth required — AngelOne's scrip master JSON is a public file.

2026-09-21: ported the market-data-service rewrite (see that file's
docstring for the full incident write-up) — single-flight fetch, failure
backoff, streaming parse (the old resp.json() transiently held the whole
multi-tens-of-MB file as Python objects, which matters most here: this
service's container limit is only 400 MB), stale-while-revalidate, and a
disk snapshot. Keep the two copies in sync.
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

logger = logging.getLogger("position-stocks-scrip-master")

SCRIP_MASTER_URL = os.getenv(
    "ANGELONE_SCRIP_MASTER_URL",
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json",
)
# The file itself is only republished once a day — no point re-fetching more often.
REFRESH_INTERVAL_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_REFRESH_S") or "").strip() or str(24 * 3600)))

# How long a request thread that finds NO map at all (cold start) will wait for
# the single in-flight fetch before giving up and letting its caller fall back
# to its non-AngelOne path. Deliberately short: these are request threads.
DEFAULT_WAIT_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_WAIT_S") or "").strip() or "3"))
# Failure backoff: first retry after BASE seconds, doubling up to MAX.
_FAIL_BACKOFF_BASE_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_FAIL_BACKOFF_S") or "").strip() or "30"))
_FAIL_BACKOFF_MAX_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_FAIL_BACKOFF_MAX_S") or "").strip() or "600"))
# Network: per-read timeout (httpx applies it between chunks, not to the whole
# download) plus a hard wall-clock cap on the entire download.
_READ_TIMEOUT_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_READ_TIMEOUT_S") or "").strip() or "30"))
_MAX_DOWNLOAD_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_MAX_DOWNLOAD_S") or "").strip() or "180"))
# Disk snapshot of the parsed map (tiny). A snapshot up to CACHE_MAX_AGE old is
# accepted for a warm start; anything older than REFRESH_INTERVAL_S is then
# refreshed in the background right away.
_CACHE_PATH = os.getenv("ANGELONE_SCRIP_MASTER_CACHE_PATH", "/tmp/position_stocks_scrip_master_cache.json")
_CACHE_MAX_AGE_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_CACHE_MAX_AGE_S") or "").strip() or str(7 * 24 * 3600)))
# Streaming-parse safety valve: if this much data accumulates without a single
# complete JSON object being decodable, the payload is malformed — bail out.
_MAX_PENDING_CHARS = 8 * 1024 * 1024
# 2026-10-21: extra URLs tried in order when the primary download fails (comma-separated; empty by default - set it
# only to a mirror you have verified serves the same file). Each URL gets its own _MAX_DOWNLOAD_S.
_FALLBACK_URLS = [u.strip() for u in (os.getenv("ANGELONE_SCRIP_MASTER_FALLBACK_URLS") or "").split(",") if u.strip()]
# 2026-10-21: after ONE default-wait caller has waited out DEFAULT_WAIT_S on the in-flight cold download, other
# default-wait callers return at once for this long instead of each queueing their own DEFAULT_WAIT_S behind the lock
# (a loop of N get_token() calls - /internal/feed-coverage takes up to 200 - used to stack N x 3 s on request threads,
# which is what timed real-trade's /quotes/bulk and /quote calls out while the first download was in flight).
# Callers that pass an explicit wait_s (the WS feeds, the movers sweep) are never short-circuited.
_COLD_NOWAIT_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_COLD_NOWAIT_S") or "").strip() or "15"))

# group488: after a container (re)start with a stored snapshot, the first background refresh of a STALE snapshot waits this
# long (plus up to 50 % random jitter) counted from process start, so it does not hit AngelOne's file server at the same
# moment as market-data-service, which fetches its own copy right at boot (default 0 there). The stale map keeps being served
# meanwhile. A cold start with no map is never delayed. 0 switches it off.
try:
    _BG_START_DELAY_S = float(((os.getenv("ANGELONE_SCRIP_MASTER_BG_START_DELAY_S") or "").strip() or "30"))
except ValueError:
    _BG_START_DELAY_S = 30.0
if not (_BG_START_DELAY_S == _BG_START_DELAY_S and 0.0 <= _BG_START_DELAY_S <= 600.0):
    _BG_START_DELAY_S = 30.0
_PROC_START = time.time()
_bg_first_done = False

_lock = threading.Lock()        # guards the map swap + failure bookkeeping (held for microseconds)
_load_lock = threading.Lock()   # single-flight: held for the whole duration of a network fetch
_token_map: Dict[str, str] = {}   # e.g. "SBIN" -> "3045"
_loaded_at: float = 0.0
_fail_count: int = 0
_next_retry_at: float = 0.0
_disk_tried: bool = False
_no_wait_until: float = 0.0   # wall-clock time until which default-wait cold callers skip the lock wait


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
    if in_array:
        # The chunks ran out before the closing "]": a truncated download (AngelOne has served this file with HTTP 200 and a
        # cut-off body). Never accept a partial map as if it were the whole file - the caller records a failure and retries.
        raise ValueError("scrip master payload ended before the closing ']' (truncated download)")


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


def _fetch_map_from(url: str) -> Dict[str, str]:
    """Download + stream-parse ONE url. Raises on any failure."""
    deadline = time.monotonic() + _MAX_DOWNLOAD_S
    timeout = httpx.Timeout(connect=10.0, read=_READ_TIMEOUT_S, write=10.0, pool=10.0)

    def _text_chunks(resp: httpx.Response) -> Iterator[str]:
        for text in resp.iter_text():
            if time.monotonic() > deadline:
                raise TimeoutError(f"scrip master download exceeded {_MAX_DOWNLOAD_S:.0f}s wall-clock cap")
            yield text

    with httpx.stream("GET", url, timeout=timeout) as resp:
        resp.raise_for_status()
        return _rows_to_map(_iter_json_array(_text_chunks(resp)))


def _fetch_map() -> Dict[str, str]:
    """Download the scrip master from SCRIP_MASTER_URL, then each ANGELONE_SCRIP_MASTER_FALLBACK_URLS entry if that fails.
    Raises the last failure when every URL fails (earlier failures are logged)."""
    urls = [SCRIP_MASTER_URL] + [u for u in _FALLBACK_URLS if u != SCRIP_MASTER_URL]
    last_exc: Optional[BaseException] = None
    for i, url in enumerate(urls):
        try:
            return _fetch_map_from(url)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if i < len(urls) - 1:
                logger.warning("scrip master download from %s failed (%s: %s) - trying the next URL",
                               url, type(e).__name__, e)
    assert last_exc is not None
    raise last_exc


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
            "position-stocks: scrip master warm-started from disk snapshot: %d NSE-EQ symbols (%.1fh old)",
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
        "position-stocks: scrip master fetch failed (attempt #%d): %s — not retrying for %.0fs",
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
    logger.info("position-stocks: scrip master loaded: %d NSE-EQ symbols", len(new_map))
    _save_disk(new_map)


def _bg_start_wait_s() -> float:
    """group488: seconds the first background refresh of this process still has to wait (0 when none / already done)."""
    global _bg_first_done
    if _bg_first_done or _BG_START_DELAY_S <= 0:
        return 0.0
    _bg_first_done = True
    import random
    due = _PROC_START + _BG_START_DELAY_S * (1.0 + random.uniform(0.0, 0.5))
    return max(0.0, due - time.time())


def _bg_refresh_and_release() -> None:
    try:
        wait = _bg_start_wait_s()
        if wait > 0:
            logger.info("position-stocks: scrip master snapshot is stale - first refresh in %.0fs (staggered after boot)", wait)
            time.sleep(wait)
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
    global _no_wait_until
    default_wait = wait_s is None
    if default_wait and time.time() < _no_wait_until:
        return          # a caller already waited out the in-flight download; don't make every request thread repeat it
    timeout = DEFAULT_WAIT_S if default_wait else wait_s
    if not _load_lock.acquire(timeout=max(0.0, timeout)):
        if default_wait:
            _no_wait_until = time.time() + _COLD_NOWAIT_S
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


def get_all_nse_eq() -> Dict[str, str]:
    """Full {clean_symbol: token} map — used to build the WS subscription
    list at startup when SCAN_UNIVERSE_SOURCE=all_nse_eq."""
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
