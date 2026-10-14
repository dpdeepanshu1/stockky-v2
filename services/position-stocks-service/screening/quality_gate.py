"""
screening/quality_gate.py — fast, best-effort fundamental/technical/news
pre-check applied to the top few price/volume candidates before entry.

WHY THIS EXISTS: the 1m/5m/15m/60m screener (screening/engine.py) only looks
at price and tick activity — it has no idea if a stock is moving because of
a genuine positive catalyst (good results, a bulk deal, positive news) versus
a fundamentally weak/illiquid name having a random spike. This module adds
that check — but ONLY for the handful of symbols the price/volume screen
already ranked highest (config.QUALITY_GATE_TOP_N), never the whole scan
universe, so it stays fast.

DESIGN — mirrors real-trade-service's candidate_engine/candidates.py
quality-gate pattern exactly, reusing the SAME shared analysis-intelligence-
service endpoints (a shared, read-only backend — not real-trade-service's
own state, so calling it doesn't compromise this service's isolation from
real-trade-service specifically):
  - GET {FUNDAMENTAL_URL}/analyze/{symbol}  -> fundamental_score, market_cap
  - GET {TECHNICAL_URL}/analyze/{symbol}    -> technical_score
  - GET {EVENT_URL}/events/{symbol}/categorized -> has_positive_catalyst,
    recent_event_score, bulk_deals (news/bulk-deal/earnings-surprise signal)

FAIL-OPEN, ALWAYS: every call has a short timeout (config.QUALITY_GATE_
TIMEOUT_S — a couple seconds, not real-trade-service's 12-60s, because this
loop ticks every 10s and anything slower isn't "quick"). Any exception,
timeout, or non-200 response leaves that field as None. A None field is
"unknown", never treated as a reject — same leniency real-trade-service's
own quality gate uses ("missing fresh fundamentals is common and shouldn't
reject an otherwise fine breakout on its own"). Only a field that IS present
and clearly below its floor causes a skip-this-one-try-next-candidate
outcome. If analysis-intelligence-service is down entirely, every check
comes back all-None and every candidate simply passes through unfiltered —
the scalp loop is never blocked or slowed by this being unavailable.

CACHE FALLBACK (session68): the paragraph above is still true for a symbol
never seen before. For a symbol this service HAS successfully scored
before, check()'s optional `cached` argument (see get_cache_batch/
upsert_cache_batch, models.ScalpQualityCache) fills in a timed-out field
with the last real value instead of leaving it None — closing the exact
gap that let MANGALAM/GEEKAYWIRE/JISLJALEQS through on 2026-09-17. Live
data always wins over cache when present; a symbol still gets one fully
fail-open pass the first time it's ever scored.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time as _time
from urllib.parse import quote
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from sqlalchemy.orm import Session

import config
from models import ScalpQualityCache
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-quality-gate")


@dataclass
class QualitySignal:
    symbol: str
    fundamental_score: Optional[float] = None
    technical_score: Optional[float] = None
    market_cap_cr: Optional[float] = None
    has_positive_catalyst: Optional[bool] = None
    recent_event_score: Optional[float] = None
    bulk_deal_flag: bool = False
    # group329: ISO date (YYYY-MM-DD) of the stock's next / current results announcement when the event service reports one.
    earnings_date: Optional[str] = None
    # group330: a "NEWS_NEGATIVE:..." reason from the same event-service answer (low event score / regulatory action / delisting risk / fresh miss).
    negative_news: Optional[str] = None
    # group344: the event service did not answer (twice) and nothing is remembered for this symbol - no news / earnings check was possible.
    event_unchecked: bool = False

    def passes(self) -> tuple[bool, str]:
        """Lenient pass/fail: only reject on data that IS present and below
        floor. Missing data always passes through with a reason noting so."""
        # group329: a stock reporting results TODAY can gap 5-10 % through a stop either way - not a scalp setup. Unknown date passes.
        if config.EARNINGS_DAY_BLOCK and self.earnings_date and self.earnings_date == ist_today_str():
            return False, f"EARNINGS_TODAY: results dated {self.earnings_date} (today) - gap risk a stop cannot cover"
        if config.NEWS_NEGATIVE_BLOCK_ENABLED and self.negative_news:
            return False, self.negative_news
        if self.event_unchecked and getattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", True):
            return False, "EVENT_CHECK_UNAVAILABLE: the news / earnings service did not answer - not entering without a news check (retried next cycle)"
        if self.fundamental_score is not None and self.fundamental_score < config.MIN_FUNDAMENTAL_SCORE:
            return False, f"fundamental_score {self.fundamental_score:.0f} < floor {config.MIN_FUNDAMENTAL_SCORE:.0f}"
        if self.technical_score is not None and self.technical_score < config.MIN_TECHNICAL_SCORE:
            return False, f"technical_score {self.technical_score:.0f} < floor {config.MIN_TECHNICAL_SCORE:.0f}"
        if self.market_cap_cr is not None and self.market_cap_cr < config.MIN_MARKET_CAP_CR:
            return False, f"market_cap ₹{self.market_cap_cr:.0f}cr < floor ₹{config.MIN_MARKET_CAP_CR:.0f}cr"
        reasons = []
        if self.has_positive_catalyst:
            reasons.append("positive catalyst")
        if self.bulk_deal_flag:
            reasons.append("bulk deal")
        if self.fundamental_score is None and self.technical_score is None and self.market_cap_cr is None:
            reasons.append("no fund/tech data available — floor check skipped")
        return True, ", ".join(reasons) if reasons else "no red flags"


def get_cache_batch(db: Session, symbols: list[str]) -> dict[str, QualitySignal]:
    """session68: read last-known-good quality scores for these symbols,
    skipping any row older than config.QUALITY_CACHE_MAX_AGE_HOURS. Called
    ONCE, synchronously, from main.py BEFORE the async check() gather —
    deliberately never called from inside a check() coroutine itself, so
    no DB session is touched while multiple check() calls are interleaved
    on the event loop."""
    if not symbols:
        return {}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=config.QUALITY_CACHE_MAX_AGE_HOURS)
    rows = (
        db.query(ScalpQualityCache)
        .filter(ScalpQualityCache.symbol.in_(symbols))
        .filter(ScalpQualityCache.updated_at >= cutoff)
        .all()
    )
    return {
        r.symbol: QualitySignal(
            symbol=r.symbol,
            fundamental_score=r.fundamental_score,
            technical_score=r.technical_score,
            market_cap_cr=r.market_cap_cr,
        )
        for r in rows
    }


def upsert_cache_batch(db: Session, signals: list[QualitySignal]) -> None:
    """session68: persist whichever fields were actually fetched live this
    cycle (not None) so a future timeout for the same symbol has real data
    to fall back to. Called ONCE, synchronously, AFTER the gather completes
    — same reasoning as get_cache_batch above. Never overwrites an existing
    cached field with None; a symbol with nothing live this cycle is
    skipped entirely (stale cache stays as-is until it naturally ages out
    via QUALITY_CACHE_MAX_AGE_HOURS)."""
    for sig in signals:
        if sig.fundamental_score is None and sig.technical_score is None and sig.market_cap_cr is None:
            continue
        row = db.query(ScalpQualityCache).filter_by(symbol=sig.symbol).one_or_none()
        if row is None:
            row = ScalpQualityCache(symbol=sig.symbol)
            db.add(row)
        if sig.fundamental_score is not None:
            row.fundamental_score = sig.fundamental_score
        if sig.technical_score is not None:
            row.technical_score = sig.technical_score
        if sig.market_cap_cr is not None:
            row.market_cap_cr = sig.market_cap_cr
        row.updated_at = datetime.now(timezone.utc)
    db.commit()


async def _fetch_fundamental(client: httpx.AsyncClient, symbol: str, timeout: Optional[float] = None) -> tuple[Optional[float], Optional[float]]:
    fund_score = market_cap_cr = None
    try:
        r = await client.get(f"{config.FUNDAMENTAL_URL}/analyze/{quote(symbol, safe='&')}",
                             timeout=config.QUALITY_GATE_TIMEOUT_S if timeout is None else timeout)
        if r.status_code == 200:
            fj = r.json()
            fund_score = fj.get("fundamental_score")
            raw_mcap = fj.get("market_cap") or (fj.get("raw") or {}).get("market_cap")
            if raw_mcap:
                try:
                    market_cap_cr = float(raw_mcap) / 1e7  # rupees -> crore
                except (TypeError, ValueError):
                    market_cap_cr = None
    except Exception as e:
        # BUG FIX (2026-09-17): httpx timeout exceptions (ReadTimeout,
        # ConnectTimeout, etc.) stringify to "" — logging bare `e` produced
        # "fundamental fetch failed for X ()" with zero information about
        # WHY it failed (timeout vs connection refused vs something else).
        # Fall back to the exception's class name whenever str(e) is empty.
        logger.info("quality_gate: fundamental fetch failed for %s (%s)", symbol, str(e) or type(e).__name__)
    return fund_score, market_cap_cr


async def _fetch_technical(client: httpx.AsyncClient, symbol: str, timeout: Optional[float] = None) -> Optional[float]:
    tech_score = None
    try:
        r = await client.get(f"{config.TECHNICAL_URL}/analyze/{quote(symbol, safe='&')}",
                             timeout=config.QUALITY_GATE_TIMEOUT_S if timeout is None else timeout)
        if r.status_code == 200:
            tech_score = r.json().get("technical_score")
    except Exception as e:
        # See the matching BUG FIX comment in _fetch_fundamental above.
        logger.info("quality_gate: technical fetch failed for %s (%s)", symbol, str(e) or type(e).__name__)
    return tech_score


async def _fetch_fund_tech(client: httpx.AsyncClient, symbol: str, timeout: Optional[float] = None) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """Runs the fundamental and technical calls concurrently, not sequentially.
    FIXED session 29: these were two independent `await`s back to back inside
    one function — real latency was fund_time + tech_time, not max(fund_time,
    tech_time), even though the module docstring and `check()`'s docstring both
    describe all three checks as running "concurrently". Combined with
    `_fetch_event_signal()` running alongside via the outer `asyncio.gather()`
    in `check()`, the true per-candidate wait was max(fund_time + tech_time,
    event_time) — silently up to ~2x QUALITY_GATE_TIMEOUT_S longer than
    intended on a timeout, directly compounding the scan-time-LTP entry-price
    staleness already tracked from session 25/26 (more wall-clock time between
    the screener's price snapshot and the real fill)."""
    (fund_score, market_cap_cr), tech_score = await asyncio.gather(
        _fetch_fundamental(client, symbol, timeout),
        _fetch_technical(client, symbol, timeout),
    )
    return fund_score, tech_score, market_cap_cr


# group329: symbol -> earnings date seen by the most recent _fetch_event_signal() call, consumed once by check().
_earnings_seen: dict = {}


# group330: symbol -> negative-news reason seen by the most recent _fetch_event_signal() call, consumed once by check().
_news_seen: dict = {}

# group344: monotonic time of the first event failure of the current unbroken run (None = the last event call worked).
_event_fail_since: list = [None]
# group341: symbols whose event call failed in the most recent check() (timeout / non-200), consumed once by check().
_event_failed: set = set()
# group341: symbol -> (monotonic ts, QualitySignal) for a FULLY fetched result, reused for QUALITY_GATE_RESULT_TTL_S seconds so the 10 s
# cycle does not re-fetch 3 endpoints for the same shortlisted symbol every time (and cause its own timeouts).
_result_cache: dict = {}
# group341: symbol -> (wall ts, earnings_date, negative_news) from the last event call that worked, so a later timeout does not
# silently switch the EARNINGS_TODAY / NEWS_NEGATIVE blocks off.
_event_memory: dict = {}


# group356: symbol -> the ONE in-flight event request (asyncio task) shared by every waiter, and symbol -> the answer of a request that
# finished after its waiters gave up: (monotonic ts, has_catalyst, event_score, bulk_flag, earnings_date, negative_news).
_event_inflight: dict = {}
_event_late: dict = {}


def reset_runtime_state() -> None:
    """group341: forget the result cache, event memory and failure marks (tests / manual reset)."""
    _event_failed.clear()
    _result_cache.clear()
    _event_memory.clear()
    _event_late.clear()
    _event_inflight.clear()
    _ft_late.clear()
    _ft_inflight.clear()

# event types that are a hard negative on their own (decayed impact at / below the limit): a regulatory action, delisting risk, a FRESH earnings miss.
_HARD_NEGATIVE_TYPES = {"regulatory_action": -8.0, "delisting_risk": -8.0, "earnings_miss": -15.0}


def _negative_news_from(body) -> Optional[str]:
    """A "NEWS_NEGATIVE:..." reason from an /events/{symbol}/categorized body, or None. Missing score / breakdown = None. Never raises."""
    try:
        if not config.NEWS_NEGATIVE_BLOCK_ENABLED or not isinstance(body, dict):
            return None
        for b in (body.get("event_score_breakdown") or []):
            if not isinstance(b, dict):
                continue
            limit = _HARD_NEGATIVE_TYPES.get(b.get("type"))
            if limit is not None and float(b.get("decayed_impact") or 0.0) <= limit:
                return f"NEWS_NEGATIVE:{b.get('type')} on record (decayed impact {float(b['decayed_impact']):+.1f}) - not a long"
        floor = float(config.NEWS_NEGATIVE_BLOCK_SCORE or 0.0)
        raw = body.get("event_score")
        if floor > 0 and raw is not None:
            score = float(raw)
            if score == score and score <= floor:
                return f"NEWS_NEGATIVE:event score {score:.0f} <= {floor:.0f} (negative news / events) - not a long"
        return None
    except Exception:  # noqa: BLE001
        return None


def _earnings_date_from(body) -> Optional[str]:
    """The earnings date (YYYY-MM-DD) in an /events/{symbol}/categorized body, or None. Both `upcoming` and `recent` are read: the
    endpoint files a bare date of TODAY under `recent` (it compares midnight against the current instant). Never raises."""
    try:
        if not isinstance(body, dict):
            return None
        dates = []
        for key in ("upcoming", "recent"):
            for item in (body.get(key) or []):
                if isinstance(item, dict) and item.get("type") == "earnings_date":
                    raw = str(item.get("date") or "")[:10]
                    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
                        dates.append(raw)
        return min(dates) if dates else None
    except Exception:  # noqa: BLE001
        return None


async def _fetch_event_signal(client: httpx.AsyncClient, symbol: str, timeout: Optional[float] = None) -> tuple[Optional[bool], Optional[float], bool]:
    has_catalyst = event_score = None
    bulk_flag = False
    try:
        r = await client.get(f"{config.EVENT_URL}/events/{quote(symbol, safe='&')}/categorized",
                             timeout=config.QUALITY_GATE_TIMEOUT_S if timeout is None else timeout)
        if r.status_code == 200:
            ej = r.json()
            has_catalyst = ej.get("has_positive_catalyst")
            event_score = ej.get("recent_event_score")
            bulk_flag = bool(ej.get("bulk_deals"))
            # group329: remember the earnings date from this same answer (the return shape stays a 3-tuple); check() picks it up.
            _ed = _earnings_date_from(ej)
            if _ed:
                _earnings_seen[symbol] = _ed
            _nr = _negative_news_from(ej)
            if _nr:
                _news_seen[symbol] = _nr
        else:
            _event_failed.add(symbol)   # group341: non-200 answer
    except Exception as e:
        _event_failed.add(symbol)       # group341: timeout / transport error
        # See the matching BUG FIX comment in _fetch_fundamental above.
        logger.info("quality_gate: event fetch failed for %s (%s)", symbol, str(e) or type(e).__name__)
    return has_catalyst, event_score, bulk_flag


def _shared_fetch_enabled() -> bool:
    return bool(getattr(config, "QUALITY_GATE_EVENT_SHARED_FETCH", False))


async def _event_worker(symbol: str) -> tuple:
    """group356: ONE event request for `symbol` with the long hard limit. Returns (failed, (catalyst, score, bulk), earnings_date,
    negative_news). The side-channel dicts _fetch_event_signal() writes are read and cleared in the same step it returns (no await in
    between), so a concurrent check() of the same symbol can never see or lose them. Never raises."""
    hard = float(getattr(config, "QUALITY_GATE_EVENT_BG_TIMEOUT_S", 45.0) or 45.0)
    try:
        async with httpx.AsyncClient() as client:
            res = await _fetch_event_signal(client, symbol, timeout=hard)
        failed = symbol in _event_failed
        _event_failed.discard(symbol)
        return failed, res, _earnings_seen.pop(symbol, None), _news_seen.pop(symbol, None)
    except asyncio.CancelledError:
        _event_failed.discard(symbol)
        raise
    except Exception as e:  # noqa: BLE001 - a quality signal must never be able to crash the trading loop
        _event_failed.discard(symbol)
        logger.info("quality_gate: shared event fetch for %s crashed (%s)", symbol, str(e) or type(e).__name__)
        return True, (None, None, False), None, None


def _event_task_done(symbol: str, task) -> None:
    """Done-callback of the shared request: unregister it, and keep a GOOD answer so the next check() of the symbol can use it even if
    every waiter had already given up. Never raises."""
    try:
        if _event_inflight.get(symbol) is task:
            _event_inflight.pop(symbol, None)
        if task.cancelled():
            return
        failed, res, earn, neg = task.result()
        if failed:
            return
        if len(_event_late) > 500:
            _event_late.clear()
        _event_late[symbol] = (_time.monotonic(), res[0], res[1], res[2], earn, neg)
        _event_memory[symbol] = (_time.time(), earn, neg)
    except Exception:  # noqa: BLE001
        pass


async def _event_shared_wait(symbol: str, wait_s: float) -> Optional[tuple]:
    """Wait up to `wait_s` for the shared in-flight event request of `symbol` (starting it when none is running). Returns its result
    tuple, or None when it has not answered yet - it keeps running either way (shielded), and a later waiter joins it."""
    loop = asyncio.get_running_loop()
    task = _event_inflight.get(symbol)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_event_worker(symbol))
        _event_inflight[symbol] = task
        task.add_done_callback(lambda t, _s=symbol: _event_task_done(_s, t))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.05, float(wait_s)))
    except asyncio.TimeoutError:
        return None


