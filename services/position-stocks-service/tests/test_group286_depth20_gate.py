"""Group 286: orders/depth_gate.max_qty_from_depth20 - size an entry down from Dhan's 20-level ask book (fails open)."""
from __future__ import annotations

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
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", True)
    monkeypatch.setattr(config, "ENTRY_DEPTH20_SLIP_PCT", 0.3)
    monkeypatch.setattr(config, "ENTRY_DEPTH20_MAX_SHARE_PCT", 50.0)
    monkeypatch.setattr(config, "ENTRY_DEPTH20_WAIT_S", 1.0)
    st = {"calls": [], "resp": Resp(200, {"available": True, "buy_qty_within_slip": 1000, "buy_impact_pct": 0.1}),
          "exc": None}

    class Client:
        def __init__(self, timeout=None):
            st["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            st["calls"].append((url, params))
            if st["exc"]:
                raise st["exc"]
            return st["resp"]
    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    return st


def test_caps_at_share_of_ask_shares_within_slip(md):
    assert depth_gate.max_qty_from_depth20("ABC", 800) == 500          # 50% of 1000
    url, params = md["calls"][0]
    assert url == f"{config.MARKET_DATA_URL}/depth/ABC"
    assert params == {"qty": 800, "slip_pct": 0.3, "wait_s": 1.0}
    assert md["timeout"] == config.ENTRY_DEPTH_TIMEOUT_S + 1.0


def test_cap_above_quantity_is_returned_as_is(md):
    assert depth_gate.max_qty_from_depth20("ABC", 100) == 500          # caller only shrinks when cap < quantity


def test_never_below_one_share(md):
    md["resp"] = Resp(200, {"available": True, "buy_qty_within_slip": 1})
    assert depth_gate.max_qty_from_depth20("ABC", 50) == 1


def test_off_by_default_makes_no_call(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH20_SLIP_PCT", 0.0)
    assert depth_gate.max_qty_from_depth20("ABC", 800) is None and md["calls"] == []


def test_depth_gate_off_means_no_cap(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", False)
    assert depth_gate.max_qty_from_depth20("ABC", 800) is None and md["calls"] == []


def test_zero_share_pct_means_no_cap(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH20_MAX_SHARE_PCT", 0.0)
    assert depth_gate.max_qty_from_depth20("ABC", 800) is None and md["calls"] == []


@pytest.mark.parametrize("q", [0, -5, None])
def test_bad_quantity_no_call(md, q):
    assert depth_gate.max_qty_from_depth20("ABC", q) is None and md["calls"] == []


@pytest.mark.parametrize("body", [
    {"available": False, "reason": "warming"}, {"available": False, "reason": "depth20_disabled"},
    {"available": True}, {"available": True, "buy_qty_within_slip": None},
    {"available": True, "buy_qty_within_slip": "x"}, {"buy_qty_within_slip": 1000}, [], "oops"])
def test_unknown_or_unavailable_depth_never_shrinks(md, body):
    md["resp"] = Resp(200, body)
    assert depth_gate.max_qty_from_depth20("ABC", 800) is None


def test_non_200_never_shrinks(md):
    md["resp"] = Resp(503, {"available": True, "buy_qty_within_slip": 10})
    assert depth_gate.max_qty_from_depth20("ABC", 800) is None


def test_exception_never_shrinks(md):
    md["exc"] = RuntimeError("down")
    assert depth_gate.max_qty_from_depth20("ABC", 800) is None


def test_zero_shares_within_slip_gives_one(md):
    md["resp"] = Resp(200, {"available": True, "buy_qty_within_slip": 0})
    assert depth_gate.max_qty_from_depth20("ABC", 800) == 1
