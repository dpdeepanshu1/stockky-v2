"""
tests/test_provider_key_redaction.py

Regression for the session99 API-key-in-URL leak: the TwelveData, Polygon and
AlphaVantage waterfall fallbacks in main.py all put the provider's API key IN
THE URL (`?apikey=<key>` / `?apiKey=<key>`), httpx logs every request at INFO
INCLUDING THE URL, and the service runs logging.basicConfig(level=logging.INFO)
— so every waterfall call wrote the key to the log. Same class of leak as
session98's Telegram-bot-token fix; main.py now installs a redacting filter on
the "httpx" logger plus scrubs its own debug-logged exception text.

The tests use a REAL httpx.Client (MockTransport) so the genuine httpx log
line is produced — nothing about the logging is faked.

Run from services/market-data-service:
    python -m pytest tests/test_provider_key_redaction.py -v
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("UPSTASH_REDIS_REST_URL", "http://example.invalid")
os.environ.setdefault("UPSTASH_REDIS_REST_TOKEN", "test-token")

import httpx
import pytest

import main  # noqa: E402

TWELVEDATA_KEY = "SECRETtwelveKEYsecretTWELVEkey123"
POLYGON_KEY = "SECRETpolygonKEYsecretPOLYGONkey456"
ALPHAVANTAGE_KEY = "SECRETalphaKEYsecretALPHAkey789"


@pytest.fixture()
def mock_get(monkeypatch):
    """Route main's module-level httpx.get through a MockTransport so the
    request (and its genuine httpx INFO log line) is real."""
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "price": "123.45",
                "results": [{"c": 123.45}],
                "Global Quote": {"05. price": "123.45"},
            },
        )

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "get", lambda url, **kw: httpx.Client(transport=transport).get(url)
    )
    return calls


def test_twelvedata_key_never_reaches_the_log(mock_get, monkeypatch, caplog):
    monkeypatch.setattr(main, "TWELVE_DATA_API_KEY", TWELVEDATA_KEY)
    with caplog.at_level(logging.INFO):
        px = main._waterfall_twelvedata_price("RELIANCE")
    assert px == 123.45
    assert f"apikey={TWELVEDATA_KEY}" in str(mock_get[0].url)  # the real request DID carry it
    assert TWELVEDATA_KEY not in caplog.text
    assert "apikey=***" in caplog.text


def test_polygon_key_never_reaches_the_log(mock_get, monkeypatch, caplog):
    monkeypatch.setattr(main, "POLYGON_API_KEY", POLYGON_KEY)
    with caplog.at_level(logging.INFO):
        px = main._waterfall_polygon_price("RELIANCE")
    assert px == 123.45
    assert f"apiKey={POLYGON_KEY}" in str(mock_get[0].url)
    assert POLYGON_KEY not in caplog.text
    assert "apiKey=***" in caplog.text


def test_alphavantage_key_never_reaches_the_log(mock_get, monkeypatch, caplog):
    monkeypatch.setattr(main, "ALPHA_VANTAGE_API_KEY", ALPHAVANTAGE_KEY)
    with caplog.at_level(logging.INFO):
        px = main._waterfall_alphavantage_price("RELIANCE")
    assert px == 123.45
    assert f"apikey={ALPHAVANTAGE_KEY}" in str(mock_get[0].url)
    assert ALPHAVANTAGE_KEY not in caplog.text
    assert "apikey=***" in caplog.text


def test_filter_install_is_idempotent():
    main._install_httpx_secret_filter()
    main._install_httpx_secret_filter()
    filters = [
        f for f in logging.getLogger("httpx").filters
        if isinstance(f, main._SecretRedactingFilter)
    ]
    assert len(filters) == 1


def test_other_httpx_log_lines_are_untouched(caplog):
    with caplog.at_level(logging.INFO, logger="httpx"):
        logging.getLogger("httpx").info(
            'HTTP Request: GET https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS "HTTP/1.1 200 OK"'
        )
    assert 'GET https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS "HTTP/1.1 200 OK"' in caplog.text


def test_error_text_from_a_failed_call_is_scrubbed_too(monkeypatch, caplog):
    monkeypatch.setattr(main, "TWELVE_DATA_API_KEY", TWELVEDATA_KEY)

    def boom(url, **kw):
        raise RuntimeError(f"tunnel failed for {url}")

    monkeypatch.setattr(httpx, "get", boom)
    with caplog.at_level(logging.DEBUG):
        px = main._waterfall_twelvedata_price("RELIANCE")
    assert px is None
    assert TWELVEDATA_KEY not in caplog.text
    assert "apikey=***" in caplog.text


@pytest.mark.parametrize("text,expected", [
    ("https://api.twelvedata.com/price?symbol=X&apikey=ABC123", "apikey=***"),
    ("https://api.polygon.io/v2/x?adjusted=true&apiKey=XYZ789", "apiKey=***"),
    ("no secrets here", "no secrets here"),
    (None, "None"),
])
def test_redact_secrets_table(text, expected):
    assert expected in main._redact_secrets(text)
