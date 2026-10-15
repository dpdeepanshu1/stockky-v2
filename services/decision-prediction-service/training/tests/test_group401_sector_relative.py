"""group401: sector-relative returns on the structure observations (training/sector_index.py + structure_obs.py + the decision hook).
No sqlalchemy / fastapi needed. Plain asserts; runs with pytest or `python3 tests/test_group401_sector_relative.py`."""
from __future__ import annotations

import ast
import os
import sys
from datetime import date

_T = os.path.dirname(os.path.abspath(__file__))
HERE = os.path.dirname(_T)
sys.path.insert(0, HERE)
sys.path.insert(0, _T)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "decision"))
import sector_index as sx  # noqa: E402
import structure_obs as so  # noqa: E402
import structure_obs_hook as hook  # noqa: E402
from test_group374_structure_obs import M, OBS, _DB, _bars  # noqa: E402


def test_index_for_maps_known_labels_and_leaves_the_rest_unknown():
    assert sx.index_for("IT") == "^CNXIT" and sx.index_for("Technology") == "^CNXIT"
    assert sx.index_for("Banks") == "^NSEBANK" and sx.index_for("PSU Bank") == "^CNXPSUBANK"
    assert sx.index_for("Financial Services") == "^CNXFIN" and sx.index_for("Healthcare") == "^CNXPHARMA" and sx.index_for("FMCG") == "^CNXFMCG"
    for loose in ("Consumer Cyclical", "Basic Materials", "Industrials", "Telecom", "", None, 5, [], "   "):
        assert sx.index_for(loose) is None          # a loose proxy would mislead, so unmapped stays unknown


def test_index_for_env_override_and_bad_override(monkeypatch=None):
    os.environ["STRUCTURE_OBS_SECTOR_INDEX_JSON"] = '{"Telecom": "^CNXMEDIA", "it": "^MYIT"}'
    try:
        assert sx.index_for("telecom") == "^CNXMEDIA" and sx.index_for("IT") == "^MYIT"
        os.environ["STRUCTURE_OBS_SECTOR_INDEX_JSON"] = "not json"
        assert sx.index_for("IT") == "^CNXIT"
        os.environ["STRUCTURE_OBS_SECTOR_INDEX_JSON"] = "[1]"
        assert sx.index_for("IT") == "^CNXIT"
    finally:
        os.environ.pop("STRUCTURE_OBS_SECTOR_INDEX_JSON", None)


def test_clean_label():
    assert sx.clean_label("  Financial   Services ") == "Financial Services" and sx.clean_label("x" * 80) == "x" * 50
    assert sx.clean_label("") is None and sx.clean_label(None) is None and sx.clean_label(3) is None


def test_sanitize_keeps_sector_and_stays_valid_without_it():
    assert so.sanitize({**OBS, "sector": " IT "})["sector"] == "IT"
    assert so.sanitize(OBS)["sector"] is None and so.sanitize({**OBS, "sector": 7})["sector"] is None


def test_evaluate_adds_sector_returns_like_the_nifty_ones():
    sec = _bars(o=200.0, step=2.0)
    r = so.evaluate(OBS, _bars(), bench=_bars(o=50.0, step=0.5), slip_pct=0.0, sector_bench=sec)
    assert r["sector_ret_5"] == round((sec[5]["close"] / sec[1]["open"] - 1) * 100, 4)
    assert r["sector_ret_1"] is not None and r["sector_ret_20"] is not None and "nifty_ret_20" in r


def test_evaluate_without_usable_sector_bars_leaves_the_fields_out():
    for sb in (None, [], "x", [{"date": "2020-01-01", "open": 1, "high": 1, "low": 1, "close": 1}]):
        r = so.evaluate(OBS, _bars(), slip_pct=0.0, sector_bench=sb)
        assert not any(k.startswith("sector_ret_") for k in r) and r["eval_status"] == "done"


