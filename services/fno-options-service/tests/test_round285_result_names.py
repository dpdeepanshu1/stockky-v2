"""Round 285: two saved backtest results never overwrite each other, a result started from the service is named after its index and strategy, and the results list is newest first by write time. No network."""
from __future__ import annotations

import json
import os
import time
from types import SimpleNamespace

import pytest

import config
from backtest import report


def test_two_results_with_the_same_name_in_the_same_second_are_both_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "strftime", lambda fmt: "20261110_101500")
    a = report.save(str(tmp_path), "fno_sim_nifty", {"kind": "SIM", "n": 1})
    b = report.save(str(tmp_path), "fno_sim_nifty", {"kind": "SIM", "n": 2})
    c = report.save(str(tmp_path), "fno_sim_nifty", {"kind": "SIM", "n": 3})
    assert len({a, b, c}) == 3 and os.path.basename(a) == "fno_sim_nifty_20261110_101500.json" and b.endswith("_2.json") and c.endswith("_3.json")
    assert [json.load(open(x))["n"] for x in (a, b, c)] == [1, 2, 3]
    assert b[:-5] + ".txt" != a[:-5] + ".txt"                                  # the .txt companions stay distinct too


def test_job_names_follow_the_command_line_style():
    assert report.job_name("sim", ["NIFTY"], "CORE") == "fno_sim_nifty"
    assert report.job_name("sim", ["NIFTY"], "ALL") == "fno_sim_nifty_portfolio"
    assert report.job_name("sim", ["SENSEX"], "BEAR_SWEEP") == "fno_sim_sensex_bear_sweep"
    assert report.job_name("replay", ["NIFTY", "SENSEX"], "CORE") == "fno_replay_nifty-sensex"
    assert report.job_name("sim", [], "CORE") == "fno_sim_nifty"


def test_the_saved_name_still_starts_with_the_prefixes_the_readiness_list_and_page_look_for(tmp_path):
    p = os.path.basename(report.save(str(tmp_path), report.job_name("sim", ["NIFTY"], "ALL"), {}))
    assert p.startswith("fno_sim") and p.endswith(".json")


@pytest.fixture()
def main_mod(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    monkeypatch.setattr(main, "provider", SimpleNamespace(daily=lambda u: [], intraday=lambda u: []))
    fake = {"kind": "SIM", "underlying": "NIFTY", "metrics": {"overall": {"trades": 0}}, "verdict": {"status": "NOT_PROVEN"}, "notes": []}
    monkeypatch.setattr(main.bt_sim, "run", lambda *a, **k: dict(fake, strategy=k.get("strategy")))
    monkeypatch.setattr(main.bt_portfolio, "run", lambda *a, **k: dict(fake, strategy="PORTFOLIO"))
    return main


def test_two_service_jobs_in_one_second_are_both_listed(main_mod, tmp_path, monkeypatch):
    main = main_mod
    monkeypatch.setattr(time, "strftime", lambda fmt: "20261110_101500")
    for strat in ("ALL", "ALL", "CORE"):
        main._jobs["j"] = {"id": "j", "kind": "sim", "state": "running"}
        main._run_job("j", main.BacktestBody(kind="sim", underlyings=["NIFTY"], strategy=strat))
        assert main._jobs["j"]["state"] == "done"
    names = sorted(f for f in os.listdir(tmp_path / "backtests") if f.endswith(".json"))
    assert names == ["fno_sim_nifty_20261110_101500.json", "fno_sim_nifty_portfolio_20261110_101500.json", "fno_sim_nifty_portfolio_20261110_101500_2.json"]
    assert len(os.listdir(tmp_path / "backtests")) == 6                        # every .json has its .txt


def test_the_results_list_is_newest_first_by_write_time_not_by_name(main_mod, tmp_path):
    main = main_mod
    d = tmp_path / "backtests"
    d.mkdir()
    for i, name in enumerate(["fno_sim_sensex_20261110_090000.json", "fno_sim_nifty_20261110_100000.json", "fno_replay_nifty_20261109_090000.json"]):
        f = d / name
        f.write_text(json.dumps({"kind": "SIM", "metrics": {"overall": {}}, "verdict": {}}))
        os.utime(f, (1_000_000 + i * 100, 1_000_000 + i * 100))
    got = [r["name"] for r in main.backtests(admin=None)["results"]]
    assert got == ["fno_replay_nifty_20261109_090000.json", "fno_sim_nifty_20261110_100000.json", "fno_sim_sensex_20261110_090000.json"]
