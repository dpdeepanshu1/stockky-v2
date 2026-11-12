"""E476: WebSocket frames were serialised with `json.dumps` (NaN allowed), so one NaN / inf anywhere in a payload (a scan result carries
decision-service scores that are not coerced on the full-scan path) became the bare token `NaN`, which the browser's `JSON.parse` rejects:
the "done" push of the whole scan was lost. Now `_ws_dumps` (NaN / inf -> null) is used by `ConnectionManager.broadcast` and every send in
the WS handler (`bound_routes_m.py`). Needs fastapi (imports main). Runs with pytest."""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import main as gw  # noqa: E402

NAN, INF = float("nan"), float("inf")


def _strict(text):
    return json.loads(text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def test_ws_dumps_turns_nan_and_inf_into_null_at_any_depth():
    out = _strict(gw._ws_dumps({"a": NAN, "b": [1.5, INF, {"c": -INF}], "d": "x", "e": None, "f": 0.0}))
    assert out == {"a": None, "b": [1.5, None, {"c": None}], "d": "x", "e": None, "f": 0.0}


def test_ws_dumps_normal_payload_is_unchanged_and_default_str_still_applies():
    from datetime import datetime
    p = {"type": "quote", "price": 101.5, "n": 3, "ok": True, "t": datetime(2026, 11, 12)}
    out = _strict(gw._ws_dumps(p))
    assert out["price"] == 101.5 and out["n"] == 3 and out["ok"] is True and out["t"].startswith("2026-11-12")


def test_broadcast_sends_valid_json_when_the_payload_holds_nan():
    sent = []

    class WS:
        async def send_text(self, m):
            sent.append(m)

    mgr = gw.ConnectionManager()
    ws = WS()
    mgr.active.append(ws) if isinstance(mgr.active, list) else mgr.active.add(ws)
    mgr.subs[id(ws)] = {"scan:t1"}
    asyncio.run(mgr.broadcast("scan:t1", {"type": "scan_status", "result": {"rows": [{"combined_score": NAN}]}}))
    assert len(sent) == 1
    out = _strict(sent[0])
    assert out["channel"] == "scan:t1" and out["result"]["rows"][0]["combined_score"] is None


def test_the_ws_handler_no_longer_uses_raw_json_dumps():
    src = open(os.path.join(_SERVICE, "gateway_support", "bound_routes_m.py"), encoding="utf-8").read()
    assert not re.search(r"ref\.json\.dumps\(", src)
    assert src.count("ref._ws_dumps(") >= 9
