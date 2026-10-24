"""E73: D22 / D23 / D24 wired into the offline OHLCV training run (ml_wiring.py), the F&O downloader (fo_download.py) and the train.py hook.
numpy + pandas (+ scikit-learn for the smoke test, which uses a stand-in xgboost when the real one is not installed). Plain asserts."""
from __future__ import annotations

import importlib
import io
import os
import sys
import tempfile
import types
import zipfile

import numpy as np
import pandas as pd
import pytest

_T = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(_T)
sys.path.insert(0, HERE)

import fo_download as fod  # noqa: E402
import fo_features as fo  # noqa: E402
import ml_wiring as mw  # noqa: E402

CFG = {"target_type": "Log_Return", "trading": {"long_threshold": 0.0, "transaction_cost_bps": 5.0, "slippage_bps": 2.0}}


# ---------------------------------------------------------------- synthetic archive
def _bars(sym: str, n: int, seed: int, start="2025-01-01", drift=0.0006, vol=1e6, price=100.0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    close = price * np.exp(np.cumsum(rng.normal(drift, 0.015, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.004, n)))
    return pd.DataFrame({"date": dates, "symbol": sym, "open": open_, "high": high, "low": low, "close": close, "volume": vol})


def _write_archive(d: str, frames) -> None:
    os.makedirs(d, exist_ok=True)
    for f in frames:
        sym = f["symbol"].iloc[0]
        for (y, m), g in f.groupby([f["date"].dt.year, f["date"].dt.month]):
            g.drop(columns=["symbol"]).to_csv(os.path.join(d, f"{sym}_{y:04d}_{m:02d}.csv"), index=False)


@pytest.fixture
def archive(tmp_path):
    d = str(tmp_path / "arch")
    _write_archive(d, [_bars("AAA", 420, 1), _bars("BBB", 420, 2), _bars("CCC", 420, 3), _bars("DEAD", 200, 4, drift=-0.002), _bars("THIN", 420, 5, vol=1.0)])
    return d


# ---------------------------------------------------------------- resolve_extras / active
def test_defaults_and_active():
    e = mw.resolve_extras({})
    assert e["source"] == "yfinance" and e["purge"] is True and not e["universe"]["enabled"] and not e["fo"]["enabled"]
    assert mw.active({}) is False and mw.active({"target_type": "Log_Return"}) is False
    assert mw.active({"ml_extras": {}}) is True and mw.active({"target_type": "TripleBarrier_Net"}) is True


def test_merge_is_one_level_deep():
    e = mw.resolve_extras({"ml_extras": {"source": "archive", "archive_dir": "x", "universe": {"enabled": True, "min_adv": 5}}})
    assert e["universe"] == {"enabled": True, "min_history": 60, "adv_window": 20, "min_adv": 5, "top_n": 0}


@pytest.mark.parametrize("block", [
    "nope", {"bogus": 1}, {"source": "csv"}, {"source": "archive"}, {"universe": {"enabled": True}}, {"universe": 3},
    {"source": "archive", "archive_dir": "x", "universe": {"enabled": True, "mean": 1}}, {"fo": {"enabled": True}}, {"long_threshold": "high"}, {"barrier_params": 3},
    {"archive_symbols": "AAA"},
])
def test_bad_extras_raise(block):
    with pytest.raises(ValueError):
        mw.resolve_extras({"ml_extras": block})


def test_feature_columns_follow_the_fo_dirs():
    assert mw.extra_feature_columns(mw.resolve_extras({})) == []
    both = mw.resolve_extras({"ml_extras": {"fo": {"enabled": True, "ban_dir": "a", "oi_dir": "b"}}})
    assert mw.extra_feature_columns(both) == list(fo.BAN_FEATURE_COLUMNS) + list(fo.OI_FEATURE_COLUMNS)
    ban_only = mw.resolve_extras({"ml_extras": {"fo": {"enabled": True, "ban_dir": "a"}}})
    assert mw.extra_feature_columns(ban_only) == list(fo.BAN_FEATURE_COLUMNS)


def test_threshold_and_no_double_costs():
    e = mw.resolve_extras({})
    assert mw.long_threshold({"target_type": "TripleBarrier_Class", "trading": {}}, e) == 0.5
    assert mw.long_threshold({"target_type": "TripleBarrier_Net", "trading": {"long_threshold": 0.002}}, e) == 0.002
    assert mw.long_threshold({"target_type": "TripleBarrier_Class", "trading": {}}, mw.resolve_extras({"ml_extras": {"long_threshold": 0.6}})) == 0.6
    tb = mw.trading_config({**CFG, "target_type": "TripleBarrier_Net"})
    assert tb["transaction_cost_bps"] == 0.0 and tb["slippage_bps"] == 0.0
    assert mw.trading_config(CFG)["transaction_cost_bps"] == 5.0          # an ordinary target keeps the simulator's costs


# ---------------------------------------------------------------- D23 archive + universe
def _features(g):
    from features import compute_feature_frame
    return compute_feature_frame(g)


def _build(archive, extras_block):
    cfg = {"ml_extras": {"source": "archive", "archive_dir": archive, **extras_block}}
    e = mw.resolve_extras(cfg)
    from features import FEATURE_COLUMNS
    return mw.build_dataset([], cfg, e, "2y", None, _features, FEATURE_COLUMNS) + (e,)


def test_archive_dataset_keeps_the_delisted_symbol_and_membership_is_point_in_time(archive):
    df, rep, _e = _build(archive, {"universe": {"enabled": True, "min_history": 60, "adv_window": 20, "min_adv": 1e7, "top_n": 0}})
    assert set(df["symbol"]) == {"AAA", "BBB", "CCC", "DEAD", "THIN"}
    assert rep["survivorship"]["ended_early_symbols"] == ["DEAD"]
    members = df[df["in_universe"]]
    assert "THIN" not in set(members["symbol"])                               # close x volume far below min_adv
    assert members.groupby("symbol")["n_prior"].min().min() >= 60             # no member row has fewer than min_history prior bars
    assert (df[df["symbol"] == "DEAD"]["in_universe"]).any()                  # the failed stock supplies in-universe rows


def test_archive_symbol_filter_and_missing_columns(archive, tmp_path):
    df, _rep, _e = _build(archive, {"archive_symbols": ["aaa"]})
    assert set(df["symbol"]) == {"AAA"}
    bad = str(tmp_path / "bad")
    os.makedirs(bad)
    pd.DataFrame({"date": ["2025-01-02"], "close": [1.0], "volume": [5]}).to_csv(os.path.join(bad, "X_2025_01.csv"), index=False)
    with pytest.raises(ValueError):
        mw.build_dataset([], {}, mw.resolve_extras({"ml_extras": {"source": "archive", "archive_dir": bad}}), "2y", None, _features, ["sma_10"])


def test_finalize_filters_after_the_labels():
    e = mw.resolve_extras({"ml_extras": {"source": "archive", "archive_dir": "x", "universe": {"enabled": True}}})
    df = pd.DataFrame({"symbol": ["A"] * 4, "date": pd.bdate_range("2025-01-01", periods=4), "in_universe": [False, True, True, False]})
    out, rep = mw.finalize(df, e)
    assert len(out) == 2 and rep == {"rows_before_universe": 4, "rows_after_universe": 2}
    off, rep2 = mw.finalize(df, mw.resolve_extras({}))
    assert len(off) == 4 and rep2["rows_after_universe"] == 4


# ---------------------------------------------------------------- D24 fo features
def test_parse_oi_new_and_old_layout_and_junk():
    new = "TckrSymb,FinInstrmTp,OpnIntrst,XpryDt\nINFY,STF,100,a\nINFY,STF,50,b\nINFY,STO,9,c\nNIFTY,IDF,5,d\nTCS,STF,7,e\n"
    got = fo.parse_fo_bhavcopy_oi(new).set_index("symbol")["open_interest"].to_dict()
    assert got == {"INFY": 150.0, "TCS": 7.0}
    old = b"INSTRUMENT,SYMBOL,OPEN_INT\nFUTSTK,SBIN,10\nOPTSTK,SBIN,3\nFUTIDX,NIFTY,9\n"
    assert fo.parse_fo_bhavcopy_oi(old).to_dict("records") == [{"symbol": "SBIN", "open_interest": 10.0}]
    assert len(fo.parse_fo_bhavcopy_oi("not,a,fo,file\n1,2,3,4\n")) == 0 and len(fo.parse_fo_bhavcopy_oi("")) == 0


def test_oi_history_round_trip(tmp_path):
    (tmp_path / "fo_oi_20250103.csv").write_text("symbol,open_interest\nAAA,100\nBBB,50\n")
    (tmp_path / "fo_oi_20250106.csv").write_text("symbol,open_interest\naaa,120\n")
    (tmp_path / "fo_oi_junk.csv").write_text("symbol,open_interest\nAAA,1\n")
    (tmp_path / "fo_oi_20250107.csv").write_text("x,y\n1,2\n")
    h = fo.load_oi_history(str(tmp_path))
    assert len(h) == 3 and set(h["symbol"]) == {"AAA", "BBB"} and h["date"].min() == pd.Timestamp("2025-01-03")
    assert len(fo.load_oi_history(str(tmp_path / "none"))) == 0


def test_fo_columns_are_nan_where_the_files_do_not_cover(archive, tmp_path):
    fod_dir = tmp_path / "fo"
    fod_dir.mkdir()
    days = pd.bdate_range("2025-06-02", periods=10)
    for i, d in enumerate(days):
        (fod_dir / f"fo_secban_{d:%Y%m%d}.csv").write_text("Securities in Ban For Trade Date\n1,AAA\n" if i >= 5 else "No Securities in Ban\n")
        (fod_dir / f"fo_oi_{d:%Y%m%d}.csv").write_text(f"symbol,open_interest\nAAA,{100 + i * 10}\nBBB,{500 - i * 10}\n")
    df, _rep, e = _build(archive, {"fo": {"enabled": True, "ban_dir": str(fod_dir), "oi_dir": str(fod_dir)}})
    for c in mw.extra_feature_columns(e):
        assert c in df.columns, c
    a = df[df["symbol"] == "AAA"].set_index("date")
    assert a.loc[days[5], "in_fo_ban"] == 1 and a.loc[days[2], "in_fo_ban"] == 0                 # covered day: a real 0 / 1
    assert np.isnan(a.loc[pd.Timestamp("2025-03-03"), "in_fo_ban"])                              # not covered: unknown, never 0
    assert np.isnan(a.loc[pd.Timestamp("2025-03-03"), "oi_buildup"])
    assert a.loc[days[3], "oi_chg_1d"] == pytest.approx(110 / 120 * 0 + (130 / 120 - 1)) and not np.isnan(a.loc[days[3], "oi_buildup"])
    c = df[df["symbol"] == "CCC"]
    assert c["oi_buildup"].isna().all()                                                           # no futures = NaN, not 0


# ---------------------------------------------------------------- D22 labels / purge / evaluation
def test_label_group_adds_end_date_and_ma_cross():
    from targets import TargetGenerator
    g = _bars("AAA", 120, 9).drop(columns=["symbol"])
    gen = TargetGenerator(target_type="TripleBarrier_Net", forecast_horizon=5)
    t, gf = gen.generate(g.copy(), inplace=False)
    out = mw.label_group(g.copy(), gf)
    ok = out["tb_end_idx"].notna()
    assert ok.sum() > 50 and out["tb_end_date"].notna().sum() == ok.sum()
    i = int(np.flatnonzero(ok.to_numpy())[10])
    assert out["tb_end_date"].iloc[i] == out["date"].iloc[int(out["tb_end_idx"].iloc[i])]
    assert out["tb_end_date"].iloc[i] > out["date"].iloc[i]
    assert out["ma_cross"].dtype == bool


def test_purge_drops_rows_whose_label_reaches_the_validation_block():
    d = pd.bdate_range("2025-01-01", periods=10)
    train = pd.DataFrame({"date": d[:5], "tb_end_date": [d[1], d[4], d[5], d[7], pd.NaT]})
    val = pd.DataFrame({"date": d[5:8]})
    kept, n = mw.purge_train(train, val)
    assert list(kept["tb_end_date"]) == [d[1], d[4]] and n == 3                                  # ends before d[5]; the NaT row is dropped too
    same, zero = mw.purge_train(train.drop(columns=["tb_end_date"]), val)
    assert len(same) == 5 and zero == 0


def test_evaluate_picks_reports_calibration_and_the_baseline_verdict():
    rng = np.random.default_rng(0)
    n = 400
    y = (rng.random(n) < 0.5).astype(float)
    pred = np.clip(0.5 + (y - 0.5) * 0.4 + rng.normal(0, 0.1, n), 0, 1)
    net = np.where(y == 1, 0.03, -0.02) + rng.normal(0, 0.002, n)
    oos = pd.DataFrame({"pred": pred, "tb_y": y, "tb_net_return": net, "ma_cross": rng.random(n) < 0.15})
    r = mw.evaluate_picks(oos, 0.5, boot=200)
    assert r["rows"] == n and r["brier"] < 0.25 and r["reliability"]["n"] == n
    assert r["vs_ma_cross"]["verdict"] == "beats_dumb" and r["vs_ma_cross"]["n_baseline"] >= 15
    assert mw.evaluate_picks(oos.iloc[:0], 0.5) == {"rows": 0}


# ---------------------------------------------------------------- fo_download
def _zip_of(csv_text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("BhavCopy.csv", csv_text)
    return buf.getvalue()


def test_bhavcopy_zip_to_oi_csv():
    z = _zip_of("TckrSymb,FinInstrmTp,OpnIntrst\nINFY,STF,100\nINFY,STF,20\nNIFTY,IDF,3\n")
    assert fod.bhavcopy_oi_csv(z).strip().splitlines() == ["symbol,open_interest", "INFY,120.0"]
    assert fod.bhavcopy_oi_csv(_zip_of("TckrSymb,FinInstrmTp,OpnIntrst\nNIFTY,IDF,3\n")) is None
    assert fod.bhavcopy_oi_csv(b"PKnot a zip") is None


def test_run_writes_skips_and_treats_a_404_as_a_holiday(tmp_path, monkeypatch):
    calls = []

    def fake_get(op, url, retries=2):
        calls.append(url)
        if "20261014" in url or "14102026" in url:
            return None                                                      # holiday
        if "secban" in url:
            return b"Securities in Ban For Trade Date\n1,RBLBANK\n"
        return _zip_of("TckrSymb,FinInstrmTp,OpnIntrst\nINFY,STF,5\n")

    monkeypatch.setattr(fod, "_opener", lambda: object())
    monkeypatch.setattr(fod, "_get", fake_get)
    monkeypatch.setattr(fod.time, "sleep", lambda s: None)
    out = str(tmp_path / "fo")
    from datetime import date
    r = fod.run(date(2026, 10, 12), date(2026, 10, 16), out, pause=0)       # Mon-Fri, Wed 14th is the "holiday"
    assert r == {"ban": 4, "oi": 4, "skipped": 0, "missing": 2}
    assert sorted(os.listdir(out))[0] == "fo_oi_20261012.csv" and not os.path.exists(os.path.join(out, "fo_secban_20261014.csv"))
    n = len(calls)
    r2 = fod.run(date(2026, 10, 12), date(2026, 10, 16), out, pause=0)      # re-run: only the missing day is asked for again
    assert r2["skipped"] == 8 and len(calls) == n + 2
    only = fod.run(date(2026, 10, 12), date(2026, 10, 12), str(tmp_path / "o2"), only="ban", pause=0)
    assert only["ban"] == 1 and only["oi"] == 0
    h = fo.load_ban_history(out)
    assert set(h["symbol"]) == {"RBLBANK"} and len(h) == 4


# ---------------------------------------------------------------- train.py smoke (stand-in xgboost / yfinance when the real ones are missing)
def _install_fakes(monkeypatch, tmp):
    """Make `import train` work without xgboost / yfinance / the DB layer; the stand-ins are scikit-learn gradient boosting."""
    from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
    added = []

    def put(name, mod):
        sys.modules[name] = mod
        added.append(name)

    try:
        importlib.import_module("xgboost")
    except ImportError:
        xgb = types.ModuleType("xgboost")
        xgb.callback = types.SimpleNamespace(TrainingCallback=object)

        class _R:
            def __init__(self, **kw):
                self.m = HistGradientBoostingRegressor(max_iter=15, max_depth=3, random_state=0)

            def fit(self, X, y, eval_set=None, verbose=False):
                self.m.fit(X, y)
                return self

            def predict(self, X):
                return self.m.predict(X)

        class _C(_R):
            def __init__(self, **kw):
                self.m = HistGradientBoostingClassifier(max_iter=15, max_depth=3, random_state=0)

            def predict_proba(self, X):
                return self.m.predict_proba(X)

        for cls, nm in ((_R, "XGBRegressor"), (_C, "XGBClassifier")):
            cls.__module__, cls.__qualname__, cls.__name__ = "xgboost", nm, nm       # joblib.dump(model) pickles by reference
        xgb.XGBRegressor, xgb.XGBClassifier = _R, _C
        put("xgboost", xgb)
    try:
        importlib.import_module("yfinance")
    except ImportError:
        put("yfinance", types.ModuleType("yfinance"))
    try:
        importlib.import_module("sqlalchemy")
    except ImportError:
        sc = types.ModuleType("scanner")
        sc.SIMILARITY_FEATURES = []
        put("scanner", sc)
    monkeypatch.setenv("TRAINING_DATA_DIR", tmp)
    open(os.path.join(tmp, "training.lock"), "w").close()
    sys.modules.pop("train", None)
    train = importlib.import_module("train")
    train.HAS_MODEL_REGISTRY = False
    train.HAS_DB = False
    return train, added


def _cfg(target, extras):
    return {
        "target_type": target, "forecast_horizon_days": 5,
        "validation_strategy": {"method": "WalkForward", "train_window_size": 600, "validation_window_size": 120, "step_size": 120, "embargo_days": 5},
        "preprocessing": {"scaler_type": "RobustScaler"},
        "model": {"max_depth": 3, "learning_rate": 0.1, "n_estimators": 10},
        "trading": {"long_threshold": 0.0, "short_threshold": 0.0, "transaction_cost_bps": 5.0, "slippage_bps": 2.0, "allow_short": False},
        "random_seed": 7, "data": {"symbols": [], "period": "2y"}, **({"ml_extras": extras} if extras is not None else {}),
    }


@pytest.mark.parametrize("target", ["TripleBarrier_Class", "TripleBarrier_Net"])
def test_train_pipeline_with_archive_universe_fo_and_purge(target, archive, tmp_path, monkeypatch):
    tmp = str(tmp_path / "store")
    os.makedirs(tmp)
    train, added = _install_fakes(monkeypatch, tmp)
    try:
        fo_dir = tmp_path / "fo"
        fo_dir.mkdir()
        for i, d in enumerate(pd.bdate_range("2025-03-03", periods=60)):
            (fo_dir / f"fo_oi_{d:%Y%m%d}.csv").write_text(f"symbol,open_interest\nAAA,{100 + i}\n")
            (fo_dir / f"fo_secban_{d:%Y%m%d}.csv").write_text("No Securities in Ban\n")
        extras = {"source": "archive", "archive_dir": archive, "universe": {"enabled": True, "min_history": 40, "adv_window": 10, "min_adv": 1e7, "top_n": 0},
                  "fo": {"enabled": True, "ban_dir": str(fo_dir), "oi_dir": str(fo_dir)}, "purge": True}
        rep = train.run_training_pipeline(_cfg(target, extras), db_session=None, model_store_path=tmp)
        w = rep["ml_wiring"]
        assert w["source"] == "archive" and w["purged_training_rows"] > 0 and "in_fo_ban" in w["feature_columns"] and "oi_buildup" in w["feature_columns"]
        assert w["dataset"]["rows_after_universe"] < w["dataset"]["rows_before_universe"] and w["dataset"]["survivorship"]["ended_early_symbols"] == ["DEAD"]
        assert w["evaluation"]["vs_ma_cross"]["verdict"] in ("insufficient_sample", "beats_dumb", "worse_than_dumb", "no_better")
        m = rep["walk_forward_metrics"]
        if target == "TripleBarrier_Class":
            assert "Brier" in m and "RMSE" not in m and w["evaluation"]["reliability"]["n"] > 0
        else:
            assert "RMSE" in m and "brier" not in w["evaluation"]
        assert os.path.exists(os.path.join(tmp, "model.pkl"))
    finally:
        sys.modules.pop("train", None)
        for n in added:
            sys.modules.pop(n, None)


def test_train_pipeline_without_extras_is_the_old_path(archive, tmp_path, monkeypatch):
    tmp = str(tmp_path / "store")
    os.makedirs(tmp)
    train, added = _install_fakes(monkeypatch, tmp)
    try:
        frames = []
        for sym, seed in (("AAA", 1), ("BBB", 2), ("CCC", 3)):
            g = _bars(sym, 420, seed)
            f = _features(g.drop(columns=["symbol"]).set_index("date"))
            f = f.dropna()
            f["symbol"], f["date"] = sym, f.index
            frames.append(f)
        monkeypatch.setattr(train, "build_multi_symbol_dataset", lambda symbols, period="2y": pd.concat(frames, ignore_index=True))
        cfg = _cfg("Log_Return", None)
        assert mw.active(cfg) is False
        rep = train.run_training_pipeline(cfg, db_session=None, model_store_path=tmp)
        assert "ml_wiring" not in rep and "RMSE" in rep["walk_forward_metrics"]
    finally:
        sys.modules.pop("train", None)
        for n in added:
            sys.modules.pop(n, None)


def test_bad_extras_fail_the_run_loudly(tmp_path, monkeypatch):
    tmp = str(tmp_path / "store")
    os.makedirs(tmp)
    train, added = _install_fakes(monkeypatch, tmp)
    try:
        with pytest.raises(ValueError):
            train.run_training_pipeline(_cfg("Log_Return", {"universe": {"enabled": True}}), db_session=None, model_store_path=tmp)
    finally:
        sys.modules.pop("train", None)
        for n in added:
            sys.modules.pop(n, None)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]) if hasattr(pytest, "main") else 0)
