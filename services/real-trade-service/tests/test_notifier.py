"""
tests/test_notifier.py

100%-coverage-plan round for notifier.py (was ~23% — and that number was never
stable: it read 52% -> 48% -> 23% across recent runs with NO change to the file,
because the module had no direct tests and its covered lines came from whichever
unmocked notify_* calls happened to run inside other tests).

notifier.py is the Telegram path for every BUY / fill / SELL / kill-switch /
TOTP-failure alert this service raises, and its stated contract is "best-effort:
a notification failure must NEVER block or fail an order path".

Everything runs through REAL httpx: httpx.post / httpx.AsyncClient are routed to
a real httpx.Client / AsyncClient with an httpx.MockTransport, so requests,
responses, exceptions, timeouts and — importantly — httpx's own INFO request log
line are all genuine. The only fakes are the transport's handler and a
controllable monotonic clock for the 5-minute dedup window.

Production fix shipping with this round (found while writing these tests,
regression-pinned below, shown to fail on the pre-fix code):
  * The Telegram BOT TOKEN was written to the service log on every direct send.
    _direct_telegram() calls https://api.telegram.org/bot<TOKEN>/sendMessage;
    httpx logs every request at INFO INCLUDING THE URL; main.py runs
    logging.basicConfig(level=logging.INFO) and nothing muted the "httpx" logger.
    Verified on the pinned httpx==0.25.2 and on 0.28.1:
        INFO:httpx:HTTP Request: POST https://api.telegram.org/bot123456789:AAH-...
    notifier.py now installs a redacting filter on the "httpx" logger and scrubs
    the error text it logs itself. (The same defect existed in three sibling
    senders — see the session note; fixed there too, with their own tests.)

What is covered:
  * _should_send — first/duplicate/expiry (exact 300s boundary), independence of
    distinct texts, a duplicate does NOT refresh the window (a drip every 100s
    cannot suppress forever), LRU cap of 64 + eviction order, refresh moves a
    key to the end, unencodable text.
  * notify_sync / notify_async — service delivered; service says not-delivered;
    non-200; unreachable; invalid JSON; each falls back to direct Telegram and
    returns its result; dedup short-circuit; exact request (URL, JSON body,
    12s timeout); async variant runs the fallback in a worker thread.
  * _direct_telegram — no token / no chat id, exact request (URL, payload,
    15s timeout), *bold* -> <b>bold</b>, HTML-mode 400 -> plain-text retry (the
    fix for messages with < & _ from Dhan error text), retry failure paths,
    transport error.
  * Token hygiene — end-to-end (real httpx log line), the filter and
    _redact_token directly, idempotent install, other httpx lines untouched.
  * Module wiring — NOTIFICATION_SERVICE_URL default and trailing-slash strip
    (fresh interpreter), is_configured.

Run from services/real-trade-service:
    python3 -m pytest tests/test_notifier.py -q --cov=notifier --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

SERVICE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVICE_DIR)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import httpx
import pytest

import config
import notifier

LOGGER = "real-trade-notifier"
TOKEN = "123456789:AAH-secretSECRETsecretSECRETsecret_xyz"
SERVICE = "http://notification-scheduler-service:8000/notification"
TG_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


# ── fixtures ──────────────────────────────────────────────────────────────
class Net:
    """Scripted network behind httpx.MockTransport."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.post_kwargs: list[dict] = []
        self.async_kwargs: list[dict] = []
        self.service = lambda req: httpx.Response(200, json={"delivered": True})
        self.telegram: list = [httpx.Response(200, json={"ok": True})]   # consumed in order; last one repeats

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.telegram.org":
            item = self.telegram.pop(0) if len(self.telegram) > 1 else self.telegram[0]
        else:
            item = self.service(request)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def service_requests(self):
        return [r for r in self.requests if r.url.host != "api.telegram.org"]

    @property
    def telegram_requests(self):
        return [r for r in self.requests if r.url.host == "api.telegram.org"]

    @staticmethod
    def body(request):
        return json.loads(request.content)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    notifier._dedup_cache.clear()
    monkeypatch.setattr(notifier, "_NOTIFICATION_SERVICE_URL", SERVICE)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "")
    yield
    notifier._dedup_cache.clear()


