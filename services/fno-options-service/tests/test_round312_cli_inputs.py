"""Round 312: the backtest command line reads dirty index CSVs and turns a refusal into a message."""
from __future__ import annotations

from datetime import datetime, timedelta

from backtest import cli


def _csv(path, rows, header="date,open,high,low,close,volume"):
    path.write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def test_a_dash_or_nan_in_the_csv_does_not_stop_the_run(tmp_path):
    f = tmp_path / "d.csv"
    _csv(f, ["2026-11-02,100,110,90,105,-", "2026-11-03,100,110,90,106,", "2026-11-04,100,110,90,nan,5", "2026-11-05,100,inf,90,107,7", "2026-11-06,100,110,90,108,12"])
    c = cli.load_candles(str(f))
    assert [x["c"] for x in c] == [105.0, 106.0, 108.0] and c[0]["v"] == 0.0 and c[2]["v"] == 12.0


def test_a_wrong_interval_is_a_message_not_a_traceback(tmp_path, capsys):
    d, i = tmp_path / "d.csv", tmp_path / "i.csv"
    _csv(d, [f"2026-10-{n:02d},100,110,90,105,0" for n in range(1, 29)])
    t0 = datetime(2026, 11, 2, 9, 15)
    _csv(i, [f"{(t0 + timedelta(minutes=15 * k)).strftime('%Y-%m-%d %H:%M')},100,101,99,100,0" for k in range(20)])      # 15-minute bars given to the 5-minute backtest
    rc = cli.main_sim(["--daily", str(d), "--intraday", str(i), "--out", str(tmp_path)])
    assert rc == 2 and "15-minute bars" in capsys.readouterr().err
