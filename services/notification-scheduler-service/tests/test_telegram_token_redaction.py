"""
tests/test_telegram_token_redaction.py  (first test file in notification-scheduler-service)

Regression for the session98 secret-in-URL leak. Every channel this service
delivers to carries its secret in the request URL (Telegram bot token, Discord
and Slack webhook URLs, CallMeBot apikey) and httpx logs every request at INFO
INCLUDING THE URL, while the service runs logging.basicConfig(level=INFO) — so
each secret was written to the log on every send. Separately, Discord/Slack call
raise_for_status(), whose HTTPStatusError message embeds the full URL, and that
text was logged AND returned to the /notify caller as "failed: <exc>".

notification/main.py's Telegram sender is the PRIMARY one for the whole platform
(every real-trade / position-stocks alert is routed through it).
scheduler/governance_check.py had the same Telegram shape.

Uses a REAL httpx.Client (MockTransport) so the genuine httpx log line and the
genuine HTTPStatusError text are produced — nothing about them is faked.
(File name kept from the first revision of this session; it now covers all four
channels.)

Run from services/notification-scheduler-service:
    python -m pytest tests -v
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

from notification import main as nmain
from scheduler import governance_check as gov

TOKEN = "123456789:AAH-secretSECRETsecretSECRETsecret_xyz"
DISCORD = "https://discord.com/api/webhooks/1122334455667788/discordSECRETtoken_abc-123"
SLACK = "https://hooks.slack.com/services/T00000000/B00000000/slackSECRETsecretXXXX"
CALLMEBOT_KEY = "cmbSECRET99"
LEAK_MARKERS = ("secretSECRET", "discordSECRET", "slackSECRET", "cmbSECRET", "1122334455667788", "T00000000")


@pytest.fixture()
def wire(monkeypatch):
    """httpx.post/get -> real Client on a MockTransport. wire.status controls the reply."""
    class Wire:
        status = 200
        seen = []

    w = Wire()
    w.seen = []

    def handler(request):
        w.seen.append(request)
        return httpx.Response(w.status, text="ok")

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Client(transport=transport).post(url, **kw))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Client(transport=transport).get(url, **kw))
    return w


def _assert_clean(text):
    for marker in LEAK_MARKERS:
        assert marker not in text, f"leaked {marker!r}"


def test_primary_telegram_sender_never_logs_the_bot_token(wire, caplog):
    cfg = {"telegram_bot_token": TOKEN, "telegram_chat_id": "42", "enabled": {"telegram": True}}
    with caplog.at_level(logging.INFO):
        assert nmain._send_telegram(cfg, "Stockky Trade", "BUY *RELIANCE*") == "sent"
    assert wire.seen[0].url.path == f"/bot{TOKEN}/sendMessage"          # the real request DID carry it
    _assert_clean(caplog.text)
    assert 'HTTP Request: POST https://api.telegram.org/bot***/sendMessage "HTTP/1.1 200 OK"' in caplog.text


def test_discord_webhook_url_never_logged_on_success(wire, caplog):
    cfg = {"discord_webhook_url": DISCORD, "enabled": {"discord": True}}
    with caplog.at_level(logging.INFO):
        assert nmain._send_discord(cfg, "t", "m") == "sent"
    assert str(wire.seen[0].url) == DISCORD
    _assert_clean(caplog.text)
    assert "https://discord.com/api/webhooks/***" in caplog.text


def test_slack_webhook_url_never_logged_on_success(wire, caplog):
    cfg = {"slack_webhook_url": SLACK, "enabled": {"slack": True}}
    with caplog.at_level(logging.INFO):
        assert nmain._send_slack(cfg, "t", "m") == "sent"
    assert str(wire.seen[0].url) == SLACK
    _assert_clean(caplog.text)
    assert "https://hooks.slack.com/services/***" in caplog.text


@pytest.mark.parametrize("sender, cfg", [
    ("_send_discord", {"discord_webhook_url": DISCORD, "enabled": {"discord": True}}),
    ("_send_slack", {"slack_webhook_url": SLACK, "enabled": {"slack": True}}),
])
def test_revoked_webhook_does_not_leak_the_url_into_the_log_or_the_returned_note(wire, caplog, sender, cfg):
    # raise_for_status() -> HTTPStatusError whose message embeds the full URL. That text was logged
    # AND returned to the /notify caller as "failed: <exc>".
    wire.status = 404
    with caplog.at_level(logging.INFO):
        result = getattr(nmain, sender)(cfg, "t", "m")
    assert result.startswith("failed: Client error '404 Not Found' for url '")
    _assert_clean(result)
    _assert_clean(caplog.text)


def test_callmebot_apikey_never_logged_or_returned(wire, caplog):
    cfg = {"enabled": {"callmebot": True}, "callmebot_user": "@someone", "callmebot_apikey": CALLMEBOT_KEY}
    with caplog.at_level(logging.INFO):
        result = nmain._send_callmebot(cfg, "Stockky", "alert")
    assert wire.seen and f"apikey={CALLMEBOT_KEY}" in str(wire.seen[0].url)    # the real request DID carry it
    assert result.startswith("sent")
    _assert_clean(caplog.text)
    _assert_clean(result)
    assert "apikey=***" in caplog.text


def test_governance_sender_never_logs_the_bot_token(wire, monkeypatch, caplog):
    monkeypatch.setattr(gov, "TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(gov, "TELEGRAM_CHAT_ID", "42")
    with caplog.at_level(logging.INFO):
        gov._send_telegram("threshold drift")
    assert wire.seen
    _assert_clean(caplog.text)


@pytest.mark.parametrize(
    "text, expected",
    [
        (f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed", "POST https://api.telegram.org/bot***/sendMessage failed"),
        (f"for url '{DISCORD}'", "for url 'https://discord.com/api/webhooks/***'"),
        ("https://DISCORDAPP.com/api/webhooks/1/abc_DEF-9?wait=true", "https://DISCORDAPP.com/api/webhooks/***?wait=true"),
        (f"for url '{SLACK}'", "for url 'https://hooks.slack.com/services/***'"),
        ("https://api.callmebot.com/text.php?user=%40x&text=hi&apikey=abc123&x=1", "https://api.callmebot.com/text.php?user=%40x&text=hi&apikey=***&x=1"),
        ("https://api.callmebot.com/start.php?APIKEY=zzz", "https://api.callmebot.com/start.php?APIKEY=***"),
        ("https://example.com/robots.txt?a=1", "https://example.com/robots.txt?a=1"),
        ("http://notification-scheduler-service:8000/health", "http://notification-scheduler-service:8000/health"),
        ("plain text", "plain text"),
    ],
)
def test_redact_secrets(text, expected):
    assert nmain._redact_secrets(text) == expected


def test_redact_secrets_never_raises():
    class Hostile:
        def __str__(self):
            raise RuntimeError("no")

    assert "withheld" in nmain._redact_secrets(Hostile())


def test_filter_rewrites_records_and_leaves_clean_ones_alone():
    f = nmain._SecretRedactingFilter()
    dirty = logging.LogRecord("httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"', ("POST", SLACK, "HTTP/1.1 200 OK"), None)
    assert f.filter(dirty) is True
    assert dirty.getMessage() == 'HTTP Request: POST https://hooks.slack.com/services/*** "HTTP/1.1 200 OK"' and dirty.args == ()
    clean = logging.LogRecord("httpx", logging.INFO, __file__, 1, "HTTP Request: %s %s", ("GET", "http://svc/health"), None)
    assert f.filter(clean) is True
    assert clean.msg == "HTTP Request: %s %s" and clean.args == ("GET", "http://svc/health")
    broken = logging.LogRecord("httpx", logging.INFO, __file__, 1, "needs an int: %d", ("x",), None)
    assert f.filter(broken) is True            # a logging filter must never break logging


def test_filter_install_is_idempotent():
    nmain._install_httpx_secret_filter()
    nmain._install_httpx_secret_filter()
    assert sum(isinstance(f, nmain._SecretRedactingFilter) for f in logging.getLogger("httpx").filters) == 1


def test_governance_filter_install_is_idempotent():
    gov._install_httpx_token_filter()
    gov._install_httpx_token_filter()
    assert sum(isinstance(f, gov._TelegramTokenRedactingFilter) for f in logging.getLogger("httpx").filters) == 1


def test_other_httpx_lines_are_untouched(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info('HTTP Request: GET http://notification-scheduler-service:8000/health "HTTP/1.1 200 OK"')
    assert 'GET http://notification-scheduler-service:8000/health "HTTP/1.1 200 OK"' in caplog.text