def test_report_group_has_relative_to_sector_and_counts_only_rows_that_have_it():
    rows = [{"eval_status": "done", "ret_5": 1.0, "ret_20": 6.0, "sector_ret_20": 2.0, "nifty_ret_20": 1.0, "final_decision": "BUY NOW"},
            {"eval_status": "done", "ret_5": 1.0, "ret_20": 3.0, "sector_ret_20": 1.0, "nifty_ret_20": 1.0, "final_decision": "BUY NOW"},
            {"eval_status": "done", "ret_5": 1.0, "ret_20": 9.0, "nifty_ret_20": 1.0, "final_decision": "BUY NOW"}]   # no sector: not counted, not zero
    g = so.summarize(rows, min_n=1)["passed_buy_now"]
    assert g["avg_rel_to_sector_20"] == 3.0 and g["n_with_sector_20"] == 2 and g["avg_rel_to_nifty_20"] is not None
    assert so.summarize([{"eval_status": "done", "ret_5": 1.0, "ret_20": 3.0, "final_decision": "BUY NOW"}], 1)["passed_buy_now"]["avg_rel_to_sector_20"] is None


def test_upsert_stores_sector_and_a_later_blank_does_not_erase_it():
    db = _DB()
    so.upsert_batch(db, M, [{**OBS, "sector": "IT"}])
    assert db.rows[0].sector == "IT"
    so.upsert_batch(db, M, [{**OBS, "final_decision": "WAIT"}])            # same day, pending row, newer decision without a sector
    assert db.rows[0].final_decision == "WAIT" and db.rows[0].sector == "IT"
    so.upsert_batch(db, M, [{**OBS, "sector": "Banks"}])
    assert db.rows[0].sector == "Banks"


def test_evaluate_pending_fetches_each_sector_index_once_and_isolates_failures():
    db = _DB()
    so.upsert_batch(db, M, [{**OBS, "symbol": "A", "sector": "IT"}, {**OBS, "symbol": "B", "sector": "Technology"}, {**OBS, "symbol": "C", "sector": "Telecom"},
                            {**OBS, "symbol": "D", "sector": "Banks"}, {**OBS, "symbol": "E"}])
    asked = []

    def sec(idx):
        asked.append(idx)
        if idx == "^NSEBANK":
            raise RuntimeError("yahoo down")
        return _bars(o=300.0, step=3.0)

    res = so.evaluate_pending(db, M, lambda s: _bars(), None, max_batch=10, today=date(2026, 10, 20), fetch_sector_bench=sec)
    by = {r.symbol: r for r in db.rows}
    assert asked.count("^CNXIT") == 1 and asked.count("^NSEBANK") == 1 and len(asked) == 2        # IT shared; Telecom / no sector never asked
    assert res["errors"] == 0 and res["done"] == 5
    assert by["A"].sector_ret_5 is not None and by["B"].sector_ret_5 == by["A"].sector_ret_5
    assert getattr(by["C"], "sector_ret_5", None) is None and getattr(by["D"], "sector_ret_5", None) is None and getattr(by["E"], "sector_ret_5", None) is None
    assert by["D"].ret_20 is not None                                                               # the stock's own outcome is still scored


def test_evaluate_pending_without_a_sector_fetcher_is_unchanged():
    db = _DB()
    so.upsert_batch(db, M, [{**OBS, "sector": "IT"}])
    res = so.evaluate_pending(db, M, lambda s: _bars(), None, max_batch=5, today=date(2026, 10, 20))
    assert res["done"] == 1 and getattr(db.rows[0], "sector_ret_5", None) is None


def test_decision_hook_passes_the_sector_through():
    resp = {"decision": "WAIT", "close": 101.5, "sector": "Technology",
            "structure": {"features": {"quality": "ok", "trend": "bullish", "atr": 2.0, "asof": "2026-10-14", "ver": "1.0.0"}}}
    assert hook.build_payload("ABC", resp)["sector"] == "Technology"
    assert hook.build_payload("ABC", {**resp, "sector": None})["sector"] is None
    assert hook.build_payload("ABC", {**resp, "sector": 5})["sector"] is None


def test_source_wiring_columns_migration_and_endpoint():
    models = open(os.path.join(HERE, "models.py"), encoding="utf-8").read()
    for c in ("sector = Column(String(50)", "sector_ret_1", "sector_ret_5", "sector_ret_10", "sector_ret_20", "_add_missing_structure_columns(engine)"):
        assert c in models
    for n in ("sector", "sector_ret_1", "sector_ret_5", "sector_ret_10", "sector_ret_20"):
        assert f'("{n}"' in models                                       # the migration list covers every new column
    app = open(os.path.join(HERE, "app.py"), encoding="utf-8").read()
    assert "_sector_bench" in app and "_bench, max_batch, None, _sector_bench" in app
    for src in (models, app):
        ast.parse(src)
    for col in so._OUTCOME_COLS[-4:]:
        assert col.startswith("sector_ret_")


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed 0 failed")
