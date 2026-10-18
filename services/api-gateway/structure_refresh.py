"""group360 (WS-3 follow-up): post-close `structure_only` refresh of the data-feed rows' flat `ms_*` market-structure keys.

Why: the feed row's `ms_*` keys are only seeded while repairing a missing RSI (group 349). Rows therefore carry NO structure, or a structure
that is days old, and every consumer that reads the feed (Buy Sniper gate, PREPARE refresh notes, feed coverage) has "no opinion". This job
walks the feed after the daily bar has closed and re-reads ONLY the structure block from the technical service
(`GET {TECHNICAL_URL}/structure/{symbol}`) -- no fundamentals, no events, no quotes -- then replaces the `ms_*` group on the row.

Rules
  * Never raises into the caller; every failure is a counter, not an exception. Fail-open: a symbol that cannot be refreshed keeps its old keys.
  * Only EXISTING rows are updated (a symbol outside the feed is not added). Replace-as-a-group, not merge: an old demand zone must not
    survive next to a newer `ms_asof` (see DataFeedStore.replace_fields).
  * A reply with no valid `market_structure` block never wipes the old keys (counter `no_block`).
  * A reply with the same `ms_hash` / `ms_asof` as stored is not written (counter `unchanged`) -- no DB churn.
  * Selection is by need: rows with no structure first, then rows whose last structure bar is older than the last COMPLETED session, then
    (only with force) everything else. A stale row that was already tried within STRUCTURE_REFRESH_RECHECK_H hours is skipped.
  * STRUCTURE_REFRESH_ENABLED=0 turns the whole job off (the endpoint answers `disabled`).

The pure helpers (`expected_bar_date`, `needs_refresh`, `select_symbols`, `refresh_one`) take their collaborators as arguments so they can be
tested with no network, no clock and no feed store.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

import ms_passthrough

logger = logging.getLogger("structure-refresh")

IST = timezone(timedelta(hours=5, minutes=30))

REFRESHED_AT_KEY = "ms_refreshed_at"
# group424: a failed fetch (the technical service answers 503 "history thin or engine error" for a new listing) used to leave no trace, so
# the same symbol was re-selected on every catch-up pass for ever. A failure now stamps the attempt and a back-off (1 h, 2 h, 4 h ... 24 h)
# keeps the symbol out of the selection. A later attempt that gets an answer (a newer `ms_refreshed_at`), or `force`, lifts it.
FAILED_AT_KEY = "ms_failed_at"
FAIL_COUNT_KEY = "ms_fail_n"
FAIL_BACKOFF_BASE_H = 1.0
FAIL_BACKOFF_MAX_H = 24.0
FAIL_COUNT_CAP = 20
FAIL_KEYS: Tuple[str, ...] = (FAILED_AT_KEY, FAIL_COUNT_KEY)
SUMMARY_KEYS: Tuple[str, ...] = tuple(getattr(ms_passthrough, "SUMMARY_KEYS", ()) or ())
# Every key this job owns on a feed row (replaced as one group; a successful refresh therefore also clears the failure stamp).
OWNED_KEYS: Tuple[str, ...] = SUMMARY_KEYS + (REFRESHED_AT_KEY,) + FAIL_KEYS

# Outcomes (also the counter names).
REFRESHED, UNCHANGED, NO_BLOCK, FAILED, NOT_IN_FEED = "refreshed", "unchanged", "no_block", "failed", "not_in_feed"


# ----------------------------------------------------------------------------- config
def _env_float(name: str, default: float, lo: float = 0.0) -> float:
    try:
        return max(lo, float((os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        return max(lo, float(default))


def _env_int(name: str, default: int, lo: int = 0) -> int:
    try:
        return max(lo, int((os.getenv(name) or "").strip() or default))
    except (TypeError, ValueError):
        return max(lo, int(default))


def enabled() -> bool:
    return (os.getenv("STRUCTURE_REFRESH_ENABLED") or "1").strip().lower() not in ("0", "false", "no", "off")


def _technical_url() -> str:
    raw = (os.getenv("TECHNICAL_URL") or "").strip().rstrip("/")
    if raw:
        return raw
    ai = (os.getenv("ANALYSIS_INTELLIGENCE_URL") or "https://analysis-intelligence-service.onrender.com").strip().rstrip("/")
    return f"{ai}/technical"


def _close_buffer_min() -> int:
    # Same variable the technical service uses to decide when today's daily bar becomes usable.
    return _env_int("STRUCTURE_BAR_CLOSE_BUFFER_MIN", 20)


# ----------------------------------------------------------------------------- in-process catch-up (group378)
# The job used to run ONLY when something outside the process asked for it (QStash tick / GitHub workflow). A missed trigger, a missing
# API_GATEWAY_URL secret or a restart after 16:00 IST therefore left every feed row without `ms_*` keys, and the bulk scan (which reads
# structure from the feed row) then had "no opinion" for every symbol -> no observations logged, REAL-mode `structure_nodata` WAITs.
# The catch-up loop in main.py asks `catchup_wanted()` every STRUCTURE_REFRESH_CATCHUP_INTERVAL_S and, when the market is closed, starts the
# normal refresh. The normal selection still decides WHAT to refresh (missing / stale rows only, rows tried within STRUCTURE_REFRESH_RECHECK_H
# are skipped), so a catch-up that finds nothing to do is a cheap no-op. STRUCTURE_REFRESH_CATCHUP=0 turns just the catch-up off.
CATCHUP_PHASES: Tuple[str, ...] = ("closed", "holiday")


def catchup_enabled() -> bool:
    return enabled() and (os.getenv("STRUCTURE_REFRESH_CATCHUP") or "1").strip().lower() not in ("0", "false", "no", "off")


def catchup_interval_s() -> float:
    return _env_float("STRUCTURE_REFRESH_CATCHUP_INTERVAL_S", 1800.0, lo=60.0)


def catchup_initial_delay_s() -> float:
    return _env_float("STRUCTURE_REFRESH_CATCHUP_DELAY_S", 180.0, lo=0.0)


def catchup_wanted(phase: Optional[str], job_status: Optional[str]) -> bool:
    """True when a catch-up refresh should be attempted now: catch-up on, market phase `closed` / `holiday` (never during pre-open, the
    session or the 15:30-16:00 post window, so the technical service is not loaded while it serves the live market) and no run in flight.
    Pure."""
    if not catchup_enabled():
        return False
    if str(phase or "").lower() not in CATCHUP_PHASES:
        return False
    return str(job_status or "").lower() != "running"


# ----------------------------------------------------------------------------- pure helpers
def _is_holiday(d: date) -> bool:
    try:
        from nse_holidays import is_nse_holiday
        return bool(is_nse_holiday(d))
    except Exception:  # noqa: BLE001 - calendar unavailable: treat as open
        return False


def expected_bar_date(now: Optional[datetime] = None, is_holiday: Optional[Callable[[date], bool]] = None,
                      buffer_min: Optional[int] = None) -> date:
    """Date of the newest daily bar the technical service can have: today once 15:30 IST + buffer has passed on a trading day, else the
    previous trading day. Weekends and NSE holidays are skipped. Pure given its arguments."""
    now = now or datetime.now(IST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=IST)
    now = now.astimezone(IST)
    hol = is_holiday or _is_holiday
    buf = _close_buffer_min() if buffer_min is None else int(buffer_min)
    d = now.date()
    closed_today = (now.time() >= dtime(15, 30)) and now >= datetime.combine(d, dtime(15, 30), IST) + timedelta(minutes=buf)
    if not (closed_today and d.weekday() < 5 and not hol(d)):
        d = d - timedelta(days=1)
    for _ in range(14):  # bounded walk back over weekend / holiday runs
        if d.weekday() < 5 and not hol(d):
            break
        d -= timedelta(days=1)
    return d


def asof_date(asof: Any) -> Optional[date]:
    """Calendar date (IST) of an `ms_asof` value: ISO string (date or datetime) or epoch seconds / milliseconds. None when unreadable."""
    try:
        if asof is None or isinstance(asof, bool):
            return None
        if isinstance(asof, (int, float)):
            ts = float(asof)
            return datetime.fromtimestamp(ts / 1000.0 if ts > 1e11 else ts, tz=timezone.utc).astimezone(IST).date()
        txt = str(asof).strip()
        if not txt:
            return None
        if len(txt) == 10:
            return date.fromisoformat(txt)
        dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
        if dt.tzinfo is None:  # a naive bar timestamp is already an exchange-local one
            return dt.date()
        return dt.astimezone(IST).date()
    except Exception:  # noqa: BLE001
        return None


def row_state(row: Any, expected: date) -> str:
    """'missing' (no usable structure keys) | 'stale' (last bar older than `expected`) | 'fresh'."""
    if not isinstance(row, dict):
        return "missing"
    q = row.get("ms_quality")
    if q is None or q == "unavailable":
        return "missing"
    d = asof_date(row.get("ms_asof"))
    if d is None or d < expected:
        return "stale"
    return "fresh"


def _parse_ts(v: Any) -> Optional[datetime]:
    """ISO timestamp (naive = IST) -> aware datetime; None when empty or unreadable."""
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).strip().replace("Z", "+00:00"))
        return t if t.tzinfo is not None else t.replace(tzinfo=IST)
    except Exception:  # noqa: BLE001
        return None


def _live_fail_count(row: Any) -> int:
    """Consecutive-failure count recorded on a row; 0 when none, unreadable, or lifted by a newer answered attempt (`ms_refreshed_at`)."""
    if not isinstance(row, dict):
        return 0
    failed = _parse_ts(row.get(FAILED_AT_KEY))
    if failed is None:
        return 0
    refreshed = _parse_ts(row.get(REFRESHED_AT_KEY))
    if refreshed is not None and refreshed > failed:
        return 0
    try:
        return max(0, min(FAIL_COUNT_CAP, int(row.get(FAIL_COUNT_KEY) or 1)))
    except (TypeError, ValueError):
        return 1


def fail_backoff_hours(n: int) -> float:
    """Wait after the n-th consecutive failure: 1 h, 2 h, 4 h ... capped at 24 h (0 for n <= 0)."""
    if n <= 0:
        return 0.0
    return min(FAIL_BACKOFF_MAX_H, FAIL_BACKOFF_BASE_H * (2.0 ** (n - 1)))


def in_fail_backoff(row: Any, now: Optional[datetime] = None) -> bool:
    n = _live_fail_count(row)
    if n <= 0:
        return False
    failed = _parse_ts(row.get(FAILED_AT_KEY))
    if failed is None:
        return False
    elapsed = ((now or datetime.now(IST)) - failed).total_seconds()
    return 0 <= elapsed < fail_backoff_hours(n) * 3600


def needs_refresh(row: Any, expected: date, now: Optional[datetime] = None, force: bool = False,
                  recheck_hours: float = 6.0) -> Optional[int]:
    """Priority (0 = most urgent) or None when the row should be left alone.
    0 missing, 1 stale, 2 fresh (force only). A missing / stale row already tried within `recheck_hours` is skipped unless force."""
    state = row_state(row, expected)
    if state == "fresh" and not force:
        return None
    if not force and in_fail_backoff(row, now):  # group424: a symbol whose fetch keeps failing waits 1 h, 2 h, 4 h ... 24 h
        return None
    if not force and recheck_hours > 0 and isinstance(row, dict):
        t = _parse_ts(row.get(REFRESHED_AT_KEY))
        if t is not None and 0 <= ((now or datetime.now(IST)) - t).total_seconds() < recheck_hours * 3600:
            return None
    return {"missing": 0, "stale": 1, "fresh": 2}[state]


def select_symbols(rows: Dict[str, Any], expected: date, now: Optional[datetime] = None, force: bool = False,
                   limit: int = 0, recheck_hours: float = 6.0) -> Tuple[List[str], Dict[str, int]]:
    """(symbols to refresh, tally) -- most urgent first, then alphabetical (deterministic). `limit` <= 0 means no cap.
    tally = {considered, missing, stale, fresh, selected, skipped}."""
    tally = {"considered": 0, "missing": 0, "stale": 0, "fresh": 0, "selected": 0, "skipped": 0}
    ranked: List[Tuple[int, str]] = []
    for sym, row in (rows or {}).items():
        base = str(sym or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if not base or base.startswith("SYSTEM:"):
            continue
        tally["considered"] += 1
        tally[row_state(row, expected)] += 1
        pr = needs_refresh(row, expected, now=now, force=force, recheck_hours=recheck_hours)
        if pr is None:
            tally["skipped"] += 1
            continue
        ranked.append((pr, base))
    ranked.sort()
    syms = [s for _, s in ranked]
    if limit and limit > 0:
        syms = syms[:limit]
    tally["selected"] = len(syms)
    return syms, tally


def _record_failure(sym: str, store: Any, stamp: str) -> None:
    """Remember a failed fetch (stamp + consecutive count) and log the symbol. The old structure keys are kept. Never raises, never creates a row."""
    try:
        existing = store.get_symbol(sym)
        if not isinstance(existing, dict) or not existing:
            return
        n = min(FAIL_COUNT_CAP, _live_fail_count(existing) + 1)
        store.replace_fields(sym, {FAILED_AT_KEY: stamp, FAIL_COUNT_KEY: n})
        logger.warning("structure_refresh: fetch failed for %s (consecutive failures=%d, next try in >= %.0f h)", sym, n, fail_backoff_hours(n))
    except Exception as e:  # noqa: BLE001
        logger.debug("structure_refresh record failure %s: %s", sym, e)


def refresh_one(sym: str, fetch: Callable[[str], Optional[Dict[str, Any]]], store: Any, now: Optional[datetime] = None) -> str:
    """Refresh one symbol's `ms_*` group. Returns one of REFRESHED / UNCHANGED / NO_BLOCK / FAILED / NOT_IN_FEED. Never raises."""
    stamp = (now or datetime.now(IST)).isoformat()
    try:
        data = fetch(sym)
    except Exception as e:  # noqa: BLE001
        logger.debug("structure_refresh fetch %s: %s", sym, e)
        _record_failure(sym, store, stamp)
        return FAILED
    if not isinstance(data, dict):
        _record_failure(sym, store, stamp)
        return FAILED
    try:
        keys = ms_passthrough.feed_keys(data)  # {} when disabled / no valid block / unknown schema_version
        if not keys:
            # Remember the attempt (so a symbol whose structure keeps coming back empty is not retried all day) but keep the old keys.
            try:
                store.replace_fields(sym, {REFRESHED_AT_KEY: stamp})
            except Exception:  # noqa: BLE001
                pass
            return NO_BLOCK
        existing = store.get_symbol(sym)
        if not isinstance(existing, dict) or not existing:
            return NOT_IN_FEED
        same = (keys.get("ms_hash") is not None and keys.get("ms_hash") == existing.get("ms_hash")
                and keys.get("ms_asof") == existing.get("ms_asof") and keys.get("ms_quality") == existing.get("ms_quality"))
        if same:
            # touch only the attempt stamp (and clear a failure stamp left by earlier failed attempts, when there is one)
            drop = FAIL_KEYS if any(k in existing for k in FAIL_KEYS) else ()
            if not store.replace_fields(sym, {REFRESHED_AT_KEY: stamp}, drop_keys=drop):
                return FAILED
            return UNCHANGED
        fields = dict(keys)
        fields[REFRESHED_AT_KEY] = stamp
        if not store.replace_fields(sym, fields, drop_keys=OWNED_KEYS):
            return FAILED
        return REFRESHED
    except Exception as e:  # noqa: BLE001
        logger.debug("structure_refresh write %s: %s", sym, e)
        return FAILED


