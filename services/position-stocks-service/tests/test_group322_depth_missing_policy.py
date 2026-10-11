"""group322: a symbol with no depth reading is refused once depth has been seen flowing (ENTRY_DEPTH_MISSING=auto)."""
from __future__ import annotations

import time

import pytest

import config
from orders import depth_gate


class Resp:
    def __init__(self, code=200, body=None):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


@pytest.fixture
def md(monkeypatch):
    depth_gate._cache.clear()
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", True)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_SPREAD_PCT", 0.5)
    monkeypatch.setattr(config, "ENTRY_MIN_BOOK_VALUE", 0.0)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_QUOTE_AGE_S", 0.0)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MISSING", "auto")
    monkeypatch.setattr(config, "ENTRY_DEPTH_PROVEN_WINDOW_S", 21600.0)
    st = {"bodies": {}, "code": 200}

    class Client:
        def __init__(self, timeout=None):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            sym = url.rsplit("/", 1)[-1]
            if st["code"] != 200:
                return Resp(st["code"], None)
            return Resp(200, dict(st["bodies"].get(sym, {"source": "dhan"})))
    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    yield st
    depth_gate._cache.clear()


def test_auto_allows_until_depth_seen(md):
    assert depth_gate.reject_reason("AAA") is None          # no depth ever seen -> allow (old behaviour)


def test_auto_refuses_missing_after_depth_seen(md):
    md["bodies"]["GOOD"] = {"spread_pct": 0.1, "book_value_5": 1e6, "source": "dhan"}
    assert depth_gate.reject_reason("GOOD") is None          # proves depth is flowing
    r = depth_gate.reject_reason("NODEPTH")
    assert r and r.startswith("DEPTH_MISSING")


def test_auto_refuses_when_market_data_unreachable_after_proof(md):
    md["bodies"]["GOOD"] = {"spread_pct": 0.1, "book_value_5": 1e6}
    depth_gate.reject_reason("GOOD")
    md["code"] = 503
    r = depth_gate.reject_reason("OTHER")
    assert r and r.startswith("DEPTH_MISSING")


def test_proof_expires_after_window(md, monkeypatch):
    md["bodies"]["GOOD"] = {"spread_pct": 0.1, "book_value_5": 1e6}
    depth_gate.reject_reason("GOOD")
    depth_gate._depth_proven_at = time.time() - 7200
    monkeypatch.setattr(config, "ENTRY_DEPTH_PROVEN_WINDOW_S", 3600.0)
    assert depth_gate.reject_reason("NODEPTH") is None


def test_window_zero_never_expires(md, monkeypatch):
    md["bodies"]["GOOD"] = {"spread_pct": 0.1}
    depth_gate.reject_reason("GOOD")
    depth_gate._depth_proven_at = time.time() - 10 * 86400
    monkeypatch.setattr(config, "ENTRY_DEPTH_PROVEN_WINDOW_S", 0.0)
    assert depth_gate.reject_reason("NODEPTH").startswith("DEPTH_MISSING")


def test_allow_mode_never_refuses(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_MISSING", "allow")
    md["bodies"]["GOOD"] = {"spread_pct": 0.1}
    depth_gate.reject_reason("GOOD")
    assert depth_gate.reject_reason("NODEPTH") is None


def test_refuse_mode_refuses_without_proof(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_MISSING", "refuse")
    assert depth_gate.reject_reason("NODEPTH").startswith("DEPTH_MISSING")


def test_wide_spread_still_reported_as_spread(md):
    md["bodies"]["WIDE"] = {"spread_pct": 0.9, "book_value_5": 1e6, "source": "dhan"}
    assert depth_gate.reject_reason("WIDE").startswith("DEPTH_SPREAD")


def test_gate_off_never_refuses(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_MISSING", "refuse")
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", False)
    assert depth_gate.reject_reason("NODEPTH") is None
