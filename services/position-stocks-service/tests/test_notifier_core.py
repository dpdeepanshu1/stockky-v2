"""
tests/test_notifier_core.py

Closes the coverage gaps left after session112's fire-and-forget split
(see tests/test_notifier_fire_and_forget.py and
archive/session-notes/SESSION112_NOTIFY_FIRE_AND_FORGET_POSITION_STOCKS_SERVICE_2026-09-25.md).
Confirmed against an actual coverage run: notifier.py sat at 77% (99 stmts,
23 missed: 85-86, 118, 121, 140-143, 211-216, 225-226, 241-247) because every
order-file test monkeypatches notify_sync/notify_fire_and_forget wholesale
(so the real notify_sync body never runs) and no test exercised the dedup
cache's refresh/eviction branches, _deliver_sync's success/not-delivered/
exception branches, or _direct_telegram's no-token and HTML-retry branches
with real (mocked-transport) httpx.

This file covers, directly against the real functions:
  * _should_send — refresh-on-resend after the window expires (moves the
    key to the end rather than treating it as brand new) and LRU eviction
    once the cache exceeds _DEDUP_CACHE_SIZE.
  * notify_sync — called directly (not through a test fixture's monkeypatch)
    for both the duplicate-suppressed short-circuit and the real
    _deliver_sync path.
  * _deliver_sync — service delivers (200 + delivered:true), service says
    not-delivered (200 + delivered:false) falls through to direct Telegram,
    and the service being unreachable (exception) falls through too.
  * _direct_telegram — no TELEGRAM_BOT_TOKEN/CHAT_ID configured, and the
    non-200 HTML-mode-rejected retry path (both a successful plain-text
    retry and a retry that itself raises).
  * _TelegramTokenRedactingFilter — a malformed record (getMessage() raises)
    must not break logging.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_notifier_core.py -q \
        --cov=notifier --cov-report=term-missing
"""
from __future__ import annotations

import logging
import time
from types import SimpleNamespace

import httpx
import pytest

import config
import notifier

LOGGER = "position-stocks-notifier"


@pytest.fixture(autouse=True)
def _clean_dedup():
    notifier._dedup_cache.clear()
    yield
    notifier._dedup_cache.clear()


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(notifier, "time", SimpleNamespace(monotonic=lambda: state["t"]))
    return state


@pytest.fixture()
def real_httpx(monkeypatch):
    """Real httpx.Client over a MockTransport, driven by a scriptable
    per-host handler — no application logic is faked, only the transport."""
    state = {"service": None, "telegram": None, "requests": []}

    def handler(request):
        state["requests"].append(request)
        if request.url.host == "api.telegram.org":
            action = state["telegram"]
        else:
            action = state["service"]
        if isinstance(action, Exception):
            raise action
        return action(request) if callable(action) else action

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    return state


# ══════════════════════════════════════════════════════════════════════════
# _should_send — dedup refresh / eviction
# ══════════════════════════════════════════════════════════════════════════
class TestShouldSendRefreshAndEviction:
    def test_resend_after_window_expiry_refreshes_the_existing_key(self, clock):
        assert notifier._should_send("SAME MSG") is True
        assert "SAME MSG" not in ["dup"]  # sanity, not a real assertion target
        clock["t"] += notifier._DEDUP_WINDOW_S + 1   # past the 5-minute window
        # Resend: hash already present in cache (expired) -> hits the
        # `if h in _dedup_cache: move_to_end` refresh branch, not a fresh insert.
        assert notifier._should_send("SAME MSG") is True

    def test_cache_evicts_oldest_once_it_exceeds_the_cap(self, clock):
        for i in range(notifier._DEDUP_CACHE_SIZE + 5):
            assert notifier._should_send(f"msg-{i}") is True
        assert len(notifier._dedup_cache) == notifier._DEDUP_CACHE_SIZE
        # The earliest messages were evicted (LRU, oldest first) — a repeat
        # of one of them is treated as brand new, not suppressed.
        assert notifier._should_send("msg-0") is True


