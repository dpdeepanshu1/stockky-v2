"""E379 / E380 / E381: analyze() must not 500 on non-finite overrides or NaN / missing volume, and must not cache a structure-less result for the
full TTL. technical/main.py is loaded under its own module name; fastapi / httpx are stubbed only when they are not installed."""
from __future__ import annotations

import importlib.util
import json
import math
import os
import sys
import types

import numpy as np
import pandas as pd
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(HERE, "technical", "main.py")


from ._tech_loader import load as _loadmod


def _load():
    return _loadmod("technical_main_e379")


@pytest.fixture()
def tm(monkeypatch):
    m = _load()
    m._mem_tech.clear()
    m._mem_tech_exp.clear()
    monkeypatch.setattr(m, "_ms_analyze", None)          # structure off unless a test turns it on
    monkeypatch.setattr(m, "_ms_flatten", None)
    return m


def _df(n=80, volume=True):
    idx = pd.bdate_range("2026-06-01", periods=n)
    rng = np.random.default_rng(7)
    c = 100 + np.cumsum(rng.normal(0.2, 1.0, n))
    d = pd.DataFrame({"Open": c - 0.2, "High": c + 1, "Low": c - 1, "Close": c}, index=idx)
    if volume:
        d["Volume"] = 1000.0 + np.arange(n)
    return d


def _run(tm, monkeypatch, df, **kw):
    monkeypatch.setattr(tm, "_fetch_history", lambda s, force=False: df)
    return tm.analyze("TESTCO", force=True, lite=False, **kw) if "force" not in kw else tm.analyze("TESTCO", **kw)


def _json_ok(out):
    json.dumps(out, allow_nan=False)      # what the response serializer does


def test_e379_nan_inf_overrides_use_defaults(tm, monkeypatch):
    base = _run(tm, monkeypatch, _df())
    for kw in ({"meanrev_weight": float("nan")}, {"trend_weight": float("inf")}, {"rsi_oversold": float("nan")},
               {"rsi_overbought": float("-inf")}, {"extended_1m_pct": float("nan")}, {"extended_short_pct": float("inf")}):
        out = _run(tm, monkeypatch, _df(), **kw)
        _json_ok(out)
        assert out["technical_score"] == base["technical_score"], kw
        assert out["adaptive_params_used"]["trend_weight"] == 1.0 and out["adaptive_params_used"]["meanrev_weight"] == 1.0


def test_e379_nonfinite_override_is_not_treated_as_override(tm, monkeypatch):
    stored = []
    real = tm._cache_set
    monkeypatch.setattr(tm, "_cache_set", lambda k, v, ttl=None: (stored.append(k), real(k, v, ttl=ttl))[1])
    _run(tm, monkeypatch, _df(), meanrev_weight=float("nan"))
    assert "tech_analysis:TESTCO" in stored          # same as the static defaults, so the shared cache entry is used


def test_e380_nan_last_volume_and_missing_volume_column(tm, monkeypatch):
    d = _df()
    d.iloc[-1, d.columns.get_loc("Volume")] = float("nan")
    out = _run(tm, monkeypatch, d)
    _json_ok(out)
    assert out["volume_ratio"] is None and out["volume_surge"] is False
    out2 = _run(tm, monkeypatch, _df(volume=False))
    _json_ok(out2)
    assert out2["volume_ratio"] is None
    ok = _run(tm, monkeypatch, _df())
    assert ok["volume_ratio"] is not None and math.isfinite(ok["volume_ratio"])


def test_e381_missing_structure_is_cached_briefly(tm, monkeypatch):
    seen = {}
    real = tm._cache_set
    monkeypatch.setattr(tm, "_cache_set", lambda k, v, ttl=None: (seen.__setitem__(k, ttl), real(k, v, ttl=ttl))[1])
    monkeypatch.setattr(tm, "_ms_analyze", lambda *a, **k: None)
    monkeypatch.setattr(tm, "_structure_enabled", lambda: True)
    monkeypatch.setattr(tm, "get_cache_ttl", lambda: 21600)
    monkeypatch.setattr(tm, "_structure_cached", lambda *a, **k: (None, None))
    _run(tm, monkeypatch, _df())
    assert seen["tech_analysis:TESTCO"] == tm._INSUFFICIENT_CACHE_TTL
    seen.clear()
    monkeypatch.setattr(tm, "_structure_cached", lambda *a, **k: ({"quality": "ok"}, {"x": 1}))
    _run(tm, monkeypatch, _df())
    assert seen["tech_analysis:TESTCO"] is None       # a real block keeps the normal TTL
