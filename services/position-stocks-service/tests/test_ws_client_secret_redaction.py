"""
tests/test_ws_client_secret_redaction.py

Regression for the session99 AngelOne feed-secret-in-URL leak:
feed/ws_client.py builds ws_url with clientCode / feedToken / apiKey as query
params (required by AngelOne's feed protocol — no header-auth alternative).
Nothing in this module logs ws_url directly, but the `websockets` library logs
the full request line (path + query string, i.e. the feed token and API key)
via its own "websockets.client" logger at DEBUG
(websockets/client.py: `self.logger.debug("> GET %s HTTP/1.1", request.path)`).

This service's LOG_LEVEL defaults to INFO, so by default the line is
suppressed — this is a lower-severity, conditional leak compared to session98's
always-on-at-INFO Telegram-token leak. But LOG_LEVEL=DEBUG is a real, supported
config knob, and turning it on for any unrelated reason (debugging a different
issue) would silently put the feed token and API key into the log. ws_client.py
now installs a redacting filter on "websockets.client" at import, closing it
regardless of LOG_LEVEL.

The tests use REAL websockets client/server connections (no mocking of the
library's logging) so the genuine DEBUG request-line log is produced.

Run from services/position-stocks-service:
    python -m pytest tests/test_ws_client_secret_redaction.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import websockets

import feed.ws_client as ws_client  # noqa: E402  (installs the filter at import)

FEED_TOKEN = "SECRETfeedTOKENsecretFEEDtoken123"
API_KEY = "SECRETapiKEYsecretAPIkey456"


async def _echo(websocket):
    async for _ in websocket:
        pass


@pytest.mark.asyncio
async def test_feed_token_and_api_key_never_reach_the_client_log(caplog):
    client_logger = logging.getLogger("websockets.client")
    prev_propagate = client_logger.propagate
    client_logger.propagate = True  # so caplog (attached to the root handler) sees it
    try:
        with caplog.at_level(logging.DEBUG, logger="websockets.client"):
            server = await websockets.serve(_echo, "localhost", 0)
            port = server.sockets[0].getsockname()[1]
            url = (
                f"ws://localhost:{port}/smart-stream"
                f"?clientCode=C1&feedToken={FEED_TOKEN}&apiKey={API_KEY}"
            )
            async with websockets.connect(url):
                pass
            server.close()
            await server.wait_closed()
    finally:
        client_logger.propagate = prev_propagate

    assert FEED_TOKEN not in caplog.text
    assert API_KEY not in caplog.text
    assert "feedToken=***" in caplog.text
    assert "apiKey=***" in caplog.text


def test_filter_install_is_idempotent():
    ws_client._install_ws_secret_filter()
    ws_client._install_ws_secret_filter()
    for name in ("websockets.client", "position-stocks-ws-client"):
        filters = [
            f for f in logging.getLogger(name).filters
            if isinstance(f, ws_client._SecretRedactingFilter)
        ]
        assert len(filters) == 1, name


def test_other_websockets_client_log_lines_are_untouched(caplog):
    with caplog.at_level(logging.DEBUG, logger="websockets.client"):
        logging.getLogger("websockets.client").debug("= connection is OPEN")
    assert "= connection is OPEN" in caplog.text


@pytest.mark.parametrize("text,expected", [
    ("/smart-stream?clientCode=C1&feedToken=ABC123&apiKey=XYZ789", "feedToken=***"),
    ("/smart-stream?clientCode=C1&feedToken=ABC123&apiKey=XYZ789", "apiKey=***"),
    ("no secrets here", "no secrets here"),
    (None, "None"),
])
def test_redact_secrets_table(text, expected):
    assert expected in ws_client._redact_secrets(text)