# group356b: the same one-request-per-symbol + keep-the-late-answer treatment for the fundamental / technical calls (they stay fail-open:
# a field that never arrives is still just "unknown"; but a slow answer is no longer thrown away, and a symbol with no cached scores
# gets the retry window before it passes through unfiltered). symbol -> (monotonic ts, fundamental, technical, market_cap_cr).
_ft_inflight: dict = {}
_ft_late: dict = {}


async def _ft_worker(symbol: str) -> tuple:
    hard = float(getattr(config, "QUALITY_GATE_EVENT_BG_TIMEOUT_S", 45.0) or 45.0)
    try:
        async with httpx.AsyncClient() as client:
            return await _fetch_fund_tech(client, symbol, timeout=hard)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        logger.info("quality_gate: shared fund/tech fetch for %s crashed (%s)", symbol, str(e) or type(e).__name__)
        return None, None, None


def _ft_task_done(symbol: str, task) -> None:
    try:
        if _ft_inflight.get(symbol) is task:
            _ft_inflight.pop(symbol, None)
        if task.cancelled():
            return
        f, t, m = task.result()
        if f is None and t is None and m is None:
            return
        if len(_ft_late) > 500:
            _ft_late.clear()
        _ft_late[symbol] = (_time.monotonic(), f, t, m)
    except Exception:  # noqa: BLE001
        pass


