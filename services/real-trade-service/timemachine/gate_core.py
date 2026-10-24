"""timemachine/gate_core.py - E115: the Time Machine wired into LIVE stock picking. One file, byte-identical in position-stocks-service and
real-trade-service (scripts/check_shared_copies.py enforces it); each service adds a thin adapter (orders/tm_gate.py, entry_engine/tm_gate.py).

WHAT IT DOES, in three places a trade is chosen:
  1. RANK   shortlist builders ask `nudge(symbol, side)`: a stock whose own recent 5-minute sessions AND a made-up-session simulation both say the
            rule wins gets a few points; one where both say it loses loses a few. Cache-only: a ranking never waits for a calculation.
  2. GATE   just before an entry is committed, `check(symbol, side, stop_pct, target_pct)` replays THIS trade's own stop % / target % at THIS
            time of day. Verdict AGAINST (simulation and real history both negative after charges and slippage) = the entry is refused.
            It can only SAY NO: it never adds a trade, never widens a stop, never raises a size.
  3. WARM   a background worker computes verdicts before they are needed, so the gate is a cache read. It runs on a TIMER (a data pre-fetch at
            TM_AUTO_PREFETCH_IST, first scan at TM_AUTO_FIRST_IST, then every TM_AUTO_INTERVAL_MIN minutes until TM_AUTO_LAST_IST) and on TRIGGERS
            (a shortlist built, an entry attempt, the Market Weather 09:14 lock, a runner job, an operator `POST /time-machine/scan`).

SAFE BY CONSTRUCTION: missing or slow data is "no opinion" and never blocks; a worker or gate bug never stops an entry (every public function
catches); an uncached entry waits at most TM_GATE_INLINE_BUDGET_S seconds, then proceeds with no opinion while the answer finishes in the
background for the next attempt; a failed fetch is remembered for TM_GATE_FAIL_TTL_S so a dead market-data does not add a timeout to every candidate.
MODES (config.TM_GATE_MODE, live-readable): live = a refusal blocks the entry; log = compute and record, never block; off = nothing runs.
It is a reality check, not a forecast: fixed percent levels, one to two months of one market mood, no order book. See timemachine/engine.py LIMITS.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import zlib
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

import config
from timemachine import data as tm_data
from timemachine import engine as E
from timemachine import service as tm_service

logger = logging.getLogger("time-machine-gate")

TAG = "time_machine"
VERDICT_CAP = 400                    # cached verdicts kept (oldest dropped)
ACTIVE_CAP = 60                      # symbols the timer keeps warm
QUEUE_CAP = 200
SESSIONS_TTL_S = 1800.0              # complete past sessions do not change during the day: keep them 30 minutes, not 10
_IST = timezone(timedelta(hours=5, minutes=30))

_lock = threading.RLock()
_verdicts: Dict[tuple, Dict[str, Any]] = {}          # key -> {"at": monotonic, "v": verdict dict}
_latest: Dict[Tuple[str, str], Dict[str, Any]] = {}  # (symbol, side) -> {"at", "v"}   most recent verdict, any levels (ranking reads this)
_failures: Dict[tuple, Tuple[float, str]] = {}       # key -> (monotonic, code)
_active: Dict[Tuple[str, str], Dict[str, Any]] = {}  # (symbol, side) -> {"stop", "target", "value", "at", "source"}
_inflight: Dict[tuple, Future] = {}
_queued: set = set()
_jobs: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=QUEUE_CAP)
_recent: Deque[Dict[str, Any]] = deque(maxlen=60)
_scans: Deque[Dict[str, Any]] = deque(maxlen=20)
_adapter: Dict[str, Any] = {"charges_fn": None, "provider": None, "service": ""}
_auto: Dict[str, Any] = {"last_scan_key": None, "prefetch_day": None}
_stats: Dict[str, int] = {}
_threads: Dict[str, Any] = {"worker": None, "ticker": None, "stop": threading.Event()}
_pool: Optional[ThreadPoolExecutor] = None
_spent: Deque[Tuple[float, float]] = deque(maxlen=400)       # (monotonic, seconds an entry waited inline): the rolling-minute wait budget
WINDOW_S = 60.0

_STAT_KEYS = ("gate_checks", "rejects", "would_reject_log", "passes", "no_opinion", "cache_hits", "inline_runs", "inline_timeouts", "inline_errors", "inline_skipped",
              "warm_runs", "warm_errors", "noted", "auto_scans", "manual_scans", "prefetch_runs", "nudged_up", "nudged_down", "errors")


def reset() -> None:
    """Forget everything (tests, and a clean start). Does not stop running threads."""
    with _lock:
        _verdicts.clear(); _latest.clear(); _failures.clear(); _active.clear(); _inflight.clear(); _queued.clear()
        _recent.clear(); _scans.clear(); _spent.clear()
        _auto.update(last_scan_key=None, prefetch_day=None)
        _stats.clear(); _stats.update({k: 0 for k in _STAT_KEYS})
        while True:
            try:
                _jobs.get_nowait()
            except queue.Empty:
                break


reset()


def configure(*, charges_fn: Optional[Callable[[float, float, int, bool], float]] = None, provider: Optional[Callable[[], List[Dict[str, Any]]]] = None,
              service: str = "") -> None:
    """The service adapter sets: the round-trip charge function (entry, exit, qty, short) -> rupees; a provider of the symbols that deserve a
    timed re-check ([{symbol, side, stop_pct, target_pct, value_rs}]); and its name for the status page."""
    with _lock:
        if charges_fn is not None:
            _adapter["charges_fn"] = charges_fn
        if provider is not None:
            _adapter["provider"] = provider
        if service:
            _adapter["service"] = service


# ------------------------------------------------------------------ settings (read at call time, so the Switches page applies at once)

def _cfg(name: str, default: Any) -> Any:
    try:
        v = getattr(config, name, default)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def _num(raw: Any, default: float, lo: float, hi: float) -> float:
    """A bounded number from a raw switch value (callers pass _cfg("NAME", default) so each switch is a visible live read)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        v = float(default)
    if v != v:
        v = float(default)
    return max(lo, min(hi, v))


