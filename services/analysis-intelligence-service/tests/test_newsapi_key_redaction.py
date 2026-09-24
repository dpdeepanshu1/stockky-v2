"""
tests/test_newsapi_key_redaction.py

Regression for the session99 API-key-in-URL leak: news/main.py's
_fetch_newsapi() calls https://newsapi.org/v2/everything?...&apiKey=<key> with
httpx — the NewsAPI key is part of the URL. httpx logs every request at INFO
INCLUDING THE URL, and news/main.py runs logging.basicConfig(level=logging.INFO)
with nothing muting the "httpx" logger — so every news fetch wrote the key to
the log. Same class of leak as session98's Telegram-bot-token fix and
session99's market-data-service fix; news/main.py now installs a redacting
filter on the "httpx" logger.

The tests use a REAL httpx.Client (MockTransport) so the genuine httpx log
line is produced — nothing about the logging is faked.

This is the first test directory for analysis-intelligence-service; news/
has no package __init__.py (it's a path-mounted sub-app, like the service's
other sub-modules), so the test puts news/ on sys.path directly, the same way
main.py's own sub-mounts are laid out.

Run from services/analysis-intelligence-service:
    python -m pytest tests/test_newsapi_key_redaction.py -v
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "news"))

import httpx
import pytest

import main as news_main  # noqa: E402  (services/analysis-intelligence-service/news/main.py)

NEWSAPI_KEY = "SECRETnewsapiKEYsecretNEWSAPIkey123"


@pytest.fixture()
def mock_newsapi(monkeypatch):
    calls = []
    real_client_cls = httpx.Client

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"articles": []})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client_cls(transport=transport))
    return calls


def test_newsapi_key_never_reaches_the_log(mock_newsapi, monkeypatch, caplog):
    monkeypatch.setattr(news_main, "NEWSAPI_KEY", NEWSAPI_KEY)
    with caplog.at_level(logging.INFO):
        items = news_main._fetch_newsapi("RELIANCE")
    assert items == []
    assert f"apiKey={NEWSAPI_KEY}" in str(mock_newsapi[0].url)  # the real request DID carry it
    assert NEWSAPI_KEY not in caplog.text
    assert "apiKey=***" in caplog.text


def test_filter_install_is_idempotent():
    news_main._install_httpx_secret_filter()
    news_main._install_httpx_secret_filter()
    filters = [
        f for f in logging.getLogger("httpx").filters
        if isinstance(f, news_main._SecretRedactingFilter)
    ]
    assert len(filters) == 1


def test_other_httpx_log_lines_are_untouched(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: GET https://news.google.com/rss/search?q=Reliance "HTTP/1.1 200 OK"'
        )
    assert 'GET https://news.google.com/rss/search?q=Reliance "HTTP/1.1 200 OK"' in caplog.text


def test_no_newsapi_key_returns_early_without_a_request(monkeypatch):
    monkeypatch.setattr(news_main, "NEWSAPI_KEY", None)
    assert news_main._fetch_newsapi("RELIANCE") == []


def test_failure_error_text_is_scrubbed_too(monkeypatch, caplog):
    monkeypatch.setattr(news_main, "NEWSAPI_KEY", NEWSAPI_KEY)

    class _BoomClient:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            raise RuntimeError(f"tunnel failed for {url}")

    monkeypatch.setattr(httpx, "Client", _BoomClient)
    with caplog.at_level(logging.WARNING):
        items = news_main._fetch_newsapi("RELIANCE")
    assert items == []
    assert NEWSAPI_KEY not in caplog.text
    assert "apiKey=***" in caplog.text


@pytest.mark.parametrize("text,expected", [
    ("https://newsapi.org/v2/everything?q=X&apiKey=ABC123", "apiKey=***"),
    ("no secrets here", "no secrets here"),
    (None, "None"),
])
def test_redact_secrets_table(text, expected):
    assert expected in news_main._redact_secrets(text)