# ══════════════════════════════════════════════════════════════════════════
# notify_sync — called directly, not via a fixture's wholesale monkeypatch
# ══════════════════════════════════════════════════════════════════════════
class TestNotifySyncDirect:
    def test_duplicate_short_circuits_without_calling_deliver_sync(self, monkeypatch):
        called = []
        monkeypatch.setattr(notifier, "_deliver_sync", lambda text: called.append(text) or True)
        assert notifier.notify_sync("first") is True
        assert called == ["first"]
        assert notifier.notify_sync("first") is True   # duplicate
        assert called == ["first"]   # _deliver_sync not called again

    def test_new_message_calls_deliver_sync_and_returns_its_result(self, monkeypatch):
        monkeypatch.setattr(notifier, "_deliver_sync", lambda text: True)
        assert notifier.notify_sync("brand new alert") is True

    def test_new_message_propagates_a_false_result(self, monkeypatch):
        monkeypatch.setattr(notifier, "_deliver_sync", lambda text: False)
        assert notifier.notify_sync("delivery genuinely failed") is False


# ══════════════════════════════════════════════════════════════════════════
# _deliver_sync — service success / not-delivered / unreachable
# ══════════════════════════════════════════════════════════════════════════
class TestDeliverSync:
    def test_service_delivers_returns_true_without_touching_telegram(self, real_httpx):
        real_httpx["service"] = lambda req: httpx.Response(200, json={"delivered": True})
        real_httpx["telegram"] = lambda req: pytest.fail("should not reach Telegram")
        assert notifier._deliver_sync("all good") is True
        assert len(real_httpx["requests"]) == 1

    def test_service_200_but_not_delivered_falls_back_to_direct_telegram(self, real_httpx, monkeypatch, caplog):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
        real_httpx["service"] = lambda req: httpx.Response(200, json={"delivered": False, "note": "no chat configured"})
        real_httpx["telegram"] = lambda req: httpx.Response(200, json={"ok": True})
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert notifier._deliver_sync("not delivered by service") is True
        assert "Notification service returned not-delivered" in caplog.text
        assert len(real_httpx["requests"]) == 2   # service, then telegram

    def test_service_unreachable_falls_back_to_direct_telegram(self, real_httpx, monkeypatch, caplog):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
        real_httpx["service"] = httpx.ConnectError("service down")
        real_httpx["telegram"] = lambda req: httpx.Response(200, json={"ok": True})
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert notifier._deliver_sync("service unreachable") is True
        assert "trying direct Telegram fallback" in caplog.text


# ══════════════════════════════════════════════════════════════════════════
# _direct_telegram — no config / non-200 retry path
# ══════════════════════════════════════════════════════════════════════════
class TestDirectTelegramEdges:
    def test_no_token_or_chat_id_configured_drops_the_notification(self, monkeypatch, caplog):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert notifier._direct_telegram("nowhere to send this") is False
        assert "env vars not set" in caplog.text

    def test_html_mode_rejected_retries_as_plain_text_and_succeeds(self, real_httpx, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
        responses = iter([httpx.Response(400, text="can't parse entities"), httpx.Response(200, json={"ok": True})])
        real_httpx["telegram"] = lambda req: next(responses)
        assert notifier._direct_telegram("bad <html> text") is True
        assert len(real_httpx["requests"]) == 2

    def test_plain_text_retry_itself_raising_returns_false(self, real_httpx, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "123:tok")
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
        state = {"n": 0}

        def handler(req):
            state["n"] += 1
            if state["n"] == 1:
                return httpx.Response(400, text="rejected")
            raise httpx.ReadTimeout("hung on retry")

        real_httpx["telegram"] = handler
        assert notifier._direct_telegram("times out on retry") is False


# ══════════════════════════════════════════════════════════════════════════
# Token filter — malformed record must never break logging
# ══════════════════════════════════════════════════════════════════════════
class TestTokenFilterMalformedRecord:
    def test_a_record_whose_getMessage_raises_is_swallowed_and_still_passes(self):
        rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, "needs an int: %d", ("not-an-int",), None)
        # record.getMessage() raises TypeError for this args/format mismatch —
        # the filter must catch it (not propagate) and still return True so
        # logging itself is never broken by a hygiene filter.
        assert notifier._TelegramTokenRedactingFilter().filter(rec) is True
