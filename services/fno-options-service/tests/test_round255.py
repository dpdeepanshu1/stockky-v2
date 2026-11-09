"""Round 255: the rolling (expired options) fetch asks Dhan the way Dhan really answers (live probe 9 Nov 2026): toDate - fromDate at most 1 day, toDate inclusive.
Each test failed on the round 254 code. No network: the Dhan reply is a fake that refuses wide windows with DH-905 like the real one."""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from datetime import date as D, datetime, timedelta
from pathlib import Path

import pytest

from backtest import rolling

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("fno_rolling_fetch", ROOT / "scripts" / "fno_rolling_fetch.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)


def _span(ws):
    return [(b - a).days for a, b in ws]


def _weekdays(start, end):
    d, out = start, []
    while d < end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _covered(ws):
    out = set()
    for a, b in ws:
        d = a
        while d <= b:
            out.add(d)
            d += timedelta(days=1)
    return out


@pytest.mark.parametrize("start,end", [
    (D(2026, 11, 2), D(2026, 11, 7)), (D(2026, 11, 2), D(2026, 11, 6)), (D(2026, 11, 2), D(2026, 11, 3)), (D(2026, 11, 6), D(2026, 11, 10)),
    (D(2026, 5, 1), D(2026, 11, 7)), (D(2026, 11, 7), D(2026, 11, 9)), (D(2026, 9, 21), D(2026, 9, 23)),
])
def test_windows_cover_every_weekday_within_the_one_day_limit(start, end):
    ws = rolling.windows(start, end)
    assert all(d == 1 for d in _span(ws))                                        # never from == to, never wider than Dhan takes
    assert set(_weekdays(start, end)) <= _covered(ws)                            # every trading weekday is asked for
    assert all(b < end for _, b in ws)                                           # never a date at or after `end`


def test_windows_known_answers_and_weekend_skip():
    assert rolling.windows(D(2026, 11, 2), D(2026, 11, 7)) == [(D(2026, 11, 2), D(2026, 11, 3)), (D(2026, 11, 4), D(2026, 11, 5)), (D(2026, 11, 5), D(2026, 11, 6))]
    assert rolling.windows(D(2026, 11, 2), D(2026, 11, 6)) == [(D(2026, 11, 2), D(2026, 11, 3)), (D(2026, 11, 4), D(2026, 11, 5))]
    assert rolling.windows(D(2026, 11, 7), D(2026, 11, 9)) == []                 # Saturday + Sunday only
    assert rolling.windows(D(2026, 11, 7), D(2026, 11, 8)) == []
    assert len(rolling.windows(D(2026, 5, 1), D(2026, 11, 7))) < 90              # six months is about 80 calls per series


def test_a_wider_span_gives_fewer_calls():
    assert len(rolling.windows(D(2026, 5, 1), D(2026, 11, 7), 30)) <= 8
    assert all(0 < d <= 30 for d in _span(rolling.windows(D(2026, 5, 1), D(2026, 11, 7), 30)))


def test_build_request_enforces_the_measured_limit():
    ok = rolling.build_request("NIFTY", "CALL", "ATM", D(2026, 11, 5), D(2026, 11, 6))
    assert ok["fromDate"] == "2026-11-05" and ok["toDate"] == "2026-11-06" and ok["securityId"] == 13
    for end in (D(2026, 11, 5), D(2026, 11, 7), D(2026, 11, 12)):
        with pytest.raises(ValueError):
            rolling.build_request("NIFTY", "CALL", "ATM", D(2026, 11, 5), end)
    assert rolling.build_request("NIFTY", "CALL", "ATM", D(2026, 11, 1), D(2026, 11, 20), max_days=30)["toDate"] == "2026-11-20"


def test_in_range_trims_to_the_days_asked_for():
    ist = rolling.IST
    t = lambda d, h=9, m=15: datetime(2026, 11, d, h, m, tzinfo=ist).timestamp()
    rows = [{"t": t(4)}, {"t": t(5)}, {"t": t(5, 15, 30)}, {"t": t(6)}]
    assert [r["t"] for r in rolling.in_range(rows, D(2026, 11, 5), D(2026, 11, 6))] == [t(5), t(5, 15, 30)]
    assert rolling.in_range(rows, D(2026, 11, 7), D(2026, 11, 9)) == []


def _fake_dhan(log):
    """Answers like the real Dhan: DH-905 when the span is 2+ days; both from and to day returned (toDate inclusive); 77 bars of 5 minutes a weekday."""
    def do_call(body):
        log.append((body["fromDate"], body["toDate"]))
        f, t = D.fromisoformat(body["fromDate"]), D.fromisoformat(body["toDate"])
        if (t - f).days > 1 or t < f:
            raise urllib.error.HTTPError("http://x", 400, "bad", {}, io.BytesIO(b'{"errorCode":"DH-905"}'))
        ts, d = [], f
        while d <= t:
            if d.weekday() < 5:
                t0 = datetime(d.year, d.month, d.day, 9, 15, tzinfo=rolling.IST).timestamp()
                ts += [t0 + 300 * i for i in range(77)]
            d += timedelta(days=1)
        n = len(ts)
        side = {"timestamp": ts, "close": [100.0] * n, "open": [100.0] * n, "high": [101.0] * n, "low": [99.0] * n, "volume": [1] * n, "oi": [1] * n, "iv": [14.0] * n, "spot": [24500.0] * n, "strike": [24500.0] * n}
        return {"data": {"ce": side, "pe": side}}
    return do_call


@pytest.mark.parametrize("start,end,days", [(D(2026, 11, 2), D(2026, 11, 7), 5), (D(2026, 11, 2), D(2026, 11, 6), 4), (D(2026, 11, 3), D(2026, 11, 4), 1), (D(2026, 10, 26), D(2026, 11, 9), 10)])
def test_fetch_series_gets_every_trading_day_once(start, end, days):
    log = []
    s = F.fetch_series(_fake_dhan(log), "NIFTY", "CALL", "ATM", start, end, 5, "WEEK", 1, pause=0, sleep=lambda x: None, say=lambda m: None)
    ts = [r["t"] for r in s["rows"]]
    assert s["failed_windows"] == [] and len(ts) == 77 * days and len(set(ts)) == len(ts) and ts == sorted(ts)
    assert {datetime.fromtimestamp(t, rolling.IST).date() for t in ts} == set(_weekdays(start, end))      # nothing from outside [start, end)


def test_a_too_wide_span_is_recorded_as_failed_never_as_a_complete_file(tmp_path):
    log = []
    s = F.fetch_series(_fake_dhan(log), "NIFTY", "CALL", "ATM", D(2026, 11, 2), D(2026, 11, 7), 5, "WEEK", 1, pause=0, sleep=lambda x: None, say=lambda m: None, span_days=5)
    assert s["rows"] == [] and s["failed_windows"] and "DH-905" in s["failed_windows"][0][2]
    assert len(log) == len(s["failed_windows"])                                   # a 400 is not retried
    good = tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json"
    path, what = F.write_series(good, {}, s)
    assert path is None and not good.exists()


def test_cli_has_span_days_and_a_bad_value_is_refused(monkeypatch, capsys):
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "x")
    for bad in ("0", "31", "-2"):
        monkeypatch.setattr("sys.argv", ["f", "--from", "2026-11-02", "--to", "2026-11-07", "--span-days", bad, "--out", "/nonexistent-zzz"])
        assert F.main() == 2
    assert "--span-days must be 1 to 30" in capsys.readouterr().err
    assert F.rolling.MAX_DAYS == 1


def test_the_probe_script_never_prints_the_token():
    src = (ROOT / "scripts" / "fno_rolling_probe.py").read_text(encoding="utf-8")
    assert "print(" in src and "TOK" in src
    assert not any("TOK" in line and "print(" in line for line in src.splitlines())
