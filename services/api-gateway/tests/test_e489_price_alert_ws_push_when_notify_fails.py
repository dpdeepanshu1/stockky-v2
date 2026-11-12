"""E489: `_deliver_triggered_price_alerts` skipped the WebSocket push of an individually-sent alert whose notification failed.

E472's docstring: "every alert still gets its WebSocket push". The digest branch did that (its push loop runs after the try); the individual branch
pushed inside the same `try`, after the notification call, so a notification service that is down (or a wake call that raises) meant the first
`max_individual` alerts reached neither the phone nor the open browser tabs, although their 15 minute cooldown was already spent. The push now runs
after the try / except for each alert.

No fastapi needed (function lifted from main.py by AST, run against fakes). Runs with pytest or `python3 tests/test_e489_...py`."""
from __future__ import annotations

import ast
import asyncio
import logging
import os
import types

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TREE = ast.parse(open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read())


def _deliver(post_behaviour, ws_raises=False):
    fn = [n for n in _TREE.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_deliver_triggered_price_alerts"]
    assert len(fn) == 1
    pushed, posted = [], []

    class WS:
        async def broadcast(self, channel, msg):
            if ws_raises:
                raise RuntimeError("ws down")
            pushed.append((channel, msg["symbol"]))

    def post(url, json=None, timeout=None):
        posted.append(json["title"])
        if post_behaviour == "raise":
            raise RuntimeError("notify down")
        return types.SimpleNamespace(status_code=503 if post_behaviour == "503" else 200)

    ns = dict(asyncio=asyncio, logger=logging.getLogger("e489"), ws_manager=WS(), httpx=types.SimpleNamespace(post=post), NOTIFICATION_URL="http://n",
              _wake_notification_service=lambda: None, _price_alert_digest_text=lambda rest: "digest", _PRICE_ALERT_INDIVIDUAL_PER_TICK=5)
    exec(compile(ast.Module(body=fn, type_ignores=[]), "main.py", "exec"), ns)
    return ns["_deliver_triggered_price_alerts"], pushed, posted


def _alerts(n):
    return [{"symbol": f"S{i}", "current_price": 10 + i, "target_price": 9, "direction": "above", "id": i} for i in range(n)]


def test_a_failed_notification_still_pushes_over_the_websocket():
    f, pushed, posted = _deliver("raise")
    out = asyncio.run(f(_alerts(3), 5))
    assert out == {"individual": 0, "digest": 0, "failed": 3}
    assert [s for _c, s in pushed] == ["S0", "S1", "S2"] and all(c == "alerts" for c, _s in pushed)


def test_an_http_error_from_the_notification_service_is_a_failure_with_a_push():
    f, pushed, _p = _deliver("503")
    out = asyncio.run(f(_alerts(2), 5))
    assert out["failed"] == 2 and out["individual"] == 0 and len(pushed) == 2


def test_the_normal_path_is_unchanged():
    f, pushed, posted = _deliver("ok")
    out = asyncio.run(f(_alerts(7), 5))
    assert out == {"individual": 5, "digest": 2, "failed": 0}
    assert len(posted) == 6 and posted[-1].startswith("Price Alerts")
    assert [s for _c, s in pushed] == [f"S{i}" for i in range(7)]


def test_a_failing_websocket_does_not_stop_the_rest():
    f, pushed, posted = _deliver("ok", ws_raises=True)
    out = asyncio.run(f(_alerts(3), 5))
    assert out == {"individual": 3, "digest": 0, "failed": 0} and pushed == []


if __name__ == "__main__":
    import traceback
    failed = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("ok  ", _n)
            except Exception:
                failed += 1
                print("FAIL", _n)
                traceback.print_exc()
    raise SystemExit(1 if failed else 0)
