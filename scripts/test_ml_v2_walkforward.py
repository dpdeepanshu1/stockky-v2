"""group441: scripts/ml_v2_walkforward.py - AUC, ridge fit, walk-forward folds without leakage, verdicts, versioned model file. Plain asserts or pytest."""
from __future__ import annotations

import json
import random
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "shared"))

import ml_v2_walkforward as ml  # noqa: E402
from market_structure.ml_features import FEATURE_COLUMNS_V2, columns_hash  # noqa: E402

P = len(FEATURE_COLUMNS_V2)
DAY = 86400.0
T0 = 1_700_000_000.0 - (1_700_000_000.0 % DAY)


def test_auc_known_values_and_ties():
    assert ml.auc([0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1]) == 1.0
    assert ml.auc([0.9, 0.8, 0.2, 0.1], [0, 0, 1, 1]) == 0.0
    assert ml.auc([0.5, 0.5, 0.5, 0.5], [0, 1, 0, 1]) == 0.5
    assert ml.auc([1, 2, 3], [1, 1, 1]) is None
    assert abs(ml.auc([1, 2, 3, 4], [0, 1, 0, 1]) - 0.75) < 1e-12


def _synthetic(n, signal, seed=1, p_missing=0.0):
    rnd = random.Random(seed)
    rows = []
    for k in range(n):
        x = [None] * P
        a, b = rnd.gauss(0, 1), rnd.gauss(0, 1)
        x[FEATURE_COLUMNS_V2.index("ms_tqs")] = 50 + 10 * b
        x[FEATURE_COLUMNS_V2.index("ms_rr")] = a
        x[FEATURE_COLUMNS_V2.index("ms_room_atr")] = rnd.gauss(0, 1)
        if rnd.random() < p_missing:
            x[FEATURE_COLUMNS_V2.index("ms_rr")] = None
        y = 1 if (signal * a + rnd.gauss(0, 1)) > 0 else 0
        rows.append({"sym": f"S{k % 40}", "ts": T0 + k * 3600.0 * 6, "month": f"2026-{1 + (k * 6 // 24 // 30) % 12:02d}", "x": x,
                     "meta": {"engine_version": "1.0.0", "params_hash": "abc12345", "columns_hash": columns_hash(), "usable": True},
                     "ret": (1.0 if y else -1.0) + rnd.gauss(0, 0.3), "y": y})
    return rows


def test_fit_finds_a_planted_signal_and_ignores_empty_columns():
    rows = _synthetic(3000, 2.0)
    m = ml.fit([r["x"] for r in rows], [r["y"] for r in rows], ridge=10.0)
    assert m is not None and len(m["cols"]) == 3                       # only the three populated columns survive
    sc = [ml.score(m, r["x"]) for r in rows]
    assert ml.auc(sc, [r["y"] for r in rows]) > 0.8
    assert ml.fit([r["x"] for r in rows[:20]], [r["y"] for r in rows[:20]]) is None
    assert ml.fit([[None] * P for _ in range(100)], [0, 1] * 50) is None


def test_missing_values_use_the_train_mean_and_outliers_are_clipped():
    rows = _synthetic(500, 1.0)
    m = ml.fit([r["x"] for r in rows], [r["y"] for r in rows])
    base = [None] * P
    assert abs(ml.score(m, base) - m["b"]) < 1e-9                       # all missing = the intercept
    huge = list(rows[0]["x"])
    huge[FEATURE_COLUMNS_V2.index("ms_rr")] = 1e9
    assert abs(ml.score(m, huge)) < 100                                 # clipped at 5 sd


def test_folds_are_time_ordered_embargoed_and_never_leak():
    rows = _synthetic(1000, 1.0)
    folds = ml.make_folds(rows, 4, embargo_days=10)
    assert len(folds) == 4
    prev_end = -1
    for tr, te in folds:
        t0 = min(rows[k]["ts"] for k in te)
        assert max(rows[k]["ts"] for k in tr) < t0 - 10 * DAY           # embargo respected
        assert set(tr).isdisjoint(te)
        assert max(rows[k]["ts"] for k in tr) <= t0
        assert min(rows[k]["ts"] for k in te) >= prev_end                # test blocks move forward and do not overlap
        prev_end = max(rows[k]["ts"] for k in te)
    assert ml.make_folds(rows[:3], 4, 10) == []


def test_future_data_cannot_change_an_earlier_fold():
    rows = _synthetic(1200, 1.5, seed=3)
    f1 = ml.evaluate(rows, 4, 5, 30, 10.0, 20000, 7)["folds"][0]
    changed = [dict(r) for r in rows]
    for r in changed[900:]:
        r["y"] = 1 - r["y"]                                              # flip the labels of the last quarter only
        r["ret"] = -r["ret"]
    f1b = ml.evaluate(changed, 4, 5, 30, 10.0, 20000, 7)["folds"][0]
    assert f1 == f1b


