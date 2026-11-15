"""tests/test_round539_extra_index_tiles.py - /market/indices carries Bank Nifty and India VIX as optional extra tiles (round 539, B2).

The tiles are additive: separate keys, never in the market score, and a failure, a timeout or the switch being off leaves the Nifty / Sensex body exactly as before.
Run from services/api-gateway:  python3 -m pytest tests/test_round539_extra_index_tiles.py -v
"""
from __future__ import annotations

import os
import time
from datetime import datetime

import pandas as pd
import pytest

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


class KV:
    def __init__(self):
        self.store, self.sets = {}, []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ttl=None):
        self.sets.append((key, value, ttl))
        self.store[key] = value


@pytest.fixture
def kv(monkeypatch):
    k = KV()
    monkeypatch.setattr(gw, "_redis_get", k.get)
    monkeypatch.setattr(gw, "_redis_set", k.set)
    monkeypatch.delenv("INDICES_EXTRA_TILES", raising=False)
    return k


def _frame(closes):
    end = pd.Timestamp(datetime.now(gw.IST).date()) - pd.Timedelta(days=1)   # last row is a finished session
    idx = pd.date_range(end=end, periods=len(closes), freq="D", tz="Asia/Kolkata")
    return pd.DataFrame({"Open": closes, "Close": closes}, index=idx)


class Tick:
    def __init__(self, table):
        self.table = table

    def __call__(self, symbol):
        outer = self

        class T:
            def history(self, period="1d", **kw):
                v = outer.table.get(symbol)
                if isinstance(v, Exception):
                    raise v
                return v if v is not None else pd.DataFrame()
        return T()


def test_both_tiles_use_the_previous_session_close(kv, monkeypatch):
    monkeypatch.setattr(gw, "_IndexTicker", Tick({"^NSEBANK": _frame([50000.0, 50500.0]), "^INDIAVIX": _frame([14.0, 13.3])}))
    out = gw._extra_index_tiles()
    assert out["banknifty"] == {"price": 50500.0, "change": 500.0, "change_pct": 1.0}
    assert out["vix"]["price"] == 13.3 and out["vix"]["change"] == -0.7 and out["vix"]["change_pct"] == -5.0


def test_one_missing_tile_does_not_drop_the_other(kv, monkeypatch):
    monkeypatch.setattr(gw, "_IndexTicker", Tick({"^NSEBANK": _frame([100.0, 101.0]), "^INDIAVIX": RuntimeError("down")}))
    out = gw._extra_index_tiles()
    assert list(out) == ["banknifty"]


def test_everything_failing_gives_empty_and_a_15_minute_negative_cache(kv, monkeypatch):
    calls = []
    def boom(symbol):
        calls.append(symbol)
        raise RuntimeError("down")
    monkeypatch.setattr(gw, "_IndexTicker", boom)
    assert gw._extra_index_tiles() == {}
    assert kv.sets[-1][0] == gw.INDICES_EXTRA_CACHE_KEY and kv.sets[-1][2] == 900
    n = len(calls)
    assert gw._extra_index_tiles() == {} and len(calls) == n, "the second call is served from the negative cache"


def test_a_slow_source_is_given_up_on_after_the_deadline(kv, monkeypatch):
    class Slow:
        def __init__(self, symbol): pass
        def history(self, period="1d", **kw):
            time.sleep(1.0)
            return _frame([1.0, 2.0])
    monkeypatch.setattr(gw, "_IndexTicker", Slow)
    monkeypatch.setattr(gw, "EXTRA_INDICES_DEADLINE_S", 0.1)
    t0 = time.time()
    assert gw._extra_index_tiles() == {}
    assert time.time() - t0 < 0.8


def test_switch_off_fetches_nothing(kv, monkeypatch):
    monkeypatch.setenv("INDICES_EXTRA_TILES", "0")
    monkeypatch.setattr(gw, "_IndexTicker", lambda s: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert gw._extra_index_tiles() == {}


def test_endpoint_body_keeps_nifty_sensex_and_score_and_adds_the_tiles(kv, monkeypatch):
    nifty = pd.DataFrame({"Open": [100.0], "Close": [101.0]}, index=pd.DatetimeIndex([pd.Timestamp.now(tz="Asia/Kolkata")]))
    monkeypatch.setattr(gw, "_IndexTicker", Tick({"^NSEI": nifty, "^BSESN": nifty, "^NSEBANK": _frame([10.0, 11.0]), "^INDIAVIX": _frame([15.0, 15.0])}))
    body = __import__("json").loads(gw.get_market_indices(force_refresh=True).body)
    assert {"nifty", "sensex", "market_mood", "market_score"} <= set(body)
    assert body["banknifty"]["price"] == 11.0 and body["vix"]["change_pct"] == 0.0
    off = dict(body); off.pop("banknifty"); off.pop("vix")
    monkeypatch.setenv("INDICES_EXTRA_TILES", "0")
    kv.store.clear()
    body2 = __import__("json").loads(gw.get_market_indices(force_refresh=True).body)
    assert "banknifty" not in body2 and body2["market_score"] == body["market_score"], "the score is the same with and without the tiles"