def mode() -> str:
    """live | log | off. The Time Machine switch (TIME_MACHINE_ENABLED) off means off here too."""
    try:
        if not tm_service.enabled():
            return "off"
        m = str(_cfg("TM_GATE_MODE", "live")).strip().lower()
        return m if m in ("live", "log") else "off"
    except Exception:  # noqa: BLE001
        return "off"


def _block_codes() -> Tuple[str, ...]:
    return ("AGAINST", "NEAR_BREAKEVEN") if str(_cfg("TM_GATE_BLOCK", "against")).strip().lower() == "against_or_near" else ("AGAINST",)


def _stat(k: str, n: int = 1) -> None:
    with _lock:
        _stats[k] = _stats.get(k, 0) + n


# ------------------------------------------------------------------ time helpers

def _now_ist(now: Optional[datetime] = None) -> datetime:
    if now is not None:
        return now.astimezone(_IST) if now.tzinfo else now.replace(tzinfo=_IST)
    return datetime.now(timezone.utc).astimezone(_IST)


def _mod(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _is_trading_day(now: datetime) -> bool:
    try:
        if now.weekday() >= 5:
            return False
        try:
            import tz_utils
            return not tz_utils.is_nse_holiday(now)
        except Exception:  # noqa: BLE001 - no calendar here: a weekday is treated as a trading day
            return True
    except Exception:  # noqa: BLE001
        return False


def _hhmm(name: str, default: str) -> int:
    try:
        return E.hhmm(str(_cfg(name, default)))
    except Exception:  # noqa: BLE001
        return E.hhmm(default)


def entry_slot(side: str, now: Optional[datetime] = None) -> Optional[int]:
    """The 5-minute bar a trade opened now would enter on (minute of day), or None when the flat-by time has passed. Before the open it is 09:15."""
    n = _now_ist(now)
    flat = E.hhmm(tm_service._default_flat_by(side))
    m = max(E.OPEN_MIN, _mod(n))
    slot = E.OPEN_MIN + ((m - E.OPEN_MIN) // E.SLOT_MIN) * E.SLOT_MIN
    return slot if slot < flat else None


# ------------------------------------------------------------------ the calculation

def _charges_fn() -> Callable[[float, float, int, bool], float]:
    f = _adapter.get("charges_fn")
    return f if f is not None else tm_service.default_charges_fn()


def _norm_side(side: Any) -> str:
    s = str(side or "LONG").strip().upper()
    s = {"BUY": "LONG", "SELL": "SHORT"}.get(s, s)
    if s not in ("LONG", "SHORT"):
        raise tm_service.TimeMachineError("BAD_PARAM", "side must be LONG or SHORT")
    return s


def _key(symbol: str, side: str, stop: float, target: float, slot: int, value: float) -> tuple:
    return (symbol, side, round(float(stop), 1), round(float(target), 1), int(slot), str(_cfg("TIME_MACHINE_LOOKBACK", "1mo")), int(value // 5000))


def _window_spent() -> float:
    """Seconds entries have spent waiting inline for a verdict in the last minute."""
    cut = time.monotonic() - WINDOW_S
    with _lock:
        return sum(s for t, s in _spent if t >= cut)


def _window_open() -> bool:
    """False once entries have waited TM_GATE_WINDOW_BUDGET_S in total during the last minute: further uncached checks are queued and given no opinion, so a slow
    market-data can delay a whole cycle of candidates by at most this much, however many there are. 0 = no cap."""
    cap = _num(_cfg("TM_GATE_WINDOW_BUDGET_S", 8.0), 8.0, 0.0, 120.0)
    return cap <= 0 or _window_spent() < cap


def _verdict_ttl() -> float:
    return _num(_cfg("TM_GATE_TTL_S", 300.0), 300.0, 30.0, 3600.0)


def compute(symbol: str, side: str, stop_pct: float, target_pct: float, slot: int, value_rs: Optional[float] = None, price: Optional[float] = None,
            sims: Optional[int] = None, fetch: Optional[tm_data.Fetcher] = None, today: Optional[str] = None) -> Dict[str, Any]:
    """One compact verdict for one stock / side / levels / entry bar. Raises TimeMachineError. No order, no database, no network except the
    market-data history fetch (cached). Deterministic per (stock, side, slot, day) so a retry gives the same answer."""
    started = time.monotonic()
    symbol = tm_data.normalize_symbol(symbol)
    side = _norm_side(side)
    sims_n = int(sims or _num(_cfg("TM_GATE_SIMS", 3000), 3000, 1000, 20000))
    value = float(value_rs) if value_rs and value_rs > 0 else float(_cfg("TIME_MACHINE_DEFAULT_VALUE_RS", 20000.0))
    seed = zlib.crc32(f"{symbol}|{side}|{slot}|{today or _now_ist().strftime('%Y-%m-%d')}".encode()) & 0x7FFFFFFF
    body = {"side": side, "stop_pct": float(stop_pct), "target_pct": float(target_pct), "entry_time": E.fmt(slot), "entry_times": [E.fmt(slot)],
            "sims": sims_n, "seed": seed, "value_rs": max(1000.0, value)}
    p = tm_service.parse_params(body)
    try:
        sessions, info = tm_data.load_sessions(symbol, p["lookback"], fetch, None, ttl=SESSIONS_TTL_S)
    except tm_data.DataError as e:
        raise tm_service.TimeMachineError(e.code, e.message, e.status)
    days = sorted(sessions)
    ref = float(price) if price and price > 0 else float(sessions[days[-1]][-1][4])
    p["value_rs"] = max(p["value_rs"], ref)                         # one share must fit, or the charges cannot be worked out
    rule = tm_service.make_rule(p, ref, _charges_fn())
    hist = E.summarize_rows(E.replay_history(sessions, rule, [slot]), rule)
    sim = E.simulate(sessions, [(rule, [slot])], slot, sims_n, seed)
    sim_sum = E.summarize_acc(sim["per_plan"][0][slot], rule)
    vd = E.verdict(sim_sum, hist, rule, len(sessions))

    def r4(x: Any) -> Optional[float]:
        return round(float(x), 4) if isinstance(x, (int, float)) and x == x else None
    return {"symbol": symbol, "side": side, "code": vd["code"], "line": vd["line"], "entry_time": E.fmt(slot), "stop_pct": round(rule.stop_pct, 3),
            "target_pct": round(rule.target_pct, 3), "rr": round(rule.rr, 3), "sessions": len(sessions), "sims": sim["sessions"],
            "sim_p_target": r4(sim_sum.get("p_target")) if sim_sum.get("n") else None, "sim_avg_r_net": r4(sim_sum.get("avg_r_net")) if sim_sum.get("n") else None,
            "hist_n": int(hist.get("n", 0) or 0), "hist_p_target": r4(hist.get("p_target")) if hist.get("n") else None,
            "hist_avg_r_net": r4(hist.get("avg_r_net")) if hist.get("n") else None, "be_win_net": r4(vd.get("be_win_net")),
            "computed_at": _now_ist().isoformat(timespec="seconds"), "elapsed_ms": int((time.monotonic() - started) * 1000)}


def _compute_cached(key: tuple, symbol: str, side: str, stop: float, target: float, slot: int, value: float, price: Optional[float],
                    fetch: Optional[tm_data.Fetcher] = None) -> Dict[str, Any]:
    v = compute(symbol, side, stop, target, slot, value, price, fetch=fetch)
    now = time.monotonic()
    with _lock:
        _verdicts[key] = {"at": now, "v": v}
        _latest[(symbol, side)] = {"at": now, "v": v}
        _failures.pop(key, None)
        if len(_verdicts) > VERDICT_CAP:
            for k in sorted(_verdicts, key=lambda x: _verdicts[x]["at"])[: len(_verdicts) - VERDICT_CAP]:
                _verdicts.pop(k, None)
    return v


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="tm-gate")
        return _pool


def _lookup(key: tuple) -> Optional[Dict[str, Any]]:
    with _lock:
        hit = _verdicts.get(key)
        if hit and time.monotonic() - hit["at"] < _verdict_ttl():
            return hit["v"]
    return None


# ------------------------------------------------------------------ the decision

def _decide(v: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    if v.get("code") in _block_codes():
        return True, (f"{TAG.upper()}: {v['side']} {v['symbol']} at {v['entry_time']} with stop {v['stop_pct']:.2f}% / target {v['target_pct']:.2f}%: "
                      f"{v['line']}")[:420]
    return False, None


def _no_opinion(why: str, source: str = "none") -> Dict[str, Any]:
    return {"opinion": "no_opinion", "would_reject": False, "reason": None, "tag": TAG, "v": None, "source": source, "no_data": why, "mode": mode()}


def evaluate(symbol: Any, side: Any, stop_pct: Any, target_pct: Any, price: Any = None, value_rs: Any = None, *, allow_inline: bool = True,
             now: Optional[datetime] = None, fetch: Optional[tm_data.Fetcher] = None) -> Dict[str, Any]:
    """The gate's answer for one prospective entry. {"opinion": "ok" | "no_opinion", "would_reject", "reason", "v", "source": cache | inline | none, "no_data"}.
    Never raises. An uncached answer is computed inline for at most TM_GATE_INLINE_BUDGET_S seconds; slower = no opinion now, cached for next time."""
    try:
        m = mode()
        if m == "off":
            return _no_opinion("off")
        try:
            sym = tm_data.normalize_symbol(symbol)
            sd = _norm_side(side)
            stop, target = float(stop_pct), float(target_pct)
            if not (stop > 0 and target > 0):
                raise ValueError("levels")
        except Exception:  # noqa: BLE001
            return _no_opinion("bad symbol, side or levels")
        slot = entry_slot(sd, now)
        if slot is None:
            return _no_opinion("past the flat-by time")
        px = float(price) if isinstance(price, (int, float)) and price and price > 0 else None
        value = float(value_rs) if isinstance(value_rs, (int, float)) and value_rs and value_rs > 0 else float(_cfg("TIME_MACHINE_DEFAULT_VALUE_RS", 20000.0))
        key = _key(sym, sd, stop, target, slot, value)
        v = _lookup(key)
        source = "cache"
        if v is not None:
            _stat("cache_hits")
        else:
            with _lock:
                fail = _failures.get(key)
            if fail and time.monotonic() - fail[0] < _num(_cfg("TM_GATE_FAIL_TTL_S", 120.0), 120.0, 10.0, 1800.0):
                return _no_opinion(f"data unavailable ({fail[1]})")
            budget = _num(_cfg("TM_GATE_INLINE_BUDGET_S", 2.5), 2.5, 0.0, 15.0)
            if allow_inline and budget > 0 and not _window_open():
                _stat("inline_skipped")
                _enqueue(sym, sd, stop, target, value, px, "gate-window", slot)
                return _no_opinion("inline wait budget for this minute is used up (queued)")
            if not allow_inline or budget <= 0:
                _enqueue(sym, sd, stop, target, value, px, "gate-miss", slot)
                return _no_opinion("not computed yet (queued)")
            with _lock:
                fut = _inflight.get(key)
                if fut is None:
                    fut = _executor().submit(_run_inline, key, sym, sd, stop, target, slot, value, px, fetch)
                    _inflight[key] = fut
            t_wait = time.monotonic()
            try:
                v = fut.result(timeout=budget)
                source = "inline"
            except FutureTimeout:
                with _lock:
                    _spent.append((time.monotonic(), time.monotonic() - t_wait))
                _stat("inline_timeouts")
                return _no_opinion(f"still computing after {budget:g}s (finishes in the background)")
            except Exception as e:  # noqa: BLE001 - TimeMachineError / DataError / anything: remember, no opinion
                code = getattr(e, "code", type(e).__name__)
                with _lock:
                    _failures[key] = (time.monotonic(), str(code))
                    _spent.append((time.monotonic(), time.monotonic() - t_wait))
                _stat("inline_errors")
                return _no_opinion(f"could not compute ({code})")
            with _lock:
                _spent.append((time.monotonic(), time.monotonic() - t_wait))
        would, reason = _decide(v)
        return {"opinion": "ok", "would_reject": would, "reason": reason, "tag": TAG, "v": v, "source": source, "no_data": None, "mode": m}
    except Exception as e:  # noqa: BLE001 - a gate bug must never stop an entry
        _stat("errors")
        logger.warning("time machine gate: evaluate failed (no opinion): %s", e)
        return _no_opinion("internal error")


def _run_inline(key, sym, sd, stop, target, slot, value, px, fetch):
    try:
        _stat("inline_runs")
        return _compute_cached(key, sym, sd, stop, target, slot, value, px, fetch)
    finally:
        with _lock:
            _inflight.pop(key, None)


def check(symbol: Any, side: Any, stop_pct: Any, target_pct: Any, price: Any = None, value_rs: Any = None, *,
          now: Optional[datetime] = None, fetch: Optional[tm_data.Fetcher] = None) -> Tuple[Dict[str, Any], Optional[str]]:
    """(the evaluation, the refusal reason). The reason is set only when the entry must be refused (mode live and a blocking verdict). Counts and records every check.
    Never raises: on a bug it returns a no-opinion evaluation and no reason."""
    try:
        ev = evaluate(symbol, side, stop_pct, target_pct, price, value_rs, now=now, fetch=fetch)
        _stat("gate_checks")
        enforced = bool(ev["would_reject"]) and ev.get("mode") == "live"
        if ev["opinion"] != "ok":
            _stat("no_opinion")
        elif ev["would_reject"]:
            _stat("rejects" if enforced else "would_reject_log")
        else:
            _stat("passes")
        _remember(symbol, side, ev, enforced)
        if enforced:
            logger.info("TIME_MACHINE gate refused %s %s: %s", side, symbol, ev["reason"])
        return ev, (ev["reason"] if enforced else None)
    except Exception as e:  # noqa: BLE001
        _stat("errors")
        logger.warning("time machine gate: check failed (allowing): %s", e)
        return _no_opinion("internal error"), None


def gate_reject(symbol: Any, side: Any, stop_pct: Any, target_pct: Any, price: Any = None, value_rs: Any = None, *,
                now: Optional[datetime] = None, fetch: Optional[tm_data.Fetcher] = None) -> Optional[str]:
    """Reason text when this entry must be refused, else None. Never raises."""
    return check(symbol, side, stop_pct, target_pct, price, value_rs, now=now, fetch=fetch)[1]


def _remember(symbol: Any, side: Any, ev: Dict[str, Any], enforced: bool) -> None:
    v = ev.get("v") or {}
    with _lock:
        _recent.append({"at": _now_ist().isoformat(timespec="seconds"), "symbol": str(symbol), "side": str(side), "opinion": ev["opinion"], "code": v.get("code"),
                        "would_reject": bool(ev["would_reject"]), "enforced": enforced, "source": ev.get("source"), "no_data": ev.get("no_data"),
                        "sim_avg_r_net": v.get("sim_avg_r_net"), "hist_n": v.get("hist_n"), "hist_avg_r_net": v.get("hist_avg_r_net"),
                        "entry_time": v.get("entry_time"), "stop_pct": v.get("stop_pct"), "target_pct": v.get("target_pct")})


def attach(flow_json: Optional[str], ev: Optional[Dict[str, Any]]) -> Optional[str]:
    """flow_json text with a compact copy of the verdict under "tm" (so every trade is stamped with what the Time Machine said); unchanged without a verdict."""
    try:
        import json
        v = (ev or {}).get("v")
        if not v:
            return flow_json
        try:
            base = json.loads(flow_json) if flow_json else {}
            base = base if isinstance(base, dict) else {}
        except (TypeError, ValueError):
            base = {}
        base["tm"] = {k: v.get(k) for k in ("code", "entry_time", "stop_pct", "target_pct", "sim_p_target", "sim_avg_r_net", "hist_n", "hist_avg_r_net", "be_win_net", "sessions")}
        base["tm"]["mode"] = ev.get("mode")
        return json.dumps(base, separators=(",", ":"))
    except Exception:  # noqa: BLE001
        return flow_json


# ------------------------------------------------------------------ ranking

def nudge(symbol: Any, side: Any = "LONG") -> float:
    """Ranking points for a stock from its latest cached verdict (any levels, within twice the cache time): +TM_PICK_NUDGE_PTS when the odds favour the rule, -that when
    they are against, 0 otherwise or without a fresh verdict. Cache-only (never waits). TM_PICK_NUDGE_PTS=0 or mode off = 0. Never raises."""
    try:
        pts = _num(_cfg("TM_PICK_NUDGE_PTS", 6.0), 6.0, 0.0, 30.0)
        if pts <= 0 or mode() == "off":
            return 0.0
        sym = tm_data.normalize_symbol(symbol)
        sd = _norm_side(side)
        with _lock:
            hit = _latest.get((sym, sd))
        if not hit or time.monotonic() - hit["at"] > _verdict_ttl() * 2:
            return 0.0
        code = hit["v"].get("code")
        if code == "FAVOURS":
            _stat("nudged_up")
            return pts
        if code == "AGAINST":
            _stat("nudged_down")
            return -pts
        return 0.0
    except Exception:  # noqa: BLE001
        return 0.0


def peek(symbol: Any, side: Any = "LONG") -> Optional[Dict[str, Any]]:
    try:
        with _lock:
            hit = _latest.get((tm_data.normalize_symbol(symbol), _norm_side(side)))
        return dict(hit["v"]) if hit else None
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ the warm queue (trigger: a pick appears) and the timer (trigger: the clock)

def _enqueue(sym: str, side: str, stop: float, target: float, value: float, price: Optional[float], source: str, slot: Optional[int] = None) -> bool:
    slot = slot if slot is not None else entry_slot(side)
    if slot is None:
        return False
    key = _key(sym, side, stop, target, slot, value)
    with _lock:
        if key in _queued or key in _inflight or _lookup(key) is not None:
            return False
        f = _failures.get(key)
        if f and time.monotonic() - f[0] < _num(_cfg("TM_GATE_FAIL_TTL_S", 120.0), 120.0, 10.0, 1800.0):
            return False
        try:
            _jobs.put_nowait({"key": key, "symbol": sym, "side": side, "stop": stop, "target": target, "value": value, "price": price, "slot": slot, "source": source})
        except queue.Full:
            return False
        _queued.add(key)
    return True


def note(symbol: Any, side: Any, stop_pct: Any, target_pct: Any, value_rs: Any = None, price: Any = None, source: str = "", enqueue: bool = True) -> bool:
    """A pick appeared (shortlist built, entry attempted): remember it for the timer and, with enqueue, get its verdict computed in the background.
    An entry check passes enqueue=False (it computes the verdict itself). Never raises, never waits."""
    try:
        if mode() == "off":
            return False
        sym, sd = tm_data.normalize_symbol(symbol), _norm_side(side)
        stop, target = float(stop_pct), float(target_pct)
        if not (stop > 0 and target > 0):
            return False
        value = float(value_rs) if isinstance(value_rs, (int, float)) and value_rs and value_rs > 0 else float(_cfg("TIME_MACHINE_DEFAULT_VALUE_RS", 20000.0))
        px = float(price) if isinstance(price, (int, float)) and price and price > 0 else None
        with _lock:
            _active[(sym, sd)] = {"stop": stop, "target": target, "value": value, "at": time.monotonic(), "source": source}
            if len(_active) > ACTIVE_CAP:
                for k in sorted(_active, key=lambda x: _active[x]["at"])[: len(_active) - ACTIVE_CAP]:
                    _active.pop(k, None)
        _stat("noted")
        return _enqueue(sym, sd, stop, target, value, px, source or "note") if enqueue else False
    except Exception:  # noqa: BLE001
        return False


def process_queue_once(max_jobs: int = 50, fetch: Optional[tm_data.Fetcher] = None) -> int:
    """Run queued warm jobs on the calling thread (the worker thread calls this; tests call it directly). Returns how many ran."""
    n = 0
    while n < max_jobs:
        try:
            job = _jobs.get_nowait()
        except queue.Empty:
            break
        key = job["key"]
        try:
            if mode() == "off":
                continue
            _compute_cached(key, job["symbol"], job["side"], job["stop"], job["target"], job["slot"], job["value"], job["price"], fetch)
            _stat("warm_runs")
        except Exception as e:  # noqa: BLE001
            with _lock:
                _failures[key] = (time.monotonic(), str(getattr(e, "code", type(e).__name__)))
            _stat("warm_errors")
        finally:
            with _lock:
                _queued.discard(key)
            n += 1
    return n


def _targets() -> List[Dict[str, Any]]:
    """What the timer re-checks: the adapter's provider (the live shortlist) first, then picks noted in the last TM_AUTO_ACTIVE_MIN minutes."""
    out: List[Dict[str, Any]] = []
    seen = set()
    prov = _adapter.get("provider")
    if prov is not None:
        try:
            for r in prov() or []:
                k = (str(r.get("symbol") or "").upper(), _norm_side(r.get("side") or "LONG"))
                if k[0] and k not in seen and float(r.get("stop_pct") or 0) > 0 and float(r.get("target_pct") or 0) > 0:
                    seen.add(k)
                    out.append({"symbol": k[0], "side": k[1], "stop": float(r["stop_pct"]), "target": float(r["target_pct"]),
                                "value": float(r.get("value_rs") or 0) or float(_cfg("TIME_MACHINE_DEFAULT_VALUE_RS", 20000.0)), "price": r.get("price")})
        except Exception as e:  # noqa: BLE001
            logger.debug("time machine gate: provider failed (%s)", e)
    horizon = _num(_cfg("TM_AUTO_ACTIVE_MIN", 60.0), 60.0, 5.0, 600.0) * 60.0
    with _lock:
        for (sym, sd), a in sorted(_active.items(), key=lambda kv: -kv[1]["at"]):
            if (sym, sd) not in seen and time.monotonic() - a["at"] <= horizon:
                seen.add((sym, sd))
                out.append({"symbol": sym, "side": sd, "stop": a["stop"], "target": a["target"], "value": a["value"], "price": None})
    return out[: int(_num(_cfg("TM_AUTO_MAX_SYMBOLS", 12), 12, 1, 40))]


def scan(reason: str = "manual", now: Optional[datetime] = None, wait: bool = False, fetch: Optional[tm_data.Fetcher] = None) -> Dict[str, Any]:
    """Queue a fresh verdict for every current pick (for the CURRENT entry bar). The timer, the weather lock, the runner job and POST /time-machine/scan end here. Never raises."""
    try:
        if mode() == "off":
            return {"ok": False, "reason": reason, "queued": 0, "symbols": [], "note": "the gate is off (TM_GATE_MODE=off or TIME_MACHINE_ENABLED=false)"}
        targets = _targets()
        queued = []
        for t in targets:
            slot = entry_slot(t["side"], now)
            if slot is not None and _enqueue(t["symbol"], t["side"], t["stop"], t["target"], t["value"], t.get("price"), f"scan:{reason}", slot):
                queued.append(f"{t['side']} {t['symbol']}")
        ran = process_queue_once(fetch=fetch) if wait else 0
        _stat("manual_scans" if reason.startswith(("manual", "runner", "weather")) else "auto_scans")
        rec = {"at": _now_ist().isoformat(timespec="seconds"), "reason": reason, "targets": len(targets), "queued": len(queued), "ran": ran}
        with _lock:
            _scans.append(rec)
        return {"ok": True, **rec, "symbols": queued}
    except Exception as e:  # noqa: BLE001
        _stat("errors")
        return {"ok": False, "reason": reason, "queued": 0, "symbols": [], "note": f"scan failed: {type(e).__name__}"}


def due_trigger(now: Optional[datetime] = None) -> Optional[str]:
    """Which timed trigger is due right now, or None: "prefetch" once a day at TM_AUTO_PREFETCH_IST; "scan:HH:MM" for the latest scan boundary
    (first at TM_AUTO_FIRST_IST, then every TM_AUTO_INTERVAL_MIN minutes up to TM_AUTO_LAST_IST) that has not run. A boundary missed by more than
    one interval is not caught up (a stale slot is worse than none). Trading days only. Pure apart from the small state it keeps."""
    n = _now_ist(now)
    if not _is_trading_day(n):
        return None
    m, today = _mod(n), n.strftime("%Y-%m-%d")
    pre = _hhmm("TM_AUTO_PREFETCH_IST", "09:05")
    first, last = _hhmm("TM_AUTO_FIRST_IST", "09:16"), _hhmm("TM_AUTO_LAST_IST", "14:30")
    step = int(_num(_cfg("TM_AUTO_INTERVAL_MIN", 15), 15, 5, 120))
    if pre <= m < first and _auto["prefetch_day"] != today:
        _auto["prefetch_day"] = today
        return "prefetch"
    if first <= m <= last + step:
        b = first + ((m - first) // step) * step
        if b > last:
            return None
        k = f"{today} {E.fmt(b)}"
        if _auto["last_scan_key"] != k and m - b < step:
            _auto["last_scan_key"] = k
            return f"scan:{E.fmt(b)}"
    return None


def tick(now: Optional[datetime] = None, fetch: Optional[tm_data.Fetcher] = None, wait: bool = False) -> Optional[Dict[str, Any]]:
    """One timer pass (the ticker thread calls it every 20 seconds). Returns what it did, or None."""
    try:
        if mode() == "off" or not bool(_cfg("TM_AUTO_ENABLED", True)):
            return None
        trig = due_trigger(now)
        if trig is None:
            return None
        if trig == "prefetch":
            return prefetch(wait=wait, fetch=fetch)
        return scan(f"timer {trig.split(':', 1)[1]}", now, wait=wait, fetch=fetch)
    except Exception:  # noqa: BLE001
        _stat("errors")
        return None


def prefetch(wait: bool = False, fetch: Optional[tm_data.Fetcher] = None) -> Dict[str, Any]:
    """Load the 5-minute history of every current pick before the open, so the first gate checks of the day are cache reads. Data only, no verdict."""
    loaded, failed = [], []
    try:
        for t in _targets():
            try:
                tm_data.load_sessions(t["symbol"], str(_cfg("TIME_MACHINE_LOOKBACK", "1mo")), fetch, None, ttl=SESSIONS_TTL_S)
                loaded.append(t["symbol"])
            except Exception as e:  # noqa: BLE001
                failed.append(f"{t['symbol']}:{getattr(e, 'code', type(e).__name__)}")
        _stat("prefetch_runs")
    except Exception:  # noqa: BLE001
        _stat("errors")
    rec = {"at": _now_ist().isoformat(timespec="seconds"), "reason": "prefetch", "targets": len(loaded) + len(failed), "queued": 0, "ran": len(loaded)}
    with _lock:
        _scans.append(rec)
    return {"ok": True, **rec, "loaded": loaded, "failed": failed}


# ------------------------------------------------------------------ threads

def _worker_loop() -> None:
    stop: threading.Event = _threads["stop"]
    while not stop.is_set():
        try:
            if _jobs.empty():
                stop.wait(1.0)
                continue
            process_queue_once(10)
        except Exception:  # noqa: BLE001
            stop.wait(2.0)


def _ticker_loop() -> None:
    stop: threading.Event = _threads["stop"]
    while not stop.is_set():
        tick()
        stop.wait(20.0)


def start() -> bool:
    """Start the worker and the timer (daemon threads, idempotent). Called from each service's start-up; harmless when the gate is off (the loops idle)."""
    with _lock:
        if _threads["worker"] is not None and _threads["worker"].is_alive():
            return False
        _threads["stop"].clear()
        w = threading.Thread(target=_worker_loop, name="tm-gate-worker", daemon=True)
        t = threading.Thread(target=_ticker_loop, name="tm-gate-timer", daemon=True)
        _threads["worker"], _threads["ticker"] = w, t
        w.start(); t.start()
    return True


def stop() -> None:
    _threads["stop"].set()


# ------------------------------------------------------------------ status

def status() -> Dict[str, Any]:
    with _lock:
        st = dict(_stats)
        recent = list(reversed(_recent))[:30]
        scans = list(reversed(_scans))[:10]
        cached = len(_verdicts)
        active = [{"symbol": k[0], "side": k[1], "stop_pct": a["stop"], "target_pct": a["target"], "age_s": int(time.monotonic() - a["at"]), "source": a["source"]}
                  for k, a in sorted(_active.items(), key=lambda kv: -kv[1]["at"])][:20]
        pending = len(_queued)
    checks = st.get("gate_checks", 0)
    return {"service": _adapter.get("service"), "mode": mode(), "block": list(_block_codes()), "auto_enabled": bool(_cfg("TM_AUTO_ENABLED", True)),
            "timer": {"prefetch": str(_cfg("TM_AUTO_PREFETCH_IST", "09:05")), "first_scan": str(_cfg("TM_AUTO_FIRST_IST", "09:16")),
                      "last_scan": str(_cfg("TM_AUTO_LAST_IST", "14:30")), "every_min": int(_num(_cfg("TM_AUTO_INTERVAL_MIN", 15), 15, 5, 120)),
                      "threads_running": bool(_threads["worker"] is not None and _threads["worker"].is_alive())},
            "settings": {"sims": int(_num(_cfg("TM_GATE_SIMS", 3000), 3000, 1000, 20000)), "inline_budget_s": _num(_cfg("TM_GATE_INLINE_BUDGET_S", 2.5), 2.5, 0.0, 15.0), "window_budget_s": _num(_cfg("TM_GATE_WINDOW_BUDGET_S", 8.0), 8.0, 0.0, 120.0), "window_spent_s": round(_window_spent(), 2),
                         "ttl_s": _verdict_ttl(), "pick_nudge_pts": _num(_cfg("TM_PICK_NUDGE_PTS", 6.0), 6.0, 0.0, 30.0)},
            "counters": st, "refusal_rate": round(st.get("rejects", 0) / checks, 3) if checks else None,
            "cached_verdicts": cached, "pending_jobs": pending, "active": active, "recent_scans": scans, "recent_checks": recent,
            "note": "A refusal needs the simulation AND the stock's own recent sessions to be negative after charges. Missing or slow data is no opinion and never blocks."}
