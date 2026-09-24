"""
tests/test_notifier_token_redaction.py

Regression for the session98 Telegram bot-token leak: notifier._direct_telegram()
calls https://api.telegram.org/bot<TOKEN>/sendMessage with httpx, httpx logs
every request at INFO INCLUDING THE URL, and the service runs
logging.basicConfig(level=logging.INFO) — so the bot token landed in the log on
every direct send. notifier.py now installs a filter on the "httpx" logger.

The test uses a REAL httpx.Client (MockTransport) so the genuine httpx log line
is produced — nothing about the logging is faked.

Run from services/position-stocks-service:
    python -m pytest tests/test_notifier_token_redaction.py -v
"""
from __future__ import annotations

import logging

import httpx
import pytest

import config
import notifier

TOKEN = "123456789:AAH-secretSECRETsecretSECRETsecret_xyz"


@pytest.fixture()
def real_httpx_post(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")
    return calls


def test_bot_token_never_reaches_the_log(real_httpx_post, caplog):
    with caplog.at_level(logging.INFO):
        assert notifier._direct_telegram("hello") is True
    assert real_httpx_post[0].url.path == f"/bot{TOKEN}/sendMessage"      # the real request DID carry it
    assert "secretSECRET" not in caplog.text
    assert 'HTTP Request: POST https://api.telegram.org/bot***/sendMessage "HTTP/1.1 200 OK"' in caplog.text


def test_filter_install_is_idempotent():
    notifier._install_httpx_token_filter()
    notifier._install_httpx_token_filter()
    filters = [f for f in logging.getLogger("httpx").filters if isinstance(f, notifier._TelegramTokenRedactingFilter)]
    assert len(filters) == 1


def test_other_httpx_log_lines_are_untouched(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: GET http://notification-scheduler-service:8000/health "HTTP/1.1 200 OK"')
    assert 'GET http://notification-scheduler-service:8000/health "HTTP/1.1 200 OK"' in caplog.text


def test_error_text_from_a_failed_send_is_scrubbed_too(monkeypatch, caplog):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(config, "TELEGRAM_CHAT_ID", "42")

    def boom(url, **kw):
        raise RuntimeError(f"tunnel failed for {url}")

    monkeypatch.setattr(httpx, "post", boom)
    with caplog.at_level(logging.WARNING):
        assert notifier._direct_telegram("hello") is False
    assert "secretSECRET" not in caplog.text
    assert "tunnel failed for https://api.telegram.org/bot***/sendMessage" in caplog.text
