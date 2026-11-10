"""Round 280: borrowing the Dhan token from real-trade-service survives a cut-short or odd response (a CredsError, never a crash)."""
from __future__ import annotations

import http.client
import io
import json
import urllib.error

import pytest

from fno import creds


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()


def _opener(body):
    return lambda req, timeout=0: _Resp(body)


def test_a_cut_short_response_is_a_creds_error():
    with pytest.raises(creds.CredsError, match="cannot reach"):
        creds.fetch("http://rt", "tok", opener=_opener(http.client.IncompleteRead(b"{", 10)))


@pytest.mark.parametrize("body", [[1, 2], "text", 7, None])
def test_a_body_that_is_not_an_object_is_a_creds_error(body):
    with pytest.raises(creds.CredsError):
        creds.fetch("http://rt", "tok", opener=_opener(body if body is not None else b"null"))


def test_an_error_body_cut_short_still_gives_the_http_message():
    class Cut(io.BytesIO):
        def read(self, *a):
            raise http.client.IncompleteRead(b"", 5)

    def op(req, timeout=0):
        raise urllib.error.HTTPError("http://rt", 500, "x", {}, Cut())
    with pytest.raises(creds.CredsError, match="said 500"):
        creds.fetch("http://rt", "tok", opener=op)


def test_a_non_object_error_body_is_not_a_crash():
    def op(req, timeout=0):
        raise urllib.error.HTTPError("http://rt", 503, "x", {}, io.BytesIO(b"[1]"))
    with pytest.raises(creds.CredsError, match="said 503"):
        creds.fetch("http://rt", "tok", opener=op)


def test_good_credentials_still_work():
    c = creds.fetch("http://rt", "tok", opener=_opener({"client_id": "1", "access_token": "t", "base_url": "https://x/v2", "dhan_env": "live"}))
    assert c == {"client_id": "1", "access_token": "t", "base_url": "https://x/v2", "dhan_env": "live"}