# ----------------------------------------------------------------------------- job state + counters
_LOCK = threading.Lock()
_STOP = threading.Event()
_COUNTER_KEYS = ("processed", "total", REFRESHED, UNCHANGED, NO_BLOCK, FAILED, NOT_IN_FEED)
_STALE_AFTER_SEC = 600.0


def _fresh_job() -> Dict[str, Any]:
    j: Dict[str, Any] = {"status": "idle", "message": "Idle", "kind": "structure_refresh", "force": False}
    j.update({k: 0 for k in _COUNTER_KEYS})
    return j


_JOB: Dict[str, Any] = _fresh_job()
# Lifetime counters of this process (survive across runs; reset on restart). Exposed through get_job()["lifetime"] and /metrics.
_LIFETIME: Dict[str, int] = {"runs": 0, "symbols": 0, REFRESHED: 0, UNCHANGED: 0, NO_BLOCK: 0, FAILED: 0, NOT_IN_FEED: 0}


def _set_job(**kw: Any) -> Dict[str, Any]:
    with _LOCK:
        _JOB.update(kw)
        _JOB["updated_at"] = datetime.now(IST).isoformat()
        _JOB["_updated_epoch"] = time.time()
        return dict(_JOB)


def get_job() -> Dict[str, Any]:
    """Snapshot of the job + lifetime counters. A 'running' job with no progress for STRUCTURE_REFRESH_STALE_AFTER_SEC is reported 'stalled'."""
    with _LOCK:
        job = dict(_JOB)
        life = dict(_LIFETIME)
    if job.get("status") == "running":
        last = job.get("_updated_epoch") or 0
        stale = _env_float("STRUCTURE_REFRESH_STALE_AFTER_SEC", _STALE_AFTER_SEC, lo=30.0)
        if last and (time.time() - last) > stale:
            job["status"] = "stalled"
            job["message"] = f"No progress for {int(time.time() - last)}s - the job likely died (dyno restart). Safe to start again."
    job.pop("_updated_epoch", None)
    job["lifetime"] = life
    job["enabled"] = enabled()
    return job


