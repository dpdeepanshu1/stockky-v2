"""Group 280: orders/depth_gate - Dhan 5-level depth check before an entry (fails open)."""
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
    depth_gate._cache.clear()
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", True)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_SPREAD_PCT", 0.5)
    monkeypatch.setattr(config, "ENTRY_MIN_BOOK_VALUE", 0.0)
    st = {"calls": [], "resp": Resp(200, {"spread_pct": 0.2, "book_value_5": 500000.0, "source": "dhan"}), "exc": None}

    class Client:
        def __init__(self, timeout=None):
            st["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url):
            st["calls"].append(url)
            if st["exc"]:
                raise st["exc"]
            return st["resp"]
    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    return st


def test_good_depth_passes(md):
    assert depth_gate.reject_reason("ABC") is None
    assert md["calls"] == [f"{config.MARKET_DATA_URL}/quote/ABC"] and md["timeout"] == config.ENTRY_DEPTH_TIMEOUT_S


def test_wide_spread_is_rejected(md):
    md["resp"] = Resp(200, {"spread_pct": 0.9, "book_value_5": 1e6, "source": "dhan"})
    r = depth_gate.reject_reason("ABC")
    assert r.startswith("DEPTH_SPREAD:0.90% > 0.50%") and "dhan" in r


def test_spread_exactly_at_the_limit_passes(md):
    md["resp"] = Resp(200, {"spread_pct": 0.5})
    assert depth_gate.reject_reason("ABC") is None


def test_thin_book_is_rejected_only_when_a_minimum_is_set(md, monkeypatch):
    md["resp"] = Resp(200, {"spread_pct": 0.1, "book_value_5": 20000.0})
    assert depth_gate.reject_reason("ABC") is None                       # default: off
    depth_gate._cache.clear()
    monkeypatch.setattr(config, "ENTRY_MIN_BOOK_VALUE", 50000.0)
    assert depth_gate.reject_reason("ABC").startswith("DEPTH_THIN_BOOK:")


@pytest.mark.parametrize("body", [{}, {"price": 10}, {"spread_pct": None, "book_value_5": None},
                                  {"spread_pct": "x"}, {"spread_pct": -1}, {"spread_pct": float("nan")}])
def test_unknown_or_bad_depth_never_blocks(md, monkeypatch, body):
    monkeypatch.setattr(config, "ENTRY_MIN_BOOK_VALUE", 50000.0)
    md["resp"] = Resp(200, body)
    assert depth_gate.reject_reason("ABC") is None


@pytest.mark.parametrize("resp", [Resp(404, {}), Resp(503, None), Resp(200, None), Resp(200, ["x"])])
def test_bad_answers_never_block(md, resp):
    md["resp"] = resp
    assert depth_gate.reject_reason("ABC") is None


def test_exception_never_blocks(md):
    md["exc"] = RuntimeError("down")
    assert depth_gate.reject_reason("ABC") is None


def test_switches_off_make_no_call(md, monkeypatch):
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", False)
    assert depth_gate.reject_reason("ABC") is None and md["calls"] == []
    monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", True)
    monkeypatch.setattr(config, "ENTRY_DEPTH_MAX_SPREAD_PCT", 0.0)
    assert depth_gate.reject_reason("ABC") is None and md["calls"] == []


def test_answer_is_cached_briefly(md):
    depth_gate.reject_reason("ABC")
    depth_gate.reject_reason("ABC")
    assert len(md["calls"]) == 1


def test_entry_skips_and_releases_the_lock(monkeypatch):
    """attempt_entry wiring is covered in test_entry.py::TestDepthGateWiring."""
    assert hasattr(depth_gate, "reject_reason")


# ── group283: size down from the best-5 book ───────────────────────────────────
class TestMaxQtyFromBook:
    @pytest.fixture(autouse=True)
    def _on(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_BOOK_MAX_SHARE_PCT", 10.0)

    def test_caps_at_share_of_one_side(self, md):
        md["resp"] = Resp(200, {"book_value_5": 400000.0})     # one side 200,000 -> 10% = 20,000 -> 200 sh @100
        assert depth_gate.max_qty_from_book("ABC", 100.0) == 200

    def test_never_below_one_share(self, md):
        md["resp"] = Resp(200, {"book_value_5": 1000.0})
        assert depth_gate.max_qty_from_book("ABC", 5000.0) == 1

    def test_off_by_default_makes_no_call(self, md, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_BOOK_MAX_SHARE_PCT", 0.0)
        assert depth_gate.max_qty_from_book("ABC", 100.0) is None and md["calls"] == []

    def test_depth_gate_off_means_no_cap(self, md, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_DEPTH_GATE", False)
        assert depth_gate.max_qty_from_book("ABC", 100.0) is None and md["calls"] == []

    @pytest.mark.parametrize("body", [{}, {"book_value_5": None}, {"book_value_5": 0}, {"book_value_5": "x"}])
    def test_unknown_depth_never_shrinks(self, md, body):
        md["resp"] = Resp(200, body)
        assert depth_gate.max_qty_from_book("ABC", 100.0) is None

    @pytest.mark.parametrize("ltp", [0, -1, None])
    def test_bad_price_no_cap(self, md, ltp):
        assert depth_gate.max_qty_from_book("ABC", ltp) is None

    def test_exception_never_shrinks(self, md):
        md["exc"] = RuntimeError("down")
        assert depth_gate.max_qty_from_book("ABC", 100.0) is None
