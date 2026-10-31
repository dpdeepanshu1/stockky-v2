"""entry_engine/flow_gate.py - intraday order-flow tape gate before a BUY (E268, 30 Oct 2026).

Asks market-data GET /flow/{symbol}?side=LONG once per candidate and refuses the entry when the tape is RELIABLE and at least FLOW_GATE_MIN_AGAINST of
the six order-flow checks (value area, HVN wall, aggressor / stacked imbalance, absorption / exhaustion / book lean, delta, big prints) are against a long.

HONESTY: the tape is an ESTIMATE built from quote snapshots (cumulative volume + VWAP), not a trade feed. This gate can only say NO, and only on a reliable
tape. Unreliable tape / market-data down / slow call / any error = no opinion, the entry goes on.
FLOW_GATE_MODE live | log | off. The verdict is stamped on decision.flow_json["tape"] (FLOW_RECORD=0 stops that). Never raises.

httpx and market_feed are imported inside fetch_verdict, so the pure parts (mode, decide, attach, check with a patched fetch) import without them.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional
from urllib.parse import quote

import config

logger = logging.getLogger(__name__)

_CACHE_TTL_S = 5.0
_FAIL_TTL_S = 15.0
_CACHE_CAP = 1000
_cache: dict = {}          # (symbol, side) -> (monotonic, verdict | None)
_stats = {"calls": 0, "blocks": 0, "unreliable": 0, "no_tape": 0, "errors": 0, "cache_hits": 0}
_TAPE_KEYS = ("reliable", "block", "against", "for_count", "checks", "buy_pct_10m", "delta_agrees", "aggressor", "location", "node", "coverage_pct")


def mode() -> str:
    m = str(getattr(config, "FLOW_GATE_MODE", "off") or "off").strip().lower()
    return m if m in ("live", "log", "off") else "off"


async def fetch_verdict(symbol: str, side: str = "LONG") -> Optional[dict]:
    """One cached call to market-data. None on any failure (no opinion)."""
    key = (str(symbol).upper(), side)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < (_CACHE_TTL_S if hit[1] is not None else _FAIL_TTL_S):
        _stats["cache_hits"] += 1
        return hit[1]
    out: Optional[dict] = None
    try:
        import httpx
        from market_feed.feed import MARKET_DATA_URL
        timeout = float(getattr(config, "FLOW_GATE_TIMEOUT_S", 1.0))
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"{MARKET_DATA_URL}/flow/{quote(str(symbol), safe='&')}",
                                 params={"side": side, "min_against": int(getattr(config, "FLOW_GATE_MIN_AGAINST", 2))})
        if r.status_code == 200:
            j = r.json()
            out = j if isinstance(j, dict) else None
        _stats["calls"] += 1
    except Exception as e:  # noqa: BLE001 - no opinion
        _stats["errors"] += 1
        logger.debug("flow_gate fetch failed for %s: %s", symbol, type(e).__name__)
    if len(_cache) >= _CACHE_CAP:
        _cache.clear()
    _cache[key] = (now, out)
    return out


def decide(verdict: Optional[dict]) -> Optional[str]:
    """Pure: the refusal reason, or None. Counts what it saw."""
    if not isinstance(verdict, dict) or not verdict.get("available"):
        _stats["no_tape"] += 1
        return None
    if not verdict.get("reliable"):
        _stats["unreliable"] += 1
        return None
    if verdict.get("block"):
        _stats["blocks"] += 1
        return str(verdict.get("reason") or "ORDER_FLOW:against")
    return None


def attach(flow_json, verdict: Optional[dict]) -> Optional[str]:
    """Merge the verdict under flow_json["tape"]; returns the new JSON text (the input when there is nothing to add)."""
    if not getattr(config, "FLOW_RECORD", True) or not isinstance(verdict, dict) or not verdict.get("available"):
        return flow_json
    try:
        d = json.loads(flow_json) if isinstance(flow_json, str) and flow_json else (dict(flow_json) if isinstance(flow_json, dict) else {})
        if not isinstance(d, dict):
            d = {}
        d["tape"] = {k: verdict.get(k) for k in _TAPE_KEYS if k in verdict}
        return json.dumps(d, separators=(",", ":"), default=str)
    except Exception:  # noqa: BLE001
        return flow_json


async def check(decision, symbol: str, side: str = "LONG") -> Optional[str]:
    """Gate 4d-2: stamp the verdict on the decision and return the refusal reason (live mode only), else None. Never raises."""
    try:
        m = mode()
        if m == "off":
            return None
        verdict = await fetch_verdict(symbol, side)
        try:
            decision.flow_json = attach(getattr(decision, "flow_json", None), verdict)
        except Exception:  # noqa: BLE001
            pass
        reason = decide(verdict)
        return reason if (reason and m == "live") else None
    except Exception:  # noqa: BLE001
        _stats["errors"] += 1
        return None


def _exit_cfg(name, default):
    return getattr(config, name, default)


_exit_stats = {"calls": 0, "tighten": 0, "hold": 0, "errors": 0, "applied": 0}
_last_exit_move: dict = {}


def exit_mode() -> str:
    m = str(getattr(config, "FLOW_EXIT_MODE", "off") or "off").strip().lower()
    return m if m in ("live", "log", "off") else "off"


def exit_throttled(key) -> bool:
    """True when a tape move for `key` happened less than FLOW_EXIT_MIN_INTERVAL_S ago."""
    import time as _t
    last = _last_exit_move.get(key)
    return bool(last is not None and _t.monotonic() - last < float(_exit_cfg("FLOW_EXIT_MIN_INTERVAL_S", 20.0)))


def exit_mark(key) -> None:
    import time as _t
    if len(_last_exit_move) > 2000:
        _last_exit_move.clear()
    _last_exit_move[key] = _t.monotonic()


def _decide_exit(verdict, side, entry, stop, target, ltp, tick):
    from exit_engine import tape_exit
    return tape_exit.decide(
        side=side, entry=entry, stop=stop, target=target, ltp=ltp, tape=verdict, tick=tick,
        min_against=int(config.FLOW_EXIT_MIN_AGAINST), min_profit_pct=float(_exit_cfg("FLOW_EXIT_MIN_PROFIT_PCT", 0.3)),
        near_stop_frac=float(_exit_cfg("FLOW_EXIT_NEAR_STOP_FRAC", 0.5)), gap_pct=float(_exit_cfg("FLOW_EXIT_STOP_GAP_PCT", 0.25)),
        min_target_pct=float(_exit_cfg("FLOW_EXIT_MIN_TARGET_PCT", 0.5)))

async def exit_check(symbol: str, side: str, *, entry, stop, target, ltp, tick: float = 0.05) -> Optional[dict]:
    """E270: tape decision for an OPEN trade (side = the trade's own side). Returns the tape_exit decision dict (action HOLD|TIGHTEN), or None when the
    switch is off / no verdict. In `log` mode the caller must only log. Never raises."""
    try:
        if exit_mode() == "off":
            return None
        _exit_stats["calls"] += 1
        verdict = await _fetch_exit_verdict(symbol, side)
        if verdict is None:
            return None
        d = _decide_exit(verdict, side, entry, stop, target, ltp, tick)
        _exit_stats["tighten" if d.get("action") == "TIGHTEN" else "hold"] += 1
        return d
    except Exception:  # noqa: BLE001
        _exit_stats["errors"] += 1
        return None


async def _fetch_exit_verdict(symbol: str, side: str) -> Optional[dict]:
    """One uncached call with the short FLOW_EXIT_TIMEOUT_S (an exit needs the live tape, not a 5 s old one)."""
    try:
        import httpx
        from market_feed.feed import MARKET_DATA_URL
        async with httpx.AsyncClient(timeout=float(_exit_cfg("FLOW_EXIT_TIMEOUT_S", 0.6))) as client:
            r = await client.get(f"{MARKET_DATA_URL}/flow/{quote(str(symbol), safe='&')}",
                           params={"side": "SHORT" if str(side).upper() in ("SHORT", "SELL") else "LONG", "min_against": int(config.FLOW_EXIT_MIN_AGAINST)})
        if r.status_code == 200:
            j = r.json()
            return j if isinstance(j, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("flow exit fetch failed for %s: %s", symbol, type(e).__name__)
    return None


def exit_applied() -> None:
    """E271: count one trade whose stop and/or target was really moved by the tape (not just decided)."""
    _exit_stats["applied"] += 1


def stats() -> dict:
    return {**_stats, "mode": mode(), "exit": {**_exit_stats, "exit_mode": exit_mode()}}


def _reset_for_tests() -> None:
    _cache.clear()
    _last_exit_move.clear()
    for k in _exit_stats:
        _exit_stats[k] = 0
    for k in _stats:
        _stats[k] = 0

