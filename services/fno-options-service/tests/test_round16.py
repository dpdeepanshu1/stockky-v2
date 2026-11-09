"""Round 16 audit of the rolling (expired options) backtest data path: request validation, parser, merge, and the fetch script's failure handling.
Each test failed on the round-15 code. No network: the HTTP call is a fake."""
from __future__ import annotations

import importlib.util
import json
import os
import urllib.error
from datetime import date as D
from pathlib import Path

import pytest

from backtest import rolling

ROOT = Path(__file__).resolve().parents[3]


def _fetch_mod():
    spec = importlib.util.spec_from_file_location("fno_rolling_fetch", ROOT / "scripts" / "fno_rolling_fetch.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


F = _fetch_mod()


def _body(ts, closes):
    return {"data": {"ce": {"timestamp": ts, "close": closes, "open": closes, "high": closes, "low": closes, "volume": [1] * len(ts), "oi": [1] * len(ts), "iv": [14.0] * len(ts), "spot": [24500.0] * len(ts), "strike": [24500.0] * len(ts)}, "pe": None}}


def _http_error(code):
    import io
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b'{"errorCode":"DH-904"}'))


# ---- request builder: the strike label
def test_strike_label_is_validated():
    for ok in ("ATM", "ATM+1", "ATM-1", "ATM+10", "ATM-10"):
        assert rolling.build_request("NIFTY", "CALL", ok, D(2026, 10, 1), D(2026, 10, 5))["strike"] == ok
    for bad in ("atm", "ATM+0", "ATM+11", "ATM++1", "ATM+", "OTM+1", "24500", "", "ATM +1", None, 3):
        with pytest.raises(ValueError):
            rolling.build_request("NIFTY", "CALL", bad, D(2026, 10, 1), D(2026, 10, 5))


# ---- parser
def test_parser_survives_one_unreadable_timestamp_and_drops_zero_closes():
    rows = rolling.parse_response(_body([1756698000, "junk", None, 1756698300, 1756698600], [100.0, 101.0, 102.0, 0.0, 103.0]), "CALL")
    assert [r["t"] for r in rows] == [1756698000.0, 1756698600.0]          # junk / None timestamps skipped, the zero close is "no trade", the rest of the window is kept
    assert rolling.parse_response(_body([1756698000], [float("nan")]), "CALL") == []
    assert rolling.parse_response({"data": {"ce": {"timestamp": 12345, "close": [1]}}}, "CALL") == []
    assert rolling.parse_response({"data": {"ce": {"timestamp": "abc", "close": [1]}}}, "CALL") == []


def test_merge_keeps_each_timestamp_once_and_in_order():
    a = rolling.parse_response(_body([100, 200, 300], [1.0, 2.0, 3.0]), "CALL")
    b = rolling.parse_response(_body([300, 400], [3.5, 4.0]), "CALL")      # 300 is the shared window edge
    m = rolling.merge_rows([a, b])
    assert [r["t"] for r in m] == [100.0, 200.0, 300.0, 400.0]
    assert [r["c"] for r in m] == [1.0, 2.0, 3.5, 4.0]                      # the later window's row wins


# ---- fetch script: retries
def test_transient_errors_are_retried_and_a_bad_token_is_not():
    calls = {"n": 0}
    waits = []

    def flaky(body):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _http_error(429)
        return _body([100], [1.0])

    rows, why = F.fetch_window(flaky, {}, "CALL", retries=3, pause=0.5, sleep=waits.append)
    assert why is None and len(rows) == 1 and calls["n"] == 3 and waits == [0.5, 1.0]      # the pause doubles

    calls["n"] = 0

    def bad_token(body):
        calls["n"] += 1
        raise _http_error(401)

    rows, why = F.fetch_window(bad_token, {}, "CALL", retries=3, sleep=lambda s: None)
    assert rows == [] and why.startswith("HTTP 401") and calls["n"] == 1                   # retrying a 401 cannot help

    def down(body):
        raise urllib.error.URLError("no route")

    rows, why = F.fetch_window(down, {}, "CALL", retries=2, sleep=lambda s: None)
    assert rows == [] and "URLError" in why and "3 tries" in why


# ---- fetch script: a hole is recorded, not hidden
def test_series_records_failed_and_empty_windows():
    seq = iter([_body([100, 200], [1.0, 2.0]), _http_error(500), _http_error(500), _http_error(500), _http_error(500), {"data": {"ce": None, "pe": None}}, _body([300], [3.0])])   # 4 windows: ok, failed (1 + 3 retries), empty, ok

    def do_call(body):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r

    s = F.fetch_series(do_call, "NIFTY", "CALL", "ATM", D(2026, 1, 1), D(2026, 4, 15), 5, "WEEK", 1, pause=0, retries=3, sleep=lambda x: None, say=lambda m: None)
    assert [r["t"] for r in s["rows"]] == [100.0, 200.0, 300.0]
    assert len(s["failed_windows"]) == 1 and s["failed_windows"][0][0] == "2026-01-31" and "HTTP 500" in s["failed_windows"][0][2]
    assert s["empty_windows"] == [["2026-03-02", "2026-04-01"]]


# ---- fetch script: files
def test_a_failed_run_never_destroys_a_good_file(tmp_path):
    good = tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json"
    meta = {"underlying": "NIFTY"}
    full = {"rows": [{"t": 1.0, "c": 5.0}], "failed_windows": [], "empty_windows": []}
    path, what = F.write_series(good, meta, full)
    assert path == good and what == "complete" and json.loads(good.read_text())["complete"] is True
    before = good.read_text()

    # every call failed (expired token): no candles at all -> nothing is written, the good file stays
    path, what = F.write_series(good, meta, {"rows": [], "failed_windows": [["a", "b", "HTTP 401"]], "empty_windows": []})
    assert path is None and good.read_text() == before

    # some windows failed: the series goes to .partial and does NOT replace the complete file
    part = {"rows": [{"t": 2.0, "c": 6.0}], "failed_windows": [["a", "b", "HTTP 500"]], "empty_windows": []}
    path, what = F.write_series(good, meta, part)
    assert path.name.endswith(".json.partial") and "PARTIAL" in what and good.read_text() == before
    assert json.loads(path.read_text())["complete"] is False and json.loads(path.read_text())["failed_windows"]

    # a later complete run replaces the good file and removes the stale partial; no temp file is left behind
    path, what = F.write_series(good, meta, {"rows": [{"t": 3.0, "c": 7.0}], "failed_windows": [], "empty_windows": []})
    assert path == good and json.loads(good.read_text())["rows"][0]["t"] == 3.0
    assert sorted(os.listdir(tmp_path)) == [good.name]


def test_fetch_script_refuses_bad_arguments(monkeypatch, capsys):
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "t")
    for argv in (["--from", "2026-05-10", "--to", "2026-05-01"], ["--from", "2026-05-01", "--to", "2026-05-10", "--strikes", "ATM+99"], ["--from", "nope", "--to", "2026-05-10"]):
        monkeypatch.setattr("sys.argv", ["fno_rolling_fetch.py", *argv])
        assert F.main() == 2
