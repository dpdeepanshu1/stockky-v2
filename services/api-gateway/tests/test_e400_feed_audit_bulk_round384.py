"""Round 384: E400 - _compute_feed_audit reads the feed rows with one get_symbols_bulk() call instead of one get_symbol() per symbol."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.dirname(HERE)
sys.path.insert(0, GW)

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


def _good(price=100):
    return {"price": price, "rsi": 50, "pe_ratio": 20, "roce": 15, "sentiment_score": 0}


class _PlainStore:
    """No get_symbols_bulk: the audit must use get_symbol per symbol, as before."""
    def __init__(self, symbols, rows):
        self.symbols, self.rows, self.asked = symbols, rows, []

    def list_symbols(self):
        return self.symbols

    def get_symbol(self, sym):
        self.asked.append(sym)
        return self.rows.get(sym)


class _BulkStore(_PlainStore):
    def __init__(self, symbols, rows, bulk_raises=False, bulk_returns=None):
        super().__init__(symbols, rows)
        self.bulk_calls, self.bulk_raises, self.bulk_returns = [], bulk_raises, bulk_returns

    def get_symbols_bulk(self, symbols):
        self.bulk_calls.append(list(symbols))
        if self.bulk_raises:
            raise RuntimeError("neon down")
        if self.bulk_returns is not None:
            return self.bulk_returns
        return {s: dict(self.rows[s]) for s in symbols if s in self.rows}   # only symbols that have data, like the real one


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(gw, "_AUDIT_MEMO", {})
    monkeypatch.setattr(gw, "AUDIT_TTL_SEC", 20.0)
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)


def _audit(monkeypatch, store, limit=500):
    monkeypatch.setattr(gw, "_feed_store", lambda: store)
    return gw._compute_feed_audit(limit)


def _strip(out):
    return {k: v for k, v in out.items() if k != "cached"}


ROWS = {"AAA": _good(), "BBB": {"price": 10}}
SYMS = ["AAA", "BBB", "CCC", "tcs.ns"]


def test_e400_one_bulk_call_and_no_per_symbol_reads(monkeypatch):
    s = _BulkStore(list(SYMS), dict(ROWS))
    out = _audit(monkeypatch, s)
    assert s.bulk_calls == [["AAA", "BBB", "CCC", "TCS"]]       # cleaned, de-duplicated symbols, once
    assert s.asked == []                                           # a symbol with no data is not re-read one by one
    assert out["total_universe"] == 4 and out["fully_populated"] == 1


def test_e400_result_is_identical_to_the_per_symbol_path(monkeypatch):
    a = _audit(monkeypatch, _BulkStore(list(SYMS), dict(ROWS)))
    monkeypatch.setattr(gw, "_AUDIT_MEMO", {})
    b = _audit(monkeypatch, _PlainStore(list(SYMS), dict(ROWS)))
    assert _strip(a) == _strip(b)


@pytest.mark.parametrize("bad", ["raises", "not_a_dict"])
def test_e400_bulk_failure_falls_back_to_get_symbol(monkeypatch, bad):
    s = _BulkStore(list(SYMS), dict(ROWS), bulk_raises=(bad == "raises"), bulk_returns=(["x"] if bad == "not_a_dict" else None))
    out = _audit(monkeypatch, s)
    assert s.asked == ["AAA", "BBB", "CCC", "TCS"]
    assert out["fully_populated"] == 1 and out["incomplete_count"] == 3


def test_e400_store_without_a_bulk_reader_still_works(monkeypatch):
    s = _PlainStore(["AAA", "BBB"], dict(ROWS))
    out = _audit(monkeypatch, s)
    assert s.asked == ["AAA", "BBB"] and out["fully_populated"] == 1


def test_e400_empty_universe_makes_no_bulk_call(monkeypatch):
    import kv_cache
    monkeypatch.setattr(kv_cache, "kv_get", lambda k: None)
    s = _BulkStore([], {})
    out = _audit(monkeypatch, s)
    assert s.bulk_calls == [] and out["total_universe"] == 0


def test_e400_non_dict_bulk_row_counts_as_empty(monkeypatch):
    s = _BulkStore(["AAA", "BBB"], {}, bulk_returns={"AAA": "junk", "BBB": _good()})
    out = _audit(monkeypatch, s)
    assert out["fully_populated"] == 1 and [x["symbol"] for x in out["incomplete_stocks"]] == ["AAA"]


def test_e400_bulk_row_over_the_price_cap_is_reported_separately(monkeypatch):
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 500.0)
    s = _BulkStore(["AAA", "BIG"], {"AAA": _good(100), "BIG": _good(9000)})
    out = _audit(monkeypatch, s)
    assert out["over_cap_count"] == 1 and out["over_cap_stocks"][0]["symbol"] == "BIG" and out["fully_populated"] == 1