async def _ft_shared_wait(symbol: str, wait_s: float) -> Optional[tuple]:
    """Like _event_shared_wait, for the fundamental + technical pair. None = not answered yet (it keeps running)."""
    loop = asyncio.get_running_loop()
    task = _ft_inflight.get(symbol)
    if task is None or task.done() or task.get_loop() is not loop:
        task = loop.create_task(_ft_worker(symbol))
        _ft_inflight[symbol] = task
        task.add_done_callback(lambda t, _s=symbol: _ft_task_done(_s, t))
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.05, float(wait_s)))
    except asyncio.TimeoutError:
        return None


def _take_late_ft(symbol: str) -> Optional[tuple]:
    hit = _ft_late.pop(symbol, None)
    if hit is None:
        return None
    ttl = float(getattr(config, "QUALITY_GATE_EVENT_LATE_TTL_S", 0.0) or 0.0)
    if ttl <= 0 or _time.monotonic() - hit[0] > ttl:
        return None
    return hit


async def _ft_first(symbol: str, late_ft: Optional[tuple]) -> tuple:
    """((fund, tech, mcap), pending): a kept late answer, else the shared request for QUALITY_GATE_TIMEOUT_S."""
    if late_ft is not None:
        return (late_ft[1], late_ft[2], late_ft[3]), False
    r = await _ft_shared_wait(symbol, config.QUALITY_GATE_TIMEOUT_S)
    return (r, False) if r is not None else ((None, None, None), True)


