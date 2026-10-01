"""tests/test_main_notification_training_proxy.py — coverage for api-gateway/main.py, slice 9 (lines 6451-6810)

Pass 67. The notification-service proxy routes and the training-service proxy routes:

* `GET /notifications/health`, `GET|POST /notifications/config`, `DELETE /notifications/config/{channel}`;
* `GET|POST /notifications/call/me` (wake-first, 70 s budget, 504 vs 502);
* `POST /notifications/test` (wake-first, 75 s budget, 504 vs 502);
* `POST /notifications/send-picks` (top-5 message, "all" message split into <= 4000-char parts, the
  per-pick formatter);
* `GET /training/status`, `POST /training/train`, `GET /training/score/{symbol}`;
* `/training/{path:path}` catch-all proxy (heavy-path timeouts, header / body / query forwarding, JSON
  and non-JSON upstream replies, 504 / 502 mapping).

Everything downstream is faked: the sync `httpx.get / post / delete` used by the notification routes and
the shared async client (`_get_http_client`) used by the training routes. Nothing touches the network.
Findings are pinned as current behaviour and marked ``NOT FIXED``.

Run from services/api-gateway:
    python3 -m pytest tests/test_main_notification_training_proxy.py -v
"""
from __future__ import annotations

import json
import os

import httpx
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)

from fastapi.testclient import TestClient

NOTIF = "http://notif.local"
TRAIN = "http://train.local"


