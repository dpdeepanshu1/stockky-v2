import math
from datetime import date

import pytest

import backends
import engine


def _candles(n=120, last_day=date(2026, 10, 16), vol=1000.0):
    out = []
    d = last_day
    rows = []
    while len(rows) < n:
        if d.weekday() < 5:
            rows.append(d)
        d = date.fromordinal(d.toordinal() - 1)
    for i, day in enumerate(sorted(rows)):
        c = 100 + 3 * math.sin(i / 7.0) + i * 0.05
        out.append({"date": day.isoformat(), "open": c, "high": c + 1, "low": c - 1, "close": c, "volume": vol})
    return out


def test_prepare_bars_drops_todays_partial_bar_and_dedups():
    c = _candles(80)
    c.append(dict(c[-1]))                                   # duplicate of the last date
    today = date.fromisoformat(c[-1]["date"])
    bars = engine.prepare_bars(c, today=today)
    assert bars[-1]["date"] < today.isoformat() and len({b["date"] for b in bars}) == len(bars)


def test_prepare_bars_rejects_short_and_garbage():
    with pytest.raises(engine.BadHistory):
        engine.prepare_bars(_candles(30))
    with pytest.raises(engine.BadHistory):
        engine.prepare_bars([{"date": "x"}, "junk", None] * 50)


def test_build_record_and_features_with_fake_backend():
    rec = engine.build_record(backends.FakeBackend(), "goodco", _candles(), horizon=5, trade_mode="DEMO")
    assert rec["symbol"] == "GOODCO" and rec["fan_volume"] is not None
    p = rec["provenance"]
    assert p["backend"] == "fake" and p["as_of_bar"] == rec["as_of"] and p["context_len"] == 120 and p["trade_mode"] == "DEMO"
    f = engine.features_from_record(rec, stop_pct=3.0, target_pct=6.0)
    assert f["p_stop_close_max"] is not None and f["vol_ratio"] is not None and f["volume_pace"] is not None
    assert f["provenance"]["backend"] == "fake"


def test_no_volume_means_no_volume_forecast():
    rec = engine.build_record(backends.FakeBackend(), "IDX", _candles(vol=0.0), horizon=5)
    assert rec["fan_volume"] is None and engine.features_from_record(rec)["volume_pace"] is None


def test_a_forecast_does_not_use_bars_it_could_not_have_had():
    c = _candles(130)
    base = engine.build_record(backends.FakeBackend(), "X", c[:120], horizon=5)
    assert base["as_of"] == c[119]["date"]
    # the bar for "today" is incomplete: with today = c[120] it is dropped, so the forecast is exactly the one built on c[:120]
    withtoday = engine.build_record(backends.FakeBackend(), "X", c[:121], horizon=5, today=date.fromisoformat(c[120]["date"]))
    assert withtoday["as_of"] == base["as_of"] and withtoday["fan_close"] == base["fan_close"] and withtoday["fan_volume"] == base["fan_volume"]
    later = engine.build_record(backends.FakeBackend(), "X", c, horizon=5)
    assert later["as_of"] == c[129]["date"] and later["fan_close"] != base["fan_close"]


def test_research_backend_refuses_real_and_needs_explicit_config(monkeypatch):
    with pytest.raises(backends.BackendRefused):
        backends.Research30Backend("REAL", checkpoint="x", class_name="y")
    with pytest.raises(backends.BackendRefused):
        backends.get_backend("research-3.0", trade_mode="real")
    with pytest.raises(backends.BackendError):
        backends.Research30Backend("DEMO")                  # nothing is guessed
    assert backends.Research30Backend("DEMO", checkpoint="c", class_name="K").name == "research-3.0"


def test_unset_trade_mode_is_the_strict_reading(monkeypatch):
    monkeypatch.delenv("TSFM_TRADE_MODE", raising=False)
    monkeypatch.setenv("TSFM_RESEARCH_CHECKPOINT", "c")
    monkeypatch.setenv("TSFM_RESEARCH_CLASS", "K")
    with pytest.raises(backends.BackendRefused):
        backends.get_backend("research-3.0")


def test_unknown_backend_name_is_an_error_not_a_fallback():
    with pytest.raises(backends.BackendError):
        backends.get_backend("timesfm3")


def test_timesfm25_without_the_package_fails_cleanly():
    b = backends.TimesFM25Backend()
    with pytest.raises(backends.BackendError):
        b.forecast([[1.0, 2.0, 3.0] * 30], 5)


def test_timesfm25_adapter_reads_both_quantile_layouts(monkeypatch):
    import sys
    import types
    import numpy as np

    class _M:
        def compile(self, cfg): pass

        def forecast(self, horizon, inputs):
            n = len(inputs)
            q = np.zeros((n, horizon, 10), dtype="float32")
            for j in range(10):
                q[:, :, j] = 100 + j
            return np.full((n, horizon), 100.0), q

    class _Cls:
        @staticmethod
        def from_pretrained(name): return _M()

    mod = types.ModuleType("timesfm")
    mod.TimesFM_2p5_200M_torch = _Cls
    mod.ForecastConfig = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "timesfm", mod)
    fans = backends.TimesFM25Backend().forecast([[1.0] * 100, [2.0] * 100], 5)
    assert len(fans) == 2 and fans[0]["quantiles"]["0.1"] == [101.0] * 5 and fans[0]["quantiles"]["0.9"] == [109.0] * 5 and fans[0]["mean"] == [100.0] * 5