def _take_late_event(symbol: str) -> Optional[tuple]:
    """A fresh answer left by an earlier request that outlived its waiters (consumed once), else None."""
    hit = _event_late.pop(symbol, None)
    if hit is None:
        return None
    ttl = float(getattr(config, "QUALITY_GATE_EVENT_LATE_TTL_S", 0.0) or 0.0)
    if ttl <= 0 or _time.monotonic() - hit[0] > ttl:
        return None
    return hit


def _apply_cache_fallback(signal: QualitySignal, cached: Optional[QualitySignal]) -> QualitySignal:
    """session68: fill in whichever fields this live fetch didn't return
    (still None) from the last-known-good cached signal. Live data always
    wins when present — this never overwrites a value the fetch DID get."""
    if cached is None:
        return signal
    if signal.fundamental_score is None:
        signal.fundamental_score = cached.fundamental_score
    if signal.technical_score is None:
        signal.technical_score = cached.technical_score
    if signal.market_cap_cr is None:
        signal.market_cap_cr = cached.market_cap_cr
    return signal


async def check(symbol: str, cached: Optional[QualitySignal] = None) -> QualitySignal:
    """Run all three best-effort checks concurrently for one symbol.
    Never raises — worst case every field is None/False.

    `cached` (session68): the symbol's last-known-good scores from
    models.ScalpQualityCache, passed in by main.py via get_cache_batch()
    (see that function — never fetched here, to keep this coroutine free
    of DB access while several run concurrently). Any field this live
    fetch didn't return falls back to `cached` instead of staying None."""
    if not config.QUALITY_GATE_ENABLED:
        return QualitySignal(symbol=symbol)

    # group341: a fully fetched result from the last QUALITY_GATE_RESULT_TTL_S seconds is reused (live fetch still runs for anything else).
    _ttl = float(getattr(config, "QUALITY_GATE_RESULT_TTL_S", 0.0) or 0.0)
    if _ttl > 0:
        _hit = _result_cache.get(symbol)
        if _hit is not None and _time.monotonic() - _hit[0] < _ttl:
            return dataclasses.replace(_hit[1])
    _news_seen.pop(symbol, None)          # group330: same rule for the negative-news reason
    _earnings_seen.pop(symbol, None)      # group329: never reuse a date left by an earlier, failed check
    _event_failed.discard(symbol)
    _shared = _shared_fetch_enabled()
    _late = _take_late_event(symbol) if _shared else None
    _late_ft = _take_late_ft(symbol) if _shared else None
    _ft_pending = False       # group356b: the shared fund/tech request has not answered yet after the first wait
    _ev_pending = False       # group356: the shared event request has not answered yet after the first wait
    async with httpx.AsyncClient() as client:
        try:
            if _shared:
                # group356: a good answer left by an earlier cycle's request is used as is; otherwise wait on the one in-flight request.
                if _late is not None:
                    (fund_score, tech_score, market_cap_cr), _ft_pending = await _ft_first(symbol, _late_ft)
                    has_catalyst, event_score, bulk_flag = _late[1], _late[2], _late[3]
                    if _late[4]:
                        _earnings_seen[symbol] = _late[4]
                    if _late[5]:
                        _news_seen[symbol] = _late[5]
                else:
                    ((fund_score, tech_score, market_cap_cr), _ft_pending), _ev1 = await asyncio.gather(
                        _ft_first(symbol, _late_ft),
                        _event_shared_wait(symbol, config.QUALITY_GATE_TIMEOUT_S),
                    )
                    if _ev1 is None:
                        has_catalyst = event_score = None
                        bulk_flag = False
                        _ev_pending = True
                    else:
                        _failed, _res, _e, _n = _ev1
                        has_catalyst, event_score, bulk_flag = _res
                        if _failed:
                            _event_failed.add(symbol)
                        else:
                            _event_late.pop(symbol, None)       # answered directly: nothing left over for the next cycle
                            if _e:
                                _earnings_seen[symbol] = _e
                            if _n:
                                _news_seen[symbol] = _n
            else:
                (fund_score, tech_score, market_cap_cr), (has_catalyst, event_score, bulk_flag) = await asyncio.gather(
                    _fetch_fund_tech(client, symbol),
                    _fetch_event_signal(client, symbol),
                )
        except Exception as e:
            # Belt-and-suspenders — asyncio.gather itself shouldn't raise given
            # the inner functions already swallow their own exceptions, but a
            # quality signal must never be able to crash the trading loop.
            logger.error("quality_gate: unexpected error for %s: %s", symbol, e, exc_info=True)
            return _apply_cache_fallback(QualitySignal(symbol=symbol), cached)

    # group356b: a symbol with no cached scores at all would pass the fundamental / technical floors unchecked, so it gets the same
    # retry window on its (single) in-flight request; one that has cached scores keeps the quick fail-open path (the late answer
    # refreshes the cache next cycle).
    _ft_need = _ft_pending and (cached is None or (cached.fundamental_score is None and cached.technical_score is None
                                                   and cached.market_cap_cr is None))
    if _ev_pending or _ft_need:
        # group356: still unanswered after QUALITY_GATE_TIMEOUT_S - keep waiting on the SAME request (no second one) for the retry window.
        # With the fail-block switched off the gate stays fail-open and quick: no extra wait (the answer is still kept for next cycle).
        _rtw = float(getattr(config, "QUALITY_GATE_EVENT_RETRY_TIMEOUT_S", 0.0) or 0.0)
        _rw = _rtw if getattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", True) else 0.0

        async def _no_wait():
            return None
        _ev2, _ft2 = await asyncio.gather(
            _event_shared_wait(symbol, _rw) if (_ev_pending and _rw > 0) else _no_wait(),
            _ft_shared_wait(symbol, _rtw) if (_ft_need and _rtw > 0) else _no_wait(),
        )
        if _ft2 is not None:
            fund_score, tech_score, market_cap_cr = _ft2
            _ft_late.pop(symbol, None)
    if _ev_pending:
        if _ev2 is None:
            _event_failed.add(symbol)         # still running; its answer is kept for the next cycle by _event_task_done()
        else:
            _failed, _res, _e, _n = _ev2
            has_catalyst, event_score, bulk_flag = _res
            if _failed:
                _event_failed.add(symbol)
            else:
                _event_late.pop(symbol, None)
                if _e:
                    _earnings_seen[symbol] = _e
                if _n:
                    _news_seen[symbol] = _n
    _ev_failed = symbol in _event_failed
    if _ev_failed and getattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", True) and not _shared:
        # group344: one retry with a longer timeout before the symbol is treated as unchecked.
        _rt = float(getattr(config, "QUALITY_GATE_EVENT_RETRY_TIMEOUT_S", 0.0) or 0.0)
        if _rt > 0:
            _event_failed.discard(symbol)
            async with httpx.AsyncClient() as _c2:
                _rc, _rs, _rb = await _fetch_event_signal(_c2, symbol, timeout=_rt)
            if symbol not in _event_failed:
                has_catalyst, event_score, bulk_flag = _rc, _rs, _rb
                _ev_failed = False
            else:
                _ev_failed = True
    _event_failed.discard(symbol)
    _earn = _earnings_seen.pop(symbol, None)
    _neg = _news_seen.pop(symbol, None)
    _mem_h = float(getattr(config, "QUALITY_GATE_EVENT_MEMORY_H", 0.0) or 0.0)
    if _mem_h > 0:
        if _ev_failed:
            _m = _event_memory.get(symbol)
            if _m is not None and _time.time() - _m[0] < _mem_h * 3600.0:
                _earn = _earn or _m[1]
                _neg = _neg or _m[2]
        else:
            _event_memory[symbol] = (_time.time(), _earn, _neg)
    # group344: event unknown (failed twice, nothing remembered) -> unchecked, unless the service has been down so long that blocking would just stop trading.
    _unchecked = False
    if getattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", True):
        if not _ev_failed:
            _event_fail_since[0] = None
        else:
            if _event_fail_since[0] is None:
                _event_fail_since[0] = _time.monotonic()
            _remembered = _mem_h > 0 and symbol in _event_memory and _time.time() - _event_memory[symbol][0] < _mem_h * 3600.0
            if not _remembered:
                _open_after = float(getattr(config, "QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S", 0.0) or 0.0)
                if _open_after > 0 and _time.monotonic() - _event_fail_since[0] >= _open_after:
                    logger.warning("quality_gate: the event service has failed for %.0fs - letting %s through without a news check (fail-open valve)",
                                   _time.monotonic() - _event_fail_since[0], symbol)
                else:
                    _unchecked = True
    _sig = _apply_cache_fallback(QualitySignal(
        symbol=symbol,
        event_unchecked=_unchecked,
        fundamental_score=fund_score,
        technical_score=tech_score,
        market_cap_cr=market_cap_cr,
        has_positive_catalyst=has_catalyst,
        recent_event_score=event_score,
        bulk_deal_flag=bulk_flag,
        earnings_date=_earn,
        negative_news=_neg,
    ), cached)
    # only a complete LIVE answer is reused; anything that fell back to the cache or lost the event call is fetched again next time
    if _ttl > 0 and not _ev_failed and None not in (fund_score, tech_score, market_cap_cr):
        if len(_result_cache) > 2000:
            _result_cache.clear()
        _result_cache[symbol] = (_time.monotonic(), dataclasses.replace(_sig))
    return _sig