class Resp:
    def __init__(self, status=200, data=None, text="", json_raises=False):
        self.status_code = status
        self._data = {} if data is None else data
        self.text = text
        self._json_raises = json_raises

    def json(self):
        if self._json_raises:
            raise ValueError("not json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=self)


@pytest.fixture
def client():
    return TestClient(gw.app, raise_server_exceptions=False)


# ═════════════════════════════════════════════════════════════════════════════
# Notification proxies
# ═════════════════════════════════════════════════════════════════════════════

class NotifEnv:
    def __init__(self):
        self.calls = []        # (method, url, kwargs)
        self.handlers = {}     # (METHOD, path-suffix) -> Resp | Exception | callable(kwargs)

    def on(self, method, suffix, out):
        self.handlers[(method, suffix)] = out

    def sent(self, suffix="/notify"):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith(suffix)]

    def _dispatch(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        for (m, suffix), out in self.handlers.items():
            if m == method and url.endswith(suffix):
                if callable(out):
                    out = out(kwargs)
                if isinstance(out, Exception):
                    raise out
                return out
        return Resp(200, {})


@pytest.fixture
def nenv(monkeypatch):
    env = NotifEnv()
    monkeypatch.setattr(gw, "NOTIFICATION_URL", NOTIF)
    monkeypatch.setattr(httpx, "get", lambda url, **kw: env._dispatch("GET", url, kw))
    monkeypatch.setattr(httpx, "post", lambda url, **kw: env._dispatch("POST", url, kw))
    monkeypatch.setattr(httpx, "delete", lambda url, **kw: env._dispatch("DELETE", url, kw))
    return env


class TestNotificationHealthAndConfig:
    def test_health_passes_the_body_through(self, nenv, client):
        nenv.on("GET", "/health", Resp(200, {"status": "ok"}))
        r = client.get("/notifications/health")
        assert r.status_code == 200 and r.json() == {"status": "ok"}
        assert nenv.calls == [("GET", f"{NOTIF}/health", {"timeout": 10})]

    @pytest.mark.parametrize("failure", [Resp(500), httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
    def test_health_failures_are_a_502(self, nenv, client, failure):
        nenv.on("GET", "/health", failure)
        r = client.get("/notifications/health")
        assert r.status_code == 502
        assert r.json()["detail"].startswith("Notification service unreachable: ")

    def test_health_non_json_body_is_an_unhandled_500(self, nenv, client):
        # NOT FIXED: only httpx.HTTPError is caught, so a 200 with a non-JSON body (a proxy error page)
        # escapes as a generic 500 instead of the 502 used for every other upstream failure.
        nenv.on("GET", "/health", Resp(200, json_raises=True))
        assert client.get("/notifications/health").status_code == 500

    def test_get_config_passes_the_body_through(self, nenv, client):
        nenv.on("GET", "/config", Resp(200, {"telegram": True}))
        r = client.get("/notifications/config")
        assert r.json() == {"telegram": True}
        assert nenv.calls == [("GET", f"{NOTIF}/config", {"timeout": 10})]

    def test_get_config_upstream_failure_is_a_502(self, nenv, client):
        nenv.on("GET", "/config", Resp(503))
        assert client.get("/notifications/config").status_code == 502

    def test_set_config_wakes_then_posts_only_the_fields_that_were_sent(self, nenv, client):
        nenv.on("POST", "/config", Resp(200, {"ok": True}))
        r = client.post("/notifications/config", json={"telegram_chat_id": "42", "slack_webhook_url": None})
        assert r.status_code == 200 and r.json() == {"ok": True}
        assert nenv.calls[0] == ("GET", f"{NOTIF}/health", {"timeout": 8})
        assert nenv.calls[1] == ("POST", f"{NOTIF}/config", {"json": {"telegram_chat_id": "42"}, "timeout": 20})

    def test_set_config_empty_body_still_posts_an_empty_payload(self, nenv, client):
        client.post("/notifications/config", json={})
        assert nenv.calls[1][2]["json"] == {}

    def test_set_config_ignores_a_failing_wake_call(self, nenv, client):
        nenv.on("GET", "/health", RuntimeError("cold"))          # not even an httpx error
        nenv.on("POST", "/config", Resp(200, {"ok": True}))
        assert client.post("/notifications/config", json={"callmebot_user": "u"}).json() == {"ok": True}

    def test_set_config_upstream_failure_is_a_502(self, nenv, client):
        nenv.on("POST", "/config", httpx.ConnectError("refused"))
        r = client.post("/notifications/config", json={"callmebot_user": "u"})
        assert r.status_code == 502 and "Notification service unreachable" in r.json()["detail"]

    def test_set_config_rejects_a_non_string_field(self, nenv, client):
        assert client.post("/notifications/config", json={"telegram_chat_id": {"x": 1}}).status_code == 422
        assert nenv.calls == []

    def test_delete_channel(self, nenv, client):
        nenv.on("DELETE", "/config/telegram", Resp(200, {"deleted": "telegram"}))
        r = client.delete("/notifications/config/telegram")
        assert r.json() == {"deleted": "telegram"}
        assert nenv.calls == [("DELETE", f"{NOTIF}/config/telegram", {"timeout": 10})]

    def test_delete_channel_upstream_failure_is_a_502(self, nenv, client):
        nenv.on("DELETE", "/config/slack", Resp(404))
        assert client.delete("/notifications/config/slack").status_code == 502


class TestCallMe:
    @pytest.mark.parametrize("method", ["get", "post"])
    def test_both_methods_wake_first_then_post_with_the_message(self, nenv, client, method):
        nenv.on("POST", "/call/me", Resp(200, {"called": True}))
        r = getattr(client, method)("/notifications/call/me", params={"message": "hello"})
        assert r.status_code == 200 and r.json() == {"called": True}
        assert nenv.calls[0] == ("GET", f"{NOTIF}/health", {"timeout": 10})
        assert nenv.calls[1] == ("POST", f"{NOTIF}/call/me", {"params": {"message": "hello"}, "timeout": 70})

    def test_default_message(self, nenv, client):
        client.get("/notifications/call/me")
        assert nenv.calls[1][2]["params"] == {"message": "Stockky test call alert"}

    def test_empty_message_falls_back_to_the_default(self, nenv, client):
        client.get("/notifications/call/me", params={"message": ""})
        assert nenv.calls[1][2]["params"] == {"message": "Stockky test call alert"}

    def test_failing_wake_is_ignored(self, nenv, client):
        nenv.on("GET", "/health", httpx.ConnectError("cold"))
        assert client.get("/notifications/call/me").status_code == 200

    def test_timeout_is_a_504_with_the_slow_network_hint(self, nenv, client):
        nenv.on("POST", "/call/me", httpx.ReadTimeout("slow"))
        r = client.get("/notifications/call/me")
        assert r.status_code == 504 and "CallMeBot timed out" in r.json()["detail"]

    @pytest.mark.parametrize("failure", [Resp(500), httpx.ConnectError("refused")])
    def test_other_failures_are_a_502(self, nenv, client, failure):
        nenv.on("POST", "/call/me", failure)
        r = client.post("/notifications/call/me")
        assert r.status_code == 502 and r.json()["detail"].startswith("CallMeBot unreachable: ")


class TestNotificationTest:
    def test_wakes_first_then_posts_with_the_long_timeout(self, nenv, client):
        nenv.on("POST", "/test", Resp(200, {"results": []}))
        r = client.post("/notifications/test")
        assert r.status_code == 200 and r.json() == {"results": []}
        assert nenv.calls == [
            ("GET", f"{NOTIF}/health", {"timeout": 10}),
            ("POST", f"{NOTIF}/test", {"timeout": 75}),
        ]

    def test_failing_wake_is_ignored(self, nenv, client):
        nenv.on("GET", "/health", RuntimeError("cold"))
        assert client.post("/notifications/test").status_code == 200

    def test_timeout_is_a_504_not_a_misleading_unreachable(self, nenv, client):
        nenv.on("POST", "/test", httpx.ReadTimeout("slow"))
        r = client.post("/notifications/test")
        assert r.status_code == 504 and "Notification test timed out" in r.json()["detail"]

    @pytest.mark.parametrize("failure", [Resp(502), httpx.ConnectError("refused")])
    def test_other_failures_are_a_502(self, nenv, client, failure):
        nenv.on("POST", "/test", failure)
        r = client.post("/notifications/test")
        assert r.status_code == 502 and "Notification service unreachable" in r.json()["detail"]


# ── send-picks ──────────────────────────────────────────────────────────────

def _pick(symbol="AAA", **kw):
    d = {"symbol": symbol, "decision": "BUY NOW", "combined_score": 72}
    d.update(kw)
    return d


def _messages(nenv):
    return [c[2]["json"]["message"] for c in nenv.sent()]


class TestSendPicksValidation:
    @pytest.mark.parametrize("payload", [{}, {"recommendations": []}, {"recommendations": None}])
    def test_missing_or_empty_recommendations_is_a_400(self, nenv, client, payload):
        r = client.post("/notifications/send-picks", json=payload)
        assert r.status_code == 400 and r.json()["detail"] == "No recommendations provided"
        assert nenv.calls == []

    def test_non_object_body_is_a_422(self, nenv, client):
        assert client.post("/notifications/send-picks", json=["x"]).status_code == 422

    def test_service_is_woken_before_sending(self, nenv, client):
        nenv.on("POST", "/notify", Resp(200, {"delivered": True}))
        client.post("/notifications/send-picks", json={"recommendations": [_pick()]})
        assert nenv.calls[0] == ("GET", f"{NOTIF}/health", {"timeout": 5})


class TestSendPicksTop5:
    def test_success_sends_one_message_with_the_top_five(self, nenv, client):
        nenv.on("POST", "/notify", Resp(200, {"delivered": True}))
        recs = [_pick(f"S{i}") for i in range(8)]
        r = client.post("/notifications/send-picks", json={"recommendations": recs})
        assert r.status_code == 200
        assert r.json() == {"success": True, "sent": 5, "message": "Notification sent"}
        (call,) = nenv.sent()
        assert call[2]["timeout"] == 15
        body = call[2]["json"]
        assert body["title"] == "Market Scan Picks" and body["channel"] == "telegram"
        msg = body["message"]
        assert msg.startswith("📊 *Top 5 Picks from Market Scan*\n\n")
        assert "5. *S4*" in msg and "S5" not in msg

    def test_explicit_top5_type_matches_the_default(self, nenv, client):
        nenv.on("POST", "/notify", Resp(200, {"delivered": True}))
        r = client.post("/notifications/send-picks", json={"type": "top5", "recommendations": [_pick()]})
        assert r.json()["sent"] == 1

    def test_undelivered_is_a_502_with_the_service_note(self, nenv, client):
        nenv.on("POST", "/notify", Resp(200, {"delivered": False, "note": "bot blocked"}))
        r = client.post("/notifications/send-picks", json={"recommendations": [_pick()]})
        assert r.status_code == 502
        assert r.json()["detail"] == "Notification service failed to deliver: bot blocked"

    def test_undelivered_without_a_note_uses_the_default(self, nenv, client):
        nenv.on("POST", "/notify", Resp(200, {}))
        r = client.post("/notifications/send-picks", json={"recommendations": [_pick()]})
        assert r.json()["detail"] == "Notification service failed to deliver: Delivery failed"

    @pytest.mark.parametrize("failure", [Resp(500), httpx.ConnectError("refused")])
    def test_http_failures_are_a_502(self, nenv, client, failure):
        nenv.on("POST", "/notify", failure)
        r = client.post("/notifications/send-picks", json={"recommendations": [_pick()]})
        assert r.status_code == 502 and r.json()["detail"].startswith("Notification service failed: ")

    def test_non_dict_notify_body_is_an_unhandled_500(self, nenv, client):
        # NOT FIXED: `data.get(...)` assumes a JSON object; a list body raises AttributeError, which the
        # `except httpx.HTTPError` does not catch.
        nenv.on("POST", "/notify", Resp(200, ["unexpected"]))
        assert client.post("/notifications/send-picks", json={"recommendations": [_pick()]}).status_code == 500

    def test_non_list_recommendations_are_sliced_per_character(self, nenv, client):
        # NOT FIXED: no type check on `recommendations`; a string is sliced and every character
        # becomes an "(invalid pick)" line instead of being rejected with a 400/422.
        nenv.on("POST", "/notify", Resp(200, {"delivered": True}))
        r = client.post("/notifications/send-picks", json={"recommendations": "abcdefg"})
        assert r.status_code == 200 and r.json()["sent"] == 5
        assert _messages(nenv)[0].count("*(invalid pick)*") == 5


class TestSendPicksFormatter:
    @pytest.fixture(autouse=True)
    def _ok(self, nenv):
        nenv.on("POST", "/notify", Resp(200, {"delivered": True}))
        self.nenv = nenv

    def _line_block(self, client, pick):
        client.post("/notifications/send-picks", json={"recommendations": [pick]})
        return _messages(self.nenv)[-1].split("\n\n", 1)[1].rstrip("\n")

    def test_full_pick_layout(self, client):
        block = self._line_block(client, {
            "symbol": "AAA", "decision": "BUY NOW", "combined_score": 81,
            "close": 100, "entry_range": {"low": 99.5, "high": 101}, "target": 110,
            "stop_loss": 95, "holding_period": "2-4 weeks"})
        assert block == "\n".join([
            "1. *AAA* – BUY NOW (Score: 81)",
            "   Current: ₹100.00",
            "   Entry: ₹99.50 – ₹101.00",
            "   Target: ₹110.00 (+10.0%)",
            "   Stop: ₹95.00",
            "   Hold: 2-4 weeks",
        ])

    def test_defaults_for_a_bare_pick(self, client):
        block = self._line_block(client, {})
        assert block == "1. *?* – UNKNOWN (Score: 0)"

    def test_score_falls_back_to_score_then_zero_but_keeps_a_zero_combined(self, client):
        assert "(Score: 55)" in self._line_block(client, {"symbol": "A", "score": 55})
        assert "(Score: 0)" in self._line_block(client, {"symbol": "A", "combined_score": 0, "score": 55})

    def test_non_dict_pick_is_marked_invalid(self, client):
        client.post("/notifications/send-picks", json={"recommendations": ["junk", _pick("BBB")]})
        msg = _messages(self.nenv)[-1]
        assert "1. *(invalid pick)*" in msg and "2. *BBB*" in msg

    def test_numbers_that_do_not_parse_are_dropped(self, client):
        block = self._line_block(client, {"symbol": "A", "close": "abc", "target": [1], "stop_loss": "12.5"})
        assert "Current" not in block and "Target" not in block
        assert "Stop: ₹12.50" in block

    def test_entry_needs_both_bounds_and_a_dict(self, client):
        assert "Entry" not in self._line_block(client, {"symbol": "A", "entry_range": {"low": 1}})
        assert "Entry" not in self._line_block(client, {"symbol": "A", "entry_range": [1, 2]})

    def test_target_without_a_close_shows_zero_upside(self, client):
        assert "Target: ₹50.00 (+0.0%)" in self._line_block(client, {"symbol": "A", "target": 50})

    def test_zero_close_is_printed_and_gives_zero_upside(self, client):
        block = self._line_block(client, {"symbol": "A", "close": 0, "target": 50})
        assert "Current: ₹0.00" in block and "(+0.0%)" in block

    def test_target_below_close_prints_a_plus_minus_sign(self, client):
        # NOT FIXED: the sign is hard-coded, so a negative upside renders as "(+-10.0%)".
        block = self._line_block(client, {"symbol": "A", "close": 100, "target": 90})
        assert "Target: ₹90.00 (+-10.0%)" in block

    def test_holding_period_estimate_is_used_when_holding_period_is_missing(self, client):
        assert "Hold: 3 weeks" in self._line_block(client, {"symbol": "A", "holding_period_estimate": "3 weeks"})

    def test_na_holding_is_hidden(self, client):
        assert "Hold" not in self._line_block(client, {"symbol": "A", "holding_period": "N/A"})

    def test_dict_holding_estimate_is_printed_as_a_python_repr(self, client):
        # NOT FIXED: scan_watchlist attaches `holding_period_estimate` as a dict
        # ({"min_days": .., "max_days": ..}); the formatter just interpolates it.
        est = {"min_days": 3, "max_days": 9}
        assert "Hold: {'min_days': 3, 'max_days': 9}" in self._line_block(
            client, {"symbol": "A", "holding_period_estimate": est})


class TestSendPicksAll:
    @pytest.fixture(autouse=True)
    def _ok(self, nenv):
        nenv.on("POST", "/notify", Resp(200, {"delivered": True}))
        self.nenv = nenv

    def test_short_list_is_a_single_untitled_part(self, client):
        recs = [_pick(f"S{i}") for i in range(7)]
        r = client.post("/notifications/send-picks", json={"type": "all", "recommendations": recs})
        assert r.json() == {"success": True, "sent": 7, "parts": 1, "message": "Notification sent in 1 parts"}
        (msg,) = _messages(self.nenv)
        assert msg.startswith("📊 *All Actionable Stocks (BUY NOW / PREPARE TO BUY)*\n\n1. *S0*")
        assert "Part" not in msg.split("\n")[0] and "7. *S6*" in msg and msg.endswith("\n")

    def test_any_non_top5_type_uses_the_all_path(self, client):
        r = client.post("/notifications/send-picks", json={"type": "whatever", "recommendations": [_pick()]})
        assert r.json()["parts"] == 1

    def test_long_list_is_split_and_numbering_continues_across_parts(self, client):
        recs = [_pick(f"S{i:03d}", close=100, target=120, stop_loss=90, holding_period="2 weeks",
                      entry_range={"low": 99, "high": 101}) for i in range(60)]
        r = client.post("/notifications/send-picks", json={"type": "all", "recommendations": recs})
        body = r.json()
        msgs = _messages(self.nenv)
        assert body["parts"] == len(msgs) > 1 and body["sent"] == 60
        assert body["message"] == f"Notification sent in {len(msgs)} parts"
        for idx, m in enumerate(msgs, 1):
            assert m.startswith(
                f"📊 *All Actionable Stocks (BUY NOW / PREPARE TO BUY)* (Part {idx}/{len(msgs)})\n\n")
            assert len(m) <= 4096                               # Telegram's hard limit
        # picks are numbered once, before chunking, so part 2 continues where part 1 stopped
        assert msgs[0].split("\n\n", 1)[1].startswith("1. *S000*")
        first_of_part_2 = int(msgs[1].split("\n\n", 1)[1].split(".", 1)[0])
        assert first_of_part_2 == msgs[0].count(" – BUY NOW") + 1
        # every pick is sent exactly once
        assert sum(m.count(" – BUY NOW") for m in msgs) == 60
        assert all(f"*S{i:03d}*" in "".join(msgs) for i in range(60))

    def test_part_failure_after_a_delivered_part_is_a_502_without_rollback(self, client):
        # NOT FIXED: part 1 is already in Telegram when part 2 fails, and the 502 does not say so.
        replies = iter([Resp(200, {"delivered": True}), Resp(200, {"delivered": False, "note": "flood"})])
        self.nenv.on("POST", "/notify", lambda kw: next(replies))
        recs = [_pick(f"S{i:03d}", close=100, target=120, stop_loss=90, holding_period="2 weeks",
                      entry_range={"low": 99, "high": 101}) for i in range(60)]
        r = client.post("/notifications/send-picks", json={"type": "all", "recommendations": recs})
        assert r.status_code == 502 and r.json()["detail"] == "Part 2 failed: flood"
        assert len(self.nenv.sent()) == 2

    def test_part_failure_without_a_note_uses_the_default(self, client):
        self.nenv.on("POST", "/notify", Resp(200, {"delivered": False}))
        r = client.post("/notifications/send-picks", json={"type": "all", "recommendations": [_pick()]})
        assert r.status_code == 502 and r.json()["detail"] == "Part 1 failed: Delivery failed"

    def test_http_failure_names_the_part(self, client):
        self.nenv.on("POST", "/notify", httpx.ConnectError("refused"))
        r = client.post("/notifications/send-picks", json={"type": "all", "recommendations": [_pick()]})
        assert r.status_code == 502
        assert r.json()["detail"].startswith("Notification service failed for part 1: ")

    def test_non_dict_notify_body_is_an_unhandled_500(self, client):
        # NOT FIXED: same `data.get` assumption as the top-5 path.
        self.nenv.on("POST", "/notify", Resp(200, ["unexpected"]))
        r = client.post("/notifications/send-picks", json={"type": "all", "recommendations": [_pick()]})
        assert r.status_code == 500

    def test_a_single_oversized_pick_sends_a_header_only_part_first(self, client):
        # NOT FIXED: when one pick alone exceeds the limit the chunker closes the (still empty) current
        # chunk, so an empty part goes out before the oversized one.
        big = _pick("X" * 4100)
        r = client.post("/notifications/send-picks", json={"type": "all", "recommendations": [big]})
        assert r.status_code == 200 and r.json()["parts"] == 2
        first, second = _messages(self.nenv)
        assert first == "📊 *All Actionable Stocks (BUY NOW / PREPARE TO BUY)* (Part 1/2)\n\n\n"
        assert "X" * 4100 in second


# ═════════════════════════════════════════════════════════════════════════════
# Training service proxies
# ═════════════════════════════════════════════════════════════════════════════

class FakeAsyncHTTP:
    """Stand-in for the shared `httpx.AsyncClient` returned by `_get_http_client`."""

    def __init__(self):
        self.calls = []        # (method, url, kwargs)
        self.handlers = {}     # (METHOD, url) -> Resp | Exception | list[Resp|Exception] (consumed in order)

    def on(self, method, url, out):
        self.handlers[(method, url)] = out

    async def _do(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        out = self.handlers.get((method, url), Resp(200, {}))
        if isinstance(out, list):
            out = out.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    async def get(self, url, **kw):
        return await self._do("GET", url, kw)

    async def post(self, url, **kw):
        return await self._do("POST", url, kw)

    async def request(self, method, url, **kw):
        return await self._do(method, url, {"method": method, **kw})


@pytest.fixture
def tenv(monkeypatch):
    env = FakeAsyncHTTP()
    monkeypatch.setattr(gw, "TRAINING_URL", TRAIN)
    monkeypatch.setattr(gw, "_get_http_client", lambda: env)
    return env


class TestTrainingStatus:
    def test_model_status_is_used_first(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/model-status", Resp(200, {"trained": True}))
        r = client.get("/training/status")
        assert r.status_code == 200 and r.json() == {"trained": True}
        assert [c[1] for c in tenv.calls] == [f"{TRAIN}/model-status"]

    def test_404_falls_back_to_api_status(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/model-status", Resp(404))
        tenv.on("GET", f"{TRAIN}/api/status", Resp(200, {"via": "api"}))
        assert client.get("/training/status").json() == {"via": "api"}
        assert [c[1] for c in tenv.calls] == [f"{TRAIN}/model-status", f"{TRAIN}/api/status"]

    def test_trailing_slash_on_the_base_url_is_trimmed(self, tenv, client, monkeypatch):
        monkeypatch.setattr(gw, "TRAINING_URL", TRAIN + "/")
        client.get("/training/status")
        assert tenv.calls[0][1] == f"{TRAIN}/model-status"

    @pytest.mark.parametrize("failure", [Resp(500), httpx.ConnectError("refused"), ValueError("weird")])
    def test_any_failure_is_a_502(self, tenv, client, failure):
        tenv.on("GET", f"{TRAIN}/model-status", failure)
        r = client.get("/training/status")
        assert r.status_code == 502 and r.json()["detail"].startswith("Training service unreachable: ")

    def test_both_404_is_a_502(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/model-status", Resp(404))
        tenv.on("GET", f"{TRAIN}/api/status", Resp(404))
        assert client.get("/training/status").status_code == 502


class TestTrainingTrain:
    def test_api_train_is_used_first(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/api/train", Resp(200, {"started": True}))
        r = client.post("/training/train")
        assert r.json() == {"started": True}
        assert [c[1] for c in tenv.calls] == [f"{TRAIN}/api/train"]

    def test_404_falls_back_to_train(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/api/train", Resp(404))
        tenv.on("POST", f"{TRAIN}/train", Resp(200, {"via": "legacy"}))
        assert client.post("/training/train").json() == {"via": "legacy"}

    def test_upstream_error_keeps_its_status_and_json_detail(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/api/train", Resp(409, {"error": "busy"}, text="busy"))
        r = client.post("/training/train")
        assert r.status_code == 409 and r.json()["detail"] == {"error": "busy"}

    def test_upstream_error_with_a_non_json_body_uses_truncated_text(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/api/train", Resp(500, text="x" * 500, json_raises=True))
        r = client.post("/training/train")
        assert r.status_code == 500 and r.json()["detail"] == "x" * 300

    def test_both_404_surfaces_the_second_404(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/api/train", Resp(404))
        tenv.on("POST", f"{TRAIN}/train", Resp(404, {"detail": "nope"}))
        r = client.post("/training/train")
        assert r.status_code == 404 and r.json()["detail"] == {"detail": "nope"}

    def test_transport_failure_is_a_502(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/api/train", httpx.ConnectError("refused"))
        r = client.post("/training/train")
        assert r.status_code == 502 and r.json()["detail"].startswith("Training service unreachable: ")

    def test_success_with_a_non_json_body_is_a_502(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/api/train", Resp(200, json_raises=True))
        assert client.post("/training/train").status_code == 502


class TestTrainingScore:
    def test_score_passes_through(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/training-score/AAA", Resp(200, {"symbol": "AAA", "score": 71}))
        assert client.get("/training/score/AAA").json() == {"symbol": "AAA", "score": 71}

    def test_404_means_no_score_yet(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/training-score/aaa", Resp(404))
        r = client.get("/training/score/aaa")
        assert r.status_code == 200
        assert r.json() == {"symbol": "AAA", "score": None, "available": False,
                            "message": "No training score for this symbol yet"}

    @pytest.mark.parametrize("failure", [Resp(500), httpx.ReadTimeout("slow")])
    def test_other_failures_are_a_502(self, tenv, client, failure):
        tenv.on("GET", f"{TRAIN}/training-score/AAA", failure)
        r = client.get("/training/score/AAA")
        assert r.status_code == 502 and r.json()["detail"].startswith("Training service unreachable: ")

    def test_trailing_slash_base_url_produces_a_double_slash(self, tenv, client, monkeypatch):
        # NOT FIXED: unlike its siblings this route never `rstrip`s TRAINING_URL.
        monkeypatch.setattr(gw, "TRAINING_URL", TRAIN + "/")
        client.get("/training/score/AAA")
        assert tenv.calls[0][1] == f"{TRAIN}//training-score/AAA"

    def test_an_http_exception_from_the_client_factory_is_passed_through(self, tenv, client, monkeypatch):
        def boom():
            raise gw.HTTPException(status_code=503, detail="pool closed")

        monkeypatch.setattr(gw, "_get_http_client", boom)
        r = client.get("/training/score/AAA")
        assert r.status_code == 503 and r.json()["detail"] == "pool closed"
        # NOT FIXED: /training/status has no such guard, so the same failure is rewritten to a 502.
        r2 = client.get("/training/status")
        assert r2.status_code == 502 and "pool closed" in r2.json()["detail"]

    def test_specific_routes_win_over_the_catch_all(self, tenv, client):
        # /training/score/AAA must not be proxied as /score/AAA through the catch-all
        client.get("/training/score/AAA")
        assert tenv.calls[0][0] == "GET" and "training-score" in tenv.calls[0][1]


class TestTrainingCatchAll:
    @pytest.mark.parametrize("path,timeout", [
        ("portfolio/summary", 90.0),
        ("universe/list", 90.0),
        ("metrics", 90.0),
        ("actionable/commit", 180.0),
        ("model/evaluate", 180.0),
        ("mark-to-market", 180.0),
        ("walk-forward", 180.0),
        ("trade/history", 180.0),
        ("clear-backup", 180.0),
        ("universe/ingest", 180.0),
        ("api/universe", 180.0),
        ("portfolio/deposit", 180.0),
        ("retrain-now", 180.0),            # any path containing "train" is heavy
    ])
    def test_timeout_depends_on_the_path(self, tenv, client, path, timeout):
        client.get(f"/training/{path}")
        assert tenv.calls[0][2]["timeout"] == timeout

    @pytest.mark.parametrize("method", ["get", "post", "put", "delete", "patch", "options"])
    def test_every_method_is_forwarded(self, tenv, client, method):
        r = getattr(client, method)("/training/portfolio/summary")
        assert r.status_code == 200
        assert tenv.calls[0][0] == method.upper()
        assert tenv.calls[0][1] == f"{TRAIN}/portfolio/summary"

    def test_json_body_headers_and_query_are_forwarded(self, tenv, client):
        tenv.on("POST", f"{TRAIN}/portfolio/deposit", Resp(200, {"ok": True}))
        r = client.post("/training/portfolio/deposit?x=1&y=2", json={"amount": 5},
                        headers={"Authorization": "Bearer secret", "Accept-Encoding": "gzip"})
        assert r.json() == {"ok": True}
        kw = tenv.calls[0][2]
        assert kw["headers"]["Accept"] == "application/json"
        assert kw["headers"]["Content-Type"] == "application/json"
        assert "Authorization" not in kw["headers"] and "Accept-Encoding" not in kw["headers"]
        assert json.loads(kw["content"]) == {"amount": 5}
        assert dict(kw["params"]) == {"x": "1", "y": "2"}

    def test_bodyless_request_sends_no_content_and_no_content_type(self, tenv, client):
        client.get("/training/portfolio/summary")
        kw = tenv.calls[0][2]
        assert kw["content"] is None and kw["headers"] == {"Accept": "application/json"}

    def test_upstream_json_is_returned_with_its_status(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/portfolio/summary", Resp(404, {"detail": "no such portfolio"}))
        r = client.get("/training/portfolio/summary")
        assert r.status_code == 404 and r.json() == {"detail": "no such portfolio"}

    def test_upstream_server_error_json_is_passed_through_not_rewritten(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/portfolio/summary", Resp(500, {"detail": "boom"}))
        r = client.get("/training/portfolio/summary")
        assert r.status_code == 500 and r.json() == {"detail": "boom"}

    def test_non_json_error_page_becomes_a_clean_json_detail(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/metrics", Resp(503, text="<html>\x00Bad\x07 Gateway\n\tretry</html>", json_raises=True))
        r = client.get("/training/metrics")
        assert r.status_code == 503
        assert r.json() == {"detail": "<html>Bad Gateway\n\tretry</html>"}

    def test_non_json_text_is_cut_at_800_chars_before_cleaning(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/metrics", Resp(500, text="a" * 2000, json_raises=True))
        assert client.get("/training/metrics").json()["detail"] == "a" * 800

    def test_empty_non_json_body_reports_the_upstream_status(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/metrics", Resp(500, text="", json_raises=True))
        r = client.get("/training/metrics")
        assert r.status_code == 500 and r.json() == {"detail": "Upstream HTTP 500"}

    @pytest.mark.parametrize("status", [200, 204, 302])
    def test_non_json_success_or_redirect_is_rewritten_to_502(self, tenv, client, status):
        tenv.on("GET", f"{TRAIN}/metrics", Resp(status, text="ok", json_raises=True))
        r = client.get("/training/metrics")
        assert r.status_code == 502 and r.json() == {"detail": "ok"}

    def test_leading_slashes_in_the_path_are_normalised(self, tenv, client):
        client.get("/training//portfolio/summary")
        assert tenv.calls[0][1] == f"{TRAIN}/portfolio/summary"

    def test_trailing_slash_base_url_is_trimmed(self, tenv, client, monkeypatch):
        monkeypatch.setattr(gw, "TRAINING_URL", TRAIN + "/")
        client.get("/training/portfolio/summary")
        assert tenv.calls[0][1] == f"{TRAIN}/portfolio/summary"

    def test_timeout_is_a_504_naming_the_path(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/portfolio/summary", httpx.ReadTimeout("slow"))
        r = client.get("/training/portfolio/summary")
        assert r.status_code == 504 and r.json()["detail"] == "Training service timeout for /portfolio/summary"

    def test_connection_error_is_a_502(self, tenv, client):
        tenv.on("GET", f"{TRAIN}/portfolio/summary", httpx.ConnectError("refused"))
        r = client.get("/training/portfolio/summary")
        assert r.status_code == 502 and r.json()["detail"].startswith("Training service unreachable: ")

    def test_unexpected_error_is_logged_and_becomes_a_502(self, tenv, client, caplog):
        tenv.on("GET", f"{TRAIN}/portfolio/summary", RuntimeError("kaboom"))
        with caplog.at_level("ERROR", logger=gw.logger.name):
            r = client.get("/training/portfolio/summary")
        assert r.status_code == 502 and r.json()["detail"] == "Training proxy error: kaboom"
        assert "training proxy error for path=portfolio/summary" in caplog.text
