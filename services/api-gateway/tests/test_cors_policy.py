"""group 454: the gateway's CORS allowlist (cors_policy.py)."""
import pytest

import cors_policy as cp


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GATEWAY_CORS_ORIGINS", raising=False)


class Req:
    def __init__(self, origin=None):
        self.headers = {"origin": origin} if origin else {}


def test_unset_is_wildcard_like_before(monkeypatch):
    assert cp.is_wildcard()
    assert cp.origin_allowed("https://anything.example")
    assert cp.response_headers(Req("https://anything.example")) == cp.WILDCARD_HEADERS
    assert cp.response_headers(None) == cp.WILDCARD_HEADERS


@pytest.mark.parametrize("raw", ["", "  ", ",,", "*", "https://a.example,*"])
def test_empty_or_star_means_wildcard(monkeypatch, raw):
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", raw)
    assert cp.is_wildcard()


def test_allowlist_exact_match(monkeypatch):
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://Stockky.example.com/, http://localhost:5173")
    assert not cp.is_wildcard()
    assert cp.origin_allowed("https://stockky.example.com")
    assert cp.origin_allowed("https://stockky.example.com/")
    assert cp.origin_allowed("http://localhost:5173")
    assert not cp.origin_allowed("http://localhost:5174")          # other port
    assert not cp.origin_allowed("http://stockky.example.com")     # other scheme
    assert not cp.origin_allowed("https://stockky.example.com.evil.io")
    assert not cp.origin_allowed("https://evil.io")
    assert not cp.origin_allowed("")


def test_error_response_headers_follow_the_allowlist(monkeypatch):
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://stockky.example.com")
    assert cp.response_headers(Req("https://stockky.example.com")) == {
        "Access-Control-Allow-Origin": "https://stockky.example.com", "Vary": "Origin"}
    assert cp.response_headers(Req("https://evil.io")) == {}
    assert cp.response_headers(Req()) == {}
    assert cp.response_headers(None) == {}


def test_gateway_wildcard_middleware_unchanged_by_default():
    import asyncio
    import main as gw

    class Resp:
        headers = {}

    async def call_next(_):
        return Resp()

    out = asyncio.run(gw.add_cors_header("REQ", call_next))
    assert out.headers == cp.WILDCARD_HEADERS


def test_gateway_middleware_does_not_widen_an_allowlist(monkeypatch):
    import asyncio
    import main as gw
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://stockky.example.com")

    class Resp:
        headers = {}

    async def call_next(_):
        return Resp()

    out = asyncio.run(gw.add_cors_header("REQ", call_next))
    assert out.headers == {}


def test_websocket_refuses_other_origins(monkeypatch):
    import asyncio
    import main as gw
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://stockky.example.com")

    class WS:
        headers = {"origin": "https://evil.io"}
        closed = None

        async def close(self, code=1000):
            self.closed = code

    ws = WS()
    asyncio.run(gw.websocket_endpoint(ws))
    assert ws.closed == 1008