@pytest.fixture()
def net(monkeypatch):
    n = Net()
    transport = httpx.MockTransport(n.handler)
    real_async = httpx.AsyncClient

    def fake_post(url, **kw):
        n.post_kwargs.append({"url": url, **kw})
        return httpx.Client(transport=transport).post(url, **kw)

    def fake_async(**kw):
        n.async_kwargs.append(kw)
        return real_async(transport=transport, **kw)

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(httpx, "AsyncClient", fake_async)
    return n


@pytest.fixture()
def telegram_configured(monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")


@pytest.fixture()
def clock(monkeypatch):
    state = {"t": 1000.0}
    monkeypatch.setattr(notifier, "time", SimpleNamespace(monotonic=lambda: state["t"]))
    return state


def run(coro):
    return asyncio.run(coro)


# ══════════════════════════════════════════════════════════════════════════
# _should_send (dedup)
# ══════════════════════════════════════════════════════════════════════════
class TestShouldSend:
    def test_first_send_then_duplicate_suppressed(self, clock):
        assert notifier._should_send("SELL AAA") is True
        assert notifier._should_send("SELL AAA") is False

    def test_distinct_messages_are_independent(self, clock):
        assert notifier._should_send("SELL AAA") is True
        assert notifier._should_send("SELL BBB") is True
        assert notifier._should_send("SELL AAA") is False

    def test_window_boundary_is_exactly_300_seconds(self, clock):
        notifier._should_send("m")
        clock["t"] += 299.999
        assert notifier._should_send("m") is False
        clock["t"] += 0.001            # now exactly 300s after the send
        assert notifier._should_send("m") is True

    def test_a_suppressed_duplicate_does_not_extend_the_window(self, clock):
        # A drip of identical alerts every 100s must not be suppressed forever.
        assert notifier._should_send("m") is True      # t=0
        clock["t"] += 100
        assert notifier._should_send("m") is False     # t=100
        clock["t"] += 100
        assert notifier._should_send("m") is False     # t=200
        clock["t"] += 101
        assert notifier._should_send("m") is True      # t=301 — window measured from the LAST SENT one

    def test_expired_key_is_refreshed_and_moved_to_the_end(self, clock):
        notifier._should_send("old")
        notifier._should_send("other")
        clock["t"] += 400
        assert notifier._should_send("old") is True
        h_old = hashlib.md5(b"old").hexdigest()
        assert list(notifier._dedup_cache)[-1] == h_old
        assert notifier._dedup_cache[h_old] == clock["t"]
        assert notifier._should_send("old") is False   # and it's suppressed again from the new timestamp

    def test_cache_is_capped_at_64_and_evicts_the_oldest(self, clock):
        for i in range(65):
            assert notifier._should_send(f"msg-{i}") is True
        assert len(notifier._dedup_cache) == notifier._DEDUP_CACHE_SIZE == 64
        assert notifier._should_send("msg-0") is True      # evicted -> sends again (no longer remembered)
        assert notifier._should_send("msg-64") is False    # newest still remembered

    def test_unencodable_text_does_not_raise(self, clock):
        assert notifier._should_send("lone surrogate \ud800 in Dhan error") is True
        assert notifier._should_send("lone surrogate \ud800 in Dhan error") is False

    def test_unicode_alert_text(self, clock):
        assert notifier._should_send("🚨 *Dhan TOTP refresh FAILED*") is True
        assert notifier._should_send("🚨 *Dhan TOTP refresh FAILED*") is False


def test_is_configured_is_always_true():
    assert notifier.is_configured() is True


# ══════════════════════════════════════════════════════════════════════════
# notify_sync
# ══════════════════════════════════════════════════════════════════════════
class TestNotifySync:
    def test_delivered_by_the_notification_service(self, net):
        assert notifier.notify_sync("BUY *AAA* filled") is True
        assert len(net.service_requests) == 1 and net.telegram_requests == []
        req = net.service_requests[0]
        assert req.method == "POST"
        assert str(req.url) == f"{SERVICE}/notify"
        assert Net.body(req) == {"title": "Stockky Trade", "message": "BUY *AAA* filled", "channel": "telegram"}
        assert net.post_kwargs[0]["timeout"] == 12.0

    def test_identical_message_within_the_window_makes_no_network_call(self, net, caplog):
        notifier.notify_sync("SELL AAA rejected")
        net.requests.clear()
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert notifier.notify_sync("SELL AAA rejected") is True
        assert net.requests == []
        assert "duplicate message suppressed within dedup window" in caplog.text

    def test_service_says_not_delivered_falls_back_to_telegram(self, net, telegram_configured, caplog):
        net.service = lambda req: httpx.Response(200, json={"delivered": False, "note": "telegram disabled"})
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert notifier.notify_sync("hello") is True
        assert len(net.telegram_requests) == 1
        assert "Notification service returned not-delivered: telegram disabled" in caplog.text

    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    def test_non_200_from_the_service_falls_back(self, net, telegram_configured, status):
        net.service = lambda req: httpx.Response(status, json={"detail": "nope"})
        assert notifier.notify_sync("hello") is True
        assert len(net.telegram_requests) == 1

    def test_unreachable_service_falls_back_and_logs_at_debug(self, net, telegram_configured, caplog):
        net.service = lambda req: httpx.ConnectError("Name or service not known")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert notifier.notify_sync("hello") is True
        assert "Notification service unreachable (Name or service not known) — trying direct Telegram fallback" in caplog.text

    def test_service_200_with_invalid_json_falls_back(self, net, telegram_configured):
        net.service = lambda req: httpx.Response(200, content=b"<html>proxy error</html>")
        assert notifier.notify_sync("hello") is True
        assert len(net.telegram_requests) == 1

    def test_service_read_timeout_falls_back(self, net, telegram_configured):
        net.service = lambda req: httpx.ReadTimeout("timed out")
        assert notifier.notify_sync("hello") is True

    def test_returns_false_when_both_channels_fail(self, net, telegram_configured):
        net.service = lambda req: httpx.ConnectError("down")
        net.telegram = [httpx.Response(500, text="oops")]
        assert notifier.notify_sync("hello") is False

    def test_returns_false_when_service_is_down_and_telegram_is_not_configured(self, net):
        net.service = lambda req: httpx.ConnectError("down")
        assert notifier.notify_sync("hello") is False
        assert net.telegram_requests == []

    def test_a_failed_delivery_still_consumes_the_dedup_slot(self, net, caplog):
        # Pinned CURRENT behaviour (see session note observation): dedup is
        # recorded BEFORE delivery, so if every channel is down the retry of the
        # same text is reported as "sent" (True) and never attempted for 5 min.
        net.service = lambda req: httpx.ConnectError("down")
        assert notifier.notify_sync("SELL AAA sent") is False
        net.requests.clear()
        assert notifier.notify_sync("SELL AAA sent") is True
        assert net.requests == []


# ══════════════════════════════════════════════════════════════════════════
# notify_async
# ══════════════════════════════════════════════════════════════════════════
class TestNotifyAsync:
    def test_delivered_by_the_notification_service(self, net):
        assert run(notifier.notify_async("BUY *AAA* filled")) is True
        assert len(net.service_requests) == 1 and net.telegram_requests == []
        assert str(net.service_requests[0].url) == f"{SERVICE}/notify"
        assert Net.body(net.service_requests[0]) == {"title": "Stockky Trade", "message": "BUY *AAA* filled", "channel": "telegram"}
        assert net.async_kwargs == [{"timeout": 12.0}]

    def test_duplicate_is_suppressed_without_touching_the_network(self, net, caplog):
        run(notifier.notify_async("dup"))
        net.requests.clear()
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert run(notifier.notify_async("dup")) is True
        assert net.requests == []
        assert "duplicate message suppressed" in caplog.text

    def test_not_delivered_falls_back_in_a_worker_thread(self, net, telegram_configured, monkeypatch, caplog):
        import threading

        net.service = lambda req: httpx.Response(200, json={"delivered": False, "note": "disabled"})
        seen = {}
        real = notifier._direct_telegram

        def spy(text):
            seen["thread"] = threading.current_thread()
            return real(text)

        monkeypatch.setattr(notifier, "_direct_telegram", spy)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert run(notifier.notify_async("hello")) is True
        assert seen["thread"] is not threading.main_thread()       # blocking httpx.post kept off the event loop
        assert "Notification service returned not-delivered: disabled" in caplog.text
        assert len(net.telegram_requests) == 1

    @pytest.mark.parametrize("status", [404, 500])
    def test_non_200_falls_back(self, net, telegram_configured, status):
        net.service = lambda req: httpx.Response(status, json={})
        assert run(notifier.notify_async("hello")) is True
        assert len(net.telegram_requests) == 1

    def test_unreachable_service_falls_back(self, net, telegram_configured, caplog):
        net.service = lambda req: httpx.ConnectError("refused")
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert run(notifier.notify_async("hello")) is True
        assert "Notification service unreachable (refused)" in caplog.text

    def test_invalid_json_falls_back(self, net, telegram_configured):
        net.service = lambda req: httpx.Response(200, content=b"not json")
        assert run(notifier.notify_async("hello")) is True

    def test_returns_false_when_everything_fails(self, net, telegram_configured):
        net.service = lambda req: httpx.ConnectError("down")
        net.telegram = [httpx.ConnectError("also down")]
        assert run(notifier.notify_async("hello")) is False

    def test_not_configured_and_service_down_returns_false(self, net):
        net.service = lambda req: httpx.ConnectError("down")
        assert run(notifier.notify_async("hello")) is False


# ══════════════════════════════════════════════════════════════════════════
# _direct_telegram
# ══════════════════════════════════════════════════════════════════════════
class TestDirectTelegram:
    @pytest.mark.parametrize("token, chat", [("", ""), (TOKEN, ""), ("", "42")])
    def test_missing_token_or_chat_id_drops_the_message_without_a_request(self, net, monkeypatch, caplog, token, chat):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", token)
        monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", chat)
        with caplog.at_level(logging.DEBUG, logger=LOGGER):
            assert notifier._direct_telegram("hello") is False
        assert net.requests == []
        assert "env vars not set — notification dropped" in caplog.text

    def test_exact_request(self, net, telegram_configured):
        assert notifier._direct_telegram("SELL *AAA* qty 5") is True
        (req,) = net.telegram_requests
        assert str(req.url) == TG_URL
        assert Net.body(req) == {
            "chat_id": "42",
            "text": "SELL <b>AAA</b> qty 5",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        assert net.post_kwargs[0]["timeout"] == 15.0

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("*a* and *b*", "<b>a</b> and <b>b</b>"),
            ("no bold here", "no bold here"),
            ("P*L unmatched", "P*L unmatched"),
            ("** empty pair", "** empty pair"),
            ("₹1,234.50 *net*", "₹1,234.50 <b>net</b>"),
        ],
    )
    def test_markdown_bold_becomes_html_bold(self, net, telegram_configured, text, expected):
        notifier._direct_telegram(text)
        assert Net.body(net.telegram_requests[0])["text"] == expected

    def test_html_mode_rejection_retries_as_plain_text_and_succeeds(self, net, telegram_configured, caplog):
        # Dhan error text containing < or & makes Telegram's HTML parser return 400.
        net.telegram = [httpx.Response(400, json={"ok": False, "description": "can't parse entities"}), httpx.Response(200, json={"ok": True})]
        text = "Order rejected: qty < min & *AAA*"
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert notifier._direct_telegram(text) is True
        first, second = net.telegram_requests
        assert Net.body(first)["parse_mode"] == "HTML"
        assert Net.body(second) == {"chat_id": "42", "text": text, "disable_web_page_preview": True}   # no parse_mode, original text
        assert net.post_kwargs[1]["timeout"] == 15.0
        assert "Direct Telegram notify failed (400): " in caplog.text
        assert "can't parse entities" in caplog.text

    def test_rejection_body_is_truncated_to_200_chars_in_the_log(self, net, telegram_configured, caplog):
        net.telegram = [httpx.Response(400, text="E" * 500), httpx.Response(200, json={})]
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            notifier._direct_telegram("x")
        assert "E" * 200 in caplog.text and "E" * 201 not in caplog.text

    def test_plain_text_retry_also_rejected_returns_false(self, net, telegram_configured):
        net.telegram = [httpx.Response(400, text="bad"), httpx.Response(403, text="bot was blocked")]
        assert notifier._direct_telegram("hello") is False
        assert len(net.telegram_requests) == 2

    def test_plain_text_retry_raising_returns_false_quietly(self, net, telegram_configured, caplog):
        net.telegram = [httpx.Response(400, text="bad"), httpx.ConnectError("reset")]
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert notifier._direct_telegram("hello") is False
        assert [r for r in caplog.records if "error" in r.getMessage().lower()] == []   # only the 400 was logged

    def test_transport_error_on_the_first_attempt_returns_false_with_a_warning_and_no_retry(self, net, telegram_configured, caplog):
        net.telegram = [httpx.ConnectTimeout("connect timed out")]
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert notifier._direct_telegram("hello") is False
        assert len(net.telegram_requests) == 1
        assert "Direct Telegram notify error: connect timed out" in caplog.text

    def test_worst_case_blocking_budget_of_notify_sync(self, net, telegram_configured):
        # Pinned so a change is a conscious one: notify_sync is called inline from
        # the exit engine, so a dead notification service (12s) + a rejected HTML
        # send (15s) + a hung plain retry (15s) can stall the caller ~42s.
        net.service = lambda req: httpx.ReadTimeout("hung")
        net.telegram = [httpx.Response(400, text="bad"), httpx.ReadTimeout("hung")]
        notifier.notify_sync("hello")
        assert [k["timeout"] for k in net.post_kwargs] == [12.0, 15.0, 15.0]


