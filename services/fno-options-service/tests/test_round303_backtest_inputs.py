"""Round 303: the backtest inputs. The replay CLI hands EVERY Params field to the engine, a bad --set is a clear error, duplicate candles are dropped, and index candles of the wrong interval are refused."""
from __future__ import annotations

import csv
from dataclasses import fields

import pytest

from backtest import cli, replay, sim
from fno.params import CONFIG_NAME, Params
from test_backtests import build_world


def test_every_params_field_has_its_config_name_and_the_map_round_trips():
    assert set(CONFIG_NAME) == {f.name for f in fields(Params)}
    p = Params().with_(max_trades_per_day=1, strat_surge_range_mult=2.5, entry_end="13:00", block_expiry_day=False, capital=123456.0)
    cfg = replay.make_cfg({CONFIG_NAME[f]: getattr(p, f) for f in CONFIG_NAME})
    assert Params.from_config(cfg) == p                                  # nothing is lost between the CLI's Params and the engine's


def test_the_replay_cli_passes_every_set_field_to_the_engine(tmp_path, monkeypatch):
    seen = {}

    def fake_run(root, days, daily, over, und, hol, vix_default):
        seen["over"] = over
        return {"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {}, "notes": []}

    monkeypatch.setattr(cli.replay, "run", fake_run)
    rc = cli.main_replay(["--snapshots", str(tmp_path), "--out", str(tmp_path), "--set", "max_trades_per_day=1", "--set", "strat_surge_range_mult=2.5", "--set", "entry_end=13:00"])
    assert rc == 0
    got = Params.from_config(replay.make_cfg(seen["over"]))
    assert (got.max_trades_per_day, got.strat_surge_range_mult, got.entry_end) == (1, 2.5, "13:00")      # only min_score, sl / target / trail, risk, max_lots and vix_max used to arrive


def test_a_bad_set_is_a_clear_error_not_a_traceback(capsys):
    for bad in ("no_such_field=1", "max_lots=abc", "max_lots=2.5"):
        with pytest.raises(SystemExit) as e:
            cli._params(cli.argparse.Namespace(capital=500000.0, set=[bad]))
        assert e.value.code == 2
    assert "unknown Params field" in capsys.readouterr().err
    p = cli._params(cli.argparse.Namespace(capital=500000.0, set=["max_lots=3.0", "block_expiry_day=false"]))
    assert p.max_lots == 3 and p.block_expiry_day is False


def test_duplicate_timestamps_in_a_candle_file_are_dropped_last_row_wins(tmp_path, capsys):
    path = tmp_path / "c.csv"
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        w.writerow(["2026-11-02 09:15", 100, 101, 99, 100.5, 10])
        w.writerow(["2026-11-02 09:20", 100.5, 102, 100, 101, 10])
        w.writerow(["2026-11-02 09:15", 100, 103, 99, 102, 20])           # the same bar again, from a second export
    rows = cli.load_candles(str(path))
    assert len(rows) == 2 and rows[0]["c"] == 102 and rows[0]["v"] == 20
    assert "1 duplicate timestamp" in capsys.readouterr().err


def test_index_candles_of_another_interval_are_refused_by_the_backtests():
    daily, intr = build_world(n_days=60, n_intraday=5)
    sim.check_bar_spacing(intr, 5)                                        # 5-minute bars into a 5-minute run: fine
    with pytest.raises(ValueError, match="15-minute"):
        sim.check_bar_spacing(intr[::3], 5)
    with pytest.raises(ValueError, match="15-minute"):
        sim.run("NIFTY", daily, intr[::3])
    holes = [b for i, b in enumerate(intr) if i % 11 != 0]                # a few missing bars do not change the median gap
    sim.check_bar_spacing(holes, 5)


def test_the_replay_cli_warns_when_the_daily_candles_are_missing(tmp_path, monkeypatch, capsys):
    """Round 502: a replay without --daily used to run silently with an empty daily picture and print results that look like live ones."""
    monkeypatch.setattr(cli.replay, "run", lambda *a, **k: {"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {}, "notes": []})
    assert cli.main_replay(["--snapshots", str(tmp_path), "--out", str(tmp_path), "--underlyings", "NIFTY"]) == 0
    err = capsys.readouterr().err
    assert "no daily candles for NIFTY" in err and "fno_export_candles.py" in err
    f = tmp_path / "nifty_d.csv"
    f.write_text("date,open,high,low,close,volume\n2026-11-12,100,110,95,105,0\n", encoding="utf-8")
    assert cli.main_replay(["--snapshots", str(tmp_path), "--out", str(tmp_path), "--underlyings", "NIFTY", "--daily", str(f)]) == 0
    assert "no daily candles" not in capsys.readouterr().err


def test_the_replay_cli_says_why_when_there_are_no_snapshot_days(tmp_path, monkeypatch, capsys):
    """Round 503: no recorded days used to give a silent empty NOT_PROVEN report (the VM host has no /data/fno; the recordings are inside the container)."""
    monkeypatch.setattr(cli.replay, "run", lambda *a, **k: {"kind": "REPLAY", "metrics": {"overall": {}}, "verdict": {}, "notes": []})
    assert cli.main_replay(["--snapshots", str(tmp_path / "nope"), "--out", str(tmp_path), "--underlyings", "NIFTY"]) == 0
    err = capsys.readouterr().err
    assert "does not exist" in err and "fno_replay_vm.sh" in err
    (tmp_path / "snapshots" / "2026-11-12").mkdir(parents=True)
    cli.main_replay(["--snapshots", str(tmp_path), "--out", str(tmp_path), "--underlyings", "NIFTY"])
    assert "no recorded snapshot days" not in capsys.readouterr().err