def test_evaluate_with_signal_beats_chance_and_without_does_not():
    good = ml.evaluate(_synthetic(4000, 2.0, seed=5), 4, 5, 30, 10.0, 20000, 7)
    assert good["pooled_auc_model"] > 0.7 and all(f["edge_pp"] > 0 for f in good["folds"])
    bad = ml.evaluate(_synthetic(4000, 0.0, seed=6), 4, 5, 30, 10.0, 20000, 7)
    assert abs(bad["pooled_auc_model"] - 0.5) < 0.05


def test_verdicts():
    F = lambda e: {"fold": 1, "auc_model": 0.6, "edge_pp": e}  # noqa: E731
    v = ml.verdict
    assert v(0, [], None, None, None, None, None, 100) == "no_data"
    assert v(50, [F(1)] * 4, 1.0, [0.5, 1.5], 0.6, 0.55, 1.0, 500) == "insufficient_sample"
    assert v(900, [F(1)] * 2, 1.0, [0.5, 1.5], 0.6, 0.55, 1.0, 500) == "insufficient_sample"
    assert v(900, [F(1)] * 4, 1.0, [0.5, 1.5], 0.6, 0.55, 1.0, 500) == "pass"
    assert v(900, [F(1)] * 4, 1.0, [-0.5, 1.5], 0.6, 0.55, 1.0, 500) == "no_edge"
    assert v(900, [F(1)] * 4, 1.0, [0.5, 1.5], 0.6, 0.65, 1.0, 500) == "no_edge"        # not better than ms_tqs alone
    assert v(900, [F(1), F(-1), F(-1), F(1)], 1.0, [0.5, 1.5], 0.6, 0.55, 1.0, 500) == "no_edge"
    assert v(900, [F(-1)] * 4, -1.0, [-1.5, -0.5], 0.45, 0.5, -1.0, 500) == "hurts"


def test_report_is_versioned_and_refuses_mixed_rows():
    rows = _synthetic(3000, 2.0, seed=8)
    a = {"folds": 4, "embargo_days": 5, "top_pct": 30, "ridge": 10.0, "max_train": 20000, "seed": 7, "horizon": 10, "boot": 200, "min_n": 500}
    rep, model = ml.build_report(rows, a, {"top_pct": 30})
    assert rep["meta"]["comparable"] is True and rep["verdict"] in ("pass", "no_edge") and rep["pooled_auc_model"] > 0.7
    assert model["version_id"].startswith(f"v2-{columns_hash()}-1.0.0-abc12345-") and model["status"].startswith("evidence_only")
    assert model["columns"] == ["ms_rr", "ms_room_atr", "ms_tqs"] or set(model["columns"]) == {"ms_rr", "ms_room_atr", "ms_tqs"}
    assert "Verdict" in ml.to_markdown(rep)
    mixed = [dict(r) for r in rows]
    mixed[0] = {**mixed[0], "meta": {**mixed[0]["meta"], "params_hash": "zzzz9999"}}
    rep2, model2 = ml.build_report(mixed, a, {"top_pct": 30})
    assert rep2["meta"]["comparable"] is False and rep2["verdict"] == "insufficient_sample" and model2 is None
    rep3, model3 = ml.build_report([], a, {"top_pct": 30})
    assert rep3["verdict"] == "no_data" and model3 is None


def _walk_file(path: Path, seed: int, n: int = 520):
    rnd = random.Random(seed)
    px, out = 100.0, []
    for k in range(n):
        o = px
        c = max(5.0, o * (1 + rnd.gauss(0.0004, 0.014)))
        out.append({"date": T0 + k * DAY, "open": o, "high": max(o, c) * 1.006, "low": min(o, c) * 0.994, "close": c, "volume": 100000.0})
        px = c
    path.write_text(json.dumps({"candles": out}))


def test_cli_on_synthetic_histories_writes_report_and_model():
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for k in range(4):
            _walk_file(d / f"SYM{k}.json", seed=k + 1)
        rc = ml.main([td, "--step", "4", "--min-n", "50", "--boot", "50", "--folds", "3", "--all-days"])
        assert rc == 0
        rep = json.loads((d / "ml_v2_out" / "ml_v2_report.json").read_text())
        assert rep["verdict"] in ("insufficient_sample", "pass", "no_edge", "hurts") and rep["rows"] > 100
        assert (d / "ml_v2_out" / "ml_v2_report.md").exists()
        mdl = json.loads((d / "ml_v2_out" / "ml_v2_model.json").read_text())
        assert mdl["columns_hash"] == columns_hash() and mdl["status"].startswith("evidence_only")
        ml.main([td, "--step", "4", "--folds", "3", "--all-days", "--no-save", "--out", str(d / "o2")])
        assert not (d / "o2" / "ml_v2_model.json").exists()


def test_empty_folder_is_no_data_and_exit_1():
    with tempfile.TemporaryDirectory() as td:
        assert ml.main([td, "--boot", "50"]) == 1
        assert json.loads((Path(td) / "ml_v2_out" / "ml_v2_report.json").read_text())["verdict"] == "no_data"


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