# ══════════════════════════════════════════════════════════════════════════
# Bot-token hygiene (session98)
# ══════════════════════════════════════════════════════════════════════════
class TestTokenHygiene:
    def test_bot_token_never_reaches_the_log_on_a_direct_send(self, net, telegram_configured, caplog):
        # REGRESSION: httpx logs "HTTP Request: POST https://api.telegram.org/bot<TOKEN>/sendMessage"
        # at INFO and the service logs at INFO. Real httpx log line, real logger.
        with caplog.at_level(logging.INFO):
            assert notifier._direct_telegram("hello") is True
        assert net.telegram_requests[0].url.path == f"/bot{TOKEN}/sendMessage"    # the request DID carry it
        assert "secretSECRET" not in caplog.text
        assert 'HTTP Request: POST https://api.telegram.org/bot***/sendMessage "HTTP/1.1 200 OK"' in caplog.text

    def test_token_is_also_scrubbed_when_the_send_takes_the_notify_sync_fallback(self, net, telegram_configured, caplog):
        net.service = lambda req: httpx.ConnectError("down")
        with caplog.at_level(logging.DEBUG):
            assert notifier.notify_sync("hello") is True
        assert "secretSECRET" not in caplog.text

    def test_token_in_a_transport_error_message_is_scrubbed_from_the_warning(self, net, telegram_configured, caplog):
        net.telegram = [RuntimeError(f"proxy refused CONNECT for {TG_URL}")]
        with caplog.at_level(logging.WARNING, logger=LOGGER):
            assert notifier._direct_telegram("hello") is False
        assert "secretSECRET" not in caplog.text
        assert "proxy refused CONNECT for https://api.telegram.org/bot***/sendMessage" in caplog.text

    def test_other_httpx_log_lines_are_untouched(self, caplog):
        with caplog.at_level(logging.INFO, logger="httpx"):
            logging.getLogger("httpx").info('HTTP Request: POST %s "HTTP/1.1 200 OK"', f"{SERVICE}/notify")
        assert f'HTTP Request: POST {SERVICE}/notify "HTTP/1.1 200 OK"' in caplog.text

    def test_filter_install_is_idempotent(self):
        notifier._install_httpx_token_filter()
        notifier._install_httpx_token_filter()
        ours = [f for f in logging.getLogger("httpx").filters if isinstance(f, notifier._TelegramTokenRedactingFilter)]
        assert len(ours) == 1

    def test_filter_rewrites_a_formatted_record_and_keeps_it(self):
        rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"',
                                ("POST", f"https://api.telegram.org/bot{TOKEN}/sendMessage", "HTTP/1.1 200 OK"), None)
        assert notifier._TelegramTokenRedactingFilter().filter(rec) is True
        assert rec.getMessage() == 'HTTP Request: POST https://api.telegram.org/bot***/sendMessage "HTTP/1.1 200 OK"'
        assert rec.args == ()

    def test_filter_leaves_clean_records_exactly_as_they_were(self):
        rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", ("GET", "http://svc/health"), None)
        assert notifier._TelegramTokenRedactingFilter().filter(rec) is True
        assert rec.msg == "HTTP Request: %s %s" and rec.args == ("GET", "http://svc/health")

    def test_filter_never_breaks_logging_on_a_malformed_record(self):
        rec = logging.LogRecord("httpx", logging.INFO, __file__, 1, "needs an int: %d", ("not-an-int",), None)
        assert notifier._TelegramTokenRedactingFilter().filter(rec) is True

    @pytest.mark.parametrize(
        "text, expected",
        [
            (f"POST {TG_URL} failed", "POST https://api.telegram.org/bot***/sendMessage failed"),
            ("/bot987654321:ZZ_top-secret1/getUpdates", "/bot***/getUpdates"),
            ("https://example.com/robots.txt", "https://example.com/robots.txt"),
            ("/bots/list", "/bots/list"),
            ("plain text", "plain text"),
        ],
    )
    def test_redact_token_url_segment(self, text, expected):
        assert notifier._redact_token(text) == expected

    def test_redact_token_also_removes_the_configured_token_wherever_it_appears(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "legacy-format-token-abcdef")
        assert notifier._redact_token("bad token legacy-format-token-abcdef used") == "bad token *** used"

    def test_a_too_short_configured_token_is_not_used_for_literal_replacement(self, monkeypatch):
        monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "abc")
        assert notifier._redact_token("abc def abc") == "abc def abc"

    def test_redact_token_never_raises(self):
        class Hostile:
            def __str__(self):
                raise RuntimeError("no")

        assert "withheld" in notifier._redact_token(Hostile())


# ══════════════════════════════════════════════════════════════════════════
# Module wiring (fresh interpreter — module-level env read)
# ══════════════════════════════════════════════════════════════════════════
def _service_url_in_fresh_interpreter(env_value):
    env = {k: v for k, v in os.environ.items() if k != "NOTIFICATION_SERVICE_URL"}
    env["DATABASE_URL"] = "sqlite:///:memory:"
    if env_value is not None:
        env["NOTIFICATION_SERVICE_URL"] = env_value
    out = subprocess.run(
        [sys.executable, "-c", "import notifier; print(notifier._NOTIFICATION_SERVICE_URL)"],
        cwd=SERVICE_DIR, env=env, capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


class TestModuleWiring:
    def test_default_service_url_is_the_compose_hostname(self):
        assert _service_url_in_fresh_interpreter(None) == "http://notification-scheduler-service:8000/notification"

    def test_env_override_has_trailing_slashes_stripped(self):
        assert _service_url_in_fresh_interpreter("http://10.0.0.5:9000/notification///") == "http://10.0.0.5:9000/notification"
