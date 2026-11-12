"""E493: three route bodies in `gateway_support/bound_routes_j.py`.

1. `scan_watchlist` built its per-horizon boards with a closure that did arithmetic / `>=` on the decision service's scores as sent, and sorted
   `all_results` on a raw `combined_score`: one text score raised TypeError at the very end of the scan and the response was lost. It now uses
   `_scan_horizon_picks` (the E472 full-scan twin, same rules, scores coerced) and `_score_sort_key`.
2. `send_picks_to_telegram` parsed prices with a bare float(): NaN / inf passed and printed as "Current: ₹nan" in the Telegram message.
3. `catalyst_alert_scan` sorted its picks on `-(score or 0)` (a text score raised TypeError after the whole universe was scored; a NaN one scrambled
   the order) and, when the notify POST failed, called the blocking `send_picks_to_telegram` inline in the event loop (up to 15 s per part).

No fastapi / httpx needed (real handlers lifted by AST, run against fakes). Runs with pytest or `python3 tests/test_e493_...py`."""
from __future__ import annotations

import ast
import asyncio
import logging
import math
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_e492_watchlist_scan_and_outcome_recorder import _scan, _row, _BRJ, _main_fn   # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _handler(name):
    build = next(n for n in _BRJ.body if isinstance(n, ast.FunctionDef) and n.name == "build_bound_routes_j")
    fn = next(n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    fn.decorator_list = []
    return fn


# ── 1. watchlist boards ──────────────────────────────────────────────────────

def test_a_text_score_no_longer_loses_the_whole_watchlist_scan():
    out = _scan({"AAA": _row("AAA", news_score=50, combined_score="abc", horizons={"short": {"score": "x"}}),
                 "BBB": _row("BBB", news_score=50, combined_score=60, decision="PREPARE TO BUY")})
    assert out["scanned"] == 2 and out["errors"] == []
    assert [r["symbol"] for r in out["recommendations_short"]][0] == "BBB"


def test_boards_keep_their_rules_for_good_scores():
    rows = {"S1": _row("S1", news_score=50, combined_score=70, decision="BUY NOW"),
            "S2": _row("S2", news_score=50, combined_score=80, decision="DO NOT BUY"),
            "S3": _row("S3", news_score=50, combined_score=55, decision="HOLD")}
    out = _scan(rows)
    syms = [r["symbol"] for r in out["recommendations_short"]]
    assert "S2" not in syms and syms[0] == "S1" and "S3" in syms          # DO NOT BUY never promoted; 55 >= 54 qualifies
    assert out["final_verdict"]["short_count"] == len(syms)


# ── 2. telegram message ──────────────────────────────────────────────────────

def _send(recs, msg_type="top5"):
    sent = []

    class HTTPException(Exception):
        def __init__(self, status_code, detail):
            self.status_code, self.detail = status_code, detail

    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"delivered": True}

    ref = types.SimpleNamespace(
        HTTPException=HTTPException, _wake_notification_service=lambda: True, math=math, NOTIFICATION_URL="http://n",
        httpx=types.SimpleNamespace(post=lambda url, json=None, timeout=None: sent.append(json) or _R(), HTTPError=Exception))
    ns = {"ref": ref}
    exec(compile(ast.Module(body=[_handler("send_picks_to_telegram")], type_ignores=[]), "bound_routes_j.py", "exec"), ns)
    ns["send_picks_to_telegram"]({"recommendations": recs, "type": msg_type})
    return "\n".join(m["message"] for m in sent)


def test_nan_and_inf_prices_are_left_out_of_the_telegram_message():
    msg = _send([{"symbol": "AAA", "decision": "BUY NOW", "combined_score": 70, "close": float("nan"), "target": float("inf"), "stop_loss": float("-inf"),
                  "entry_range": {"low": float("nan"), "high": 101.0}}])
    low = msg.lower()
    assert "nan" not in low and "inf" not in low and "AAA" in msg
    assert "Current" not in msg and "Target" not in msg and "Stop" not in msg and "Entry" not in msg


def test_good_prices_are_unchanged_in_the_message():
    msg = _send([{"symbol": "AAA", "decision": "BUY NOW", "combined_score": 70, "close": "100", "target": 110, "stop_loss": 95.5,
                  "entry_range": {"low": 99, "high": 101}}])
    assert "Current: ₹100.00" in msg and "Entry: ₹99.00 – ₹101.00" in msg and "Target: ₹110.00 (+10.0%)" in msg and "Stop: ₹95.50" in msg


# ── 3. catalyst sweep ────────────────────────────────────────────────────────

def _catalyst(items, telegram):
    store = {}

    async def hot(force=True, max_symbols=None, progress_cb=None):
        return {"universe_size": len(items), "news_driven": items}

    async def warm(*a, **k):
        return None

    class _Client:
        async def post(self, *a, **k):
            raise RuntimeError("notify down")

    ref = types.SimpleNamespace(
        BackgroundTasks=object, asyncio=asyncio, os=types.SimpleNamespace(environ={}), datetime=datetime, IST=IST, logger=logging.getLogger("e493"),
        _env_int=lambda n, d: d, _redis_get=lambda k: store.get(k), _redis_set=lambda k, v, ttl=None: store.__setitem__(k, v),
        stockky_hot_stocks=hot, _warm_upstream_services=warm, _get_http_client=lambda: _Client(), NOTIFICATION_URL="http://n/",
        send_picks_to_telegram=telegram, _hot_float=_main_fn("_hot_float", {"math": math}))
    ns = {"ref": ref}
    exec(compile(ast.Module(body=[_handler("catalyst_alert_scan")], type_ignores=[]), "bound_routes_j.py", "exec"), ns)

    async def run():
        ticks = {"n": 0}

        async def ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks["n"] += 1

        t = asyncio.create_task(ticker())
        res = await ns["catalyst_alert_scan"](background_tasks=None, force=True, notify=True, sync=True, batch_size=0, restart=False)
        t.cancel()
        return res, ticks["n"]

    return asyncio.run(run())


def _item(sym, score, dec="BUY NOW"):
    return {"symbol": sym, "decision": dec, "score": score, "reasons": ["r"]}


def test_a_text_or_nan_score_no_longer_fails_the_catalyst_sweep():
    res, _ = _catalyst([_item("AAA", "high"), _item("BBB", 80), _item("CCC", float("nan")), _item("DDD", 90), "junk"], lambda p: None)
    assert res["ok"] is True and res["status"] == "done"
    assert [p["symbol"] for p in res["picks"]][:2] == ["DDD", "BBB"]        # highest real score first; text / NaN count as 0
    assert res["actionable_count"] == 4


def test_the_blocking_telegram_fallback_no_longer_freezes_the_event_loop():
    calls = []

    def slow(payload):
        calls.append(payload)
        time.sleep(0.4)

    res, ticks = _catalyst([_item("AAA", 70)], slow)
    assert calls and res["notified"] is True
    assert ticks >= 10, ticks          # ~40 ticks when the call runs in a worker thread, 0-1 when it blocks the loop


if __name__ == "__main__":   # pragma: no cover
    failed = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("ok  ", _n)
            except BaseException as _e:   # noqa: BLE001
                failed += 1
                print("FAIL", _n, type(_e).__name__, _e)
    sys.exit(1 if failed else 0)