def is_running() -> bool:
    return get_job().get("status") == "running"


def request_stop() -> None:
    _STOP.set()


def reset_for_tests() -> None:
    global _JOB
    with _LOCK:
        _JOB = _fresh_job()
        for k in _LIFETIME:
            _LIFETIME[k] = 0
    _STOP.clear()


def _bump_metrics(outcome: str) -> None:
    try:
        from metrics import metrics
        metrics.inc("stockky_structure_refresh_total", outcome=outcome)
    except Exception:  # noqa: BLE001
        pass


# ----------------------------------------------------------------------------- network fetch (gateway)
def make_fetcher(client: Any) -> Callable[[str], Optional[Dict[str, Any]]]:
    """Fetch `{TECHNICAL_URL}/structure/{sym}` with the shared 'analysis' rate-limit bucket, as a background pipeline (it leaves the
    interactive reserve untouched). One short retry on 429 only. Returns the JSON dict or None."""
    url_base = _technical_url()
    timeout = _env_float("STRUCTURE_REFRESH_TIMEOUT_S", 20.0, lo=1.0)

    def _fetch(sym: str) -> Optional[Dict[str, Any]]:
        try:
            try:
                import rate_limiter
                rate_limiter.acquire("analysis", weight=1, pipeline="structure_refresh")
                to = rate_limiter.suggested_timeout(timeout, "analysis")
            except Exception:  # noqa: BLE001
                to = timeout
            r = client.get(f"{url_base}/structure/{sym}", timeout=to)
            if r.status_code == 429:
                time.sleep(5)
                r = client.get(f"{url_base}/structure/{sym}", timeout=to)
            if r.status_code == 200 and r.content:
                data = r.json()
                return data if isinstance(data, dict) else None
        except Exception as e:  # noqa: BLE001
            logger.debug("structure_refresh GET %s: %s", sym, e)
        return None

    return _fetch


