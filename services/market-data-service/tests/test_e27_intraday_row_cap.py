"""E27: /history keeps up to MAX_HISTORY_ROWS_INTRADAY rows for 1m ... 1h (a 1-minute chart was cut to 260 minutes); daily keeps its 260.
The cap helper is exec'd from main.py's source, so no web framework is needed."""
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()


def _load(env):
    a = SRC.index("MAX_HISTORY_ROWS = int(")
    b = SRC.index("def _report_rate_limit")

    class _OS:
        environ = dict(env)

    ns = {"os": _OS}
    exec(compile(SRC[a:b], "cap", "exec"), ns)
    return ns


def test_defaults_daily_260_intraday_2600():
    ns = _load({})
    assert ns["MAX_HISTORY_ROWS"] == 260 and ns["MAX_HISTORY_ROWS_INTRADAY"] == 2600
    for iv in ("1d", "1D", "1wk", "1mo", None, ""):
        assert ns["_history_row_cap"](iv) == 260, iv
    for iv in ("1m", "5m", "15m", "30m", "1h", "60m"):
        assert ns["_history_row_cap"](iv) == 2600, iv


def test_one_minute_session_data_fits():
    assert _load({})["_history_row_cap"]("1m") >= 5 * 375


def test_blank_or_bad_env_falls_back_and_the_cap_never_drops_below_the_daily_one():
    assert _load({"MAX_HISTORY_ROWS_INTRADAY": "   "})["MAX_HISTORY_ROWS_INTRADAY"] == 2600
    assert _load({"MAX_HISTORY_ROWS_INTRADAY": "abc"})["MAX_HISTORY_ROWS_INTRADAY"] == 2600
    assert _load({"MAX_HISTORY_ROWS_INTRADAY": "4000"})["MAX_HISTORY_ROWS_INTRADAY"] == 4000
    assert _load({"MAX_HISTORY_ROWS_INTRADAY": "100"})["MAX_HISTORY_ROWS_INTRADAY"] == 260
    assert _load({"MAX_HISTORY_ROWS": "400"})["_history_row_cap"]("1d") == 400


def test_every_truncation_in_history_uses_the_interval_aware_cap():
    assert "[-MAX_HISTORY_ROWS:]" not in SRC and "> MAX_HISTORY_ROWS:" not in SRC
    assert SRC.count("_history_row_cap(") >= 10
