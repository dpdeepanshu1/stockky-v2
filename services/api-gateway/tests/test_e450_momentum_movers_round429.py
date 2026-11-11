"""E450 (round 429): _compute_momentum_movers.
(1) A failure while building the fallback seed (index list / securities list / cursor slice) no longer raises out of the function and drops the
    movers steps 1-3 already collected.
(2) An empty pass is remembered for a short window (MOMENTUM_MOVERS_EMPTY_TTL_S, default 20 s, 0 = off), so sequential callers do not each repeat the
    AngelOne sweep and the seed fallback while every source is down."""
import pytest

import main as gw


class _Resp:
    def __init__(self, data):
        self.status_code = 200
        self._d = data

    def json(self):
        return self._d


@pytest.fixture
def mm(monkeypatch):
    st = type("S", (), {})()
    st.sets, st.http_calls, st.angel_rows, st.bulk_calls = [], 0, [], []
    st.nifty_raises = False
    monkeypatch.delenv("MOMENTUM_MOVERS_EMPTY_TTL_S", raising=False)
    gw._MM_EMPTY_UNTIL[0] = 0.0
    monkeypatch.setattr(gw, "_redis_get", lambda *a, **k: None)
    monkeypatch.setattr(gw, "_redis_set", lambda k, v, ttl=None: st.sets.append((k, v)))
    monkeypatch.setattr(gw, "_fetch_nse_board", lambda spec, key: (None, spec))

    def fake_get(url, timeout=None):
        st.http_calls += 1
        return _Resp({"status": "ok", "data": [{"symbol": s} for s in st.angel_rows]})

    monkeypatch.setattr(gw.httpx, "get", fake_get)
    for n in ("market_top_gainers", "market_top_losers", "market_most_active"):
        monkeypatch.setattr(gw, n, lambda: {"data": []})
    monkeypatch.setattr(gw, "_get_nifty50_data", lambda: [])

    def nifty():
        if st.nifty_raises:
            raise RuntimeError("NSE index lists down")
        return [f"N{i}" for i in range(60)]

    monkeypatch.setattr(gw, "_get_nifty_indices", nifty)
    monkeypatch.setattr(gw, "_get_all_nse_securities", lambda: [f"G{i}" for i in range(300)])
    import data_feed
    monkeypatch.setattr(data_feed, "bulk_yahoo_download_prices", lambda seed: (st.bulk_calls.append(list(seed)) or {}))
    yield st
    gw._MM_EMPTY_UNTIL[0] = 0.0


def test_a_seed_failure_keeps_the_movers_already_collected(mm):
    mm.angel_rows = ["AAA", "BBB"]
    mm.nifty_raises = True
    out = gw._compute_momentum_movers()
    assert out == ["AAA", "BBB"]
    assert mm.bulk_calls == []                      # no seed -> no pointless bulk call
    assert mm.sets and mm.sets[-1][1] == ["AAA", "BBB"]


def test_a_seed_failure_with_nothing_collected_is_an_empty_list_not_an_error(mm):
    mm.nifty_raises = True
    assert gw._compute_momentum_movers() == []


def test_empty_pass_is_remembered_for_a_short_window(mm):
    assert gw._compute_momentum_movers() == []
    calls = mm.http_calls
    assert gw._compute_momentum_movers() == []
    assert mm.http_calls == calls                   # second call did not repeat the sweep
    assert gw._MM_EMPTY_UNTIL[0] > 0


def test_window_expires_and_a_non_empty_pass_clears_it(mm):
    gw._compute_momentum_movers()
    gw._MM_EMPTY_UNTIL[0] = 1.0                     # long past
    mm.angel_rows = ["AAA"]
    assert gw._compute_momentum_movers() == ["AAA"]
    assert gw._MM_EMPTY_UNTIL[0] == 0.0


def test_empty_window_can_be_switched_off(mm, monkeypatch):
    monkeypatch.setenv("MOMENTUM_MOVERS_EMPTY_TTL_S", "0")
    gw._compute_momentum_movers()
    calls = mm.http_calls
    gw._compute_momentum_movers()
    assert mm.http_calls == calls + 1


def test_junk_env_value_uses_the_default(mm, monkeypatch):
    monkeypatch.setenv("MOMENTUM_MOVERS_EMPTY_TTL_S", "soon")
    gw._compute_momentum_movers()
    assert gw._MM_EMPTY_UNTIL[0] > 0


def test_window_never_exceeds_the_normal_cache_ttl(mm, monkeypatch):
    monkeypatch.setenv("MOMENTUM_MOVERS_EMPTY_TTL_S", "100000")
    t0 = gw.time.time()
    gw._compute_momentum_movers()
    assert gw._MM_EMPTY_UNTIL[0] - t0 <= gw.MOMENTUM_MOVERS_CACHE_TTL + 2

