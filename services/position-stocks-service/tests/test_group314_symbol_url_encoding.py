"""group314: symbols are percent-encoded when put into a market-data / analysis URL path.

'&' stays as is (M&M works today); '/', '?', '#' and spaces must not change the route."""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from feed import bar_backfill

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Resp:
    status_code = 200

    def json(self):
        return {"candles": []}


class _FakeClient:
    seen: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, params=None, **k):
        _FakeClient.seen.append(url)
        return _Resp()


@pytest.mark.parametrize("sym,tail", [
    ("RELIANCE", "RELIANCE"),
    ("M&M", "M&M"),
    ("BAJAJ-AUTO", "BAJAJ-AUTO"),
    ("A/B?x", "A%2FB%3Fx"),
    ("A B#c", "A%20B%23c"),
])
def test_backfill_url_encodes_symbol(monkeypatch, sym, tail):
    import httpx
    _FakeClient.seen = []
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    assert bar_backfill._fetch_candles(sym) == []
    assert _FakeClient.seen == [f"{config.MARKET_DATA_URL}/history/{tail}"]


# Static guard: the modules that build a per-symbol URL never interpolate a raw symbol.
_RAW = re.compile(r'f"\{config\.[A-Z_]+_URL\}/[a-z/]+/\{[a-z_.]*(symbol|sym)[a-z_]*\}')


@pytest.mark.parametrize("rel", [
    "feed/bar_backfill.py", "screening/prev_day.py", "screening/quality_gate.py", "orders/depth_gate.py",
])
def test_no_raw_symbol_in_url(rel):
    src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
    assert not _RAW.search(src)
    assert "from urllib.parse import quote" in src