# ----------------------------------------------------------------------------- runner
def plan(store: Any, force: bool = False, limit: Optional[int] = None, now: Optional[datetime] = None) -> Tuple[List[str], Dict[str, int], date]:
    """Resolve what a run would refresh: (symbols, tally, expected bar date). Reads every feed row once (bulk when the store offers it)."""
    now = now or datetime.now(IST)
    expected = expected_bar_date(now)
    try:
        symbols = list(store.list_symbols() or [])
    except Exception:  # noqa: BLE001
        symbols = []
    rows: Dict[str, Any] = {}
    try:
        got = store.get_symbols_bulk(symbols) if symbols else {}
        rows = dict(got) if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        rows = {}
    for s in symbols:  # bulk miss -> single reads
        if s not in rows:
            try:
                rows[s] = store.get_symbol(s) or {}
            except Exception:  # noqa: BLE001
                rows[s] = {}
    cap = _env_int("STRUCTURE_REFRESH_MAX_SYMBOLS", 0) if limit is None else max(0, int(limit))
    syms, tally = select_symbols(rows, expected, now=now, force=force, limit=cap,
                                 recheck_hours=_env_float("STRUCTURE_REFRESH_RECHECK_H", 6.0))
    return syms, tally, expected


def run(symbols: Iterable[str], store: Any, fetch: Callable[[str], Optional[Dict[str, Any]]],
        concurrency: Optional[int] = None, force: bool = False, tally: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Blocking worker (BackgroundTasks / CLI). Bounded concurrency, stop-aware, never raises. Returns the final job snapshot."""
    syms = [s for s in dict.fromkeys(str(x or "").upper().replace(".NS", "").replace(".BO", "").strip() for x in symbols) if s]
    total = len(syms)
    workers = max(1, _env_int("STRUCTURE_REFRESH_CONCURRENCY", 3) if concurrency is None else int(concurrency))
    _STOP.clear()
    counts = {REFRESHED: 0, UNCHANGED: 0, NO_BLOCK: 0, FAILED: 0, NOT_IN_FEED: 0}
    _set_job(status="running", message=f"Structure refresh: 0/{total}", processed=0, total=total, force=bool(force),
             started_at=datetime.now(IST).isoformat(), finished_at=None, selection=tally or {}, **counts)
    with _LOCK:
        _LIFETIME["runs"] += 1
    processed = 0
    stopped = False
    try:
        if total:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(refresh_one, s, fetch, store): s for s in syms}
                for fut in as_completed(futures):
                    try:
                        outcome = fut.result()
                    except Exception:  # noqa: BLE001
                        outcome = FAILED
                    counts[outcome] = counts.get(outcome, 0) + 1
                    processed += 1
                    _bump_metrics(outcome)
                    with _LOCK:
                        _LIFETIME["symbols"] += 1
                        _LIFETIME[outcome] = _LIFETIME.get(outcome, 0) + 1
                    if processed % 5 == 0 or processed == total:
                        _set_job(status="running", processed=processed,
                                 message=f"Structure refresh: {processed}/{total} (new={counts[REFRESHED]} same={counts[UNCHANGED]} "
                                         f"empty={counts[NO_BLOCK]} fail={counts[FAILED]})", **counts)
                    if _STOP.is_set():
                        stopped = True
                        for f in futures:
                            f.cancel()
                        break
    except Exception as e:  # noqa: BLE001
        logger.exception("structure_refresh run: %s", e)
        return _set_job(status="error", message=str(e)[:240], processed=processed, finished_at=datetime.now(IST).isoformat(), **counts)
    status = "stopped" if stopped else "done"
    msg = (f"{'Stopped at' if stopped else 'Done'} {processed}/{total}: new={counts[REFRESHED]} same={counts[UNCHANGED]} "
           f"empty={counts[NO_BLOCK]} fail={counts[FAILED]}")
    _set_job(status=status, message=msg, processed=processed, finished_at=datetime.now(IST).isoformat(), **counts)
    logger.info("structure_refresh %s", msg)
    return get_job()
