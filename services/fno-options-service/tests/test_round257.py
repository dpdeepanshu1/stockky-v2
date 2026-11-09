"""Round 257: scripts/fno_rolling_fetch.py --resume finishes a run that ended with failed windows: only those windows are asked again, a complete series is skipped,
a file made for another range / span is never reused. No network: a fake Dhan that can be told to fail on one day."""
from __future__ import annotations

import importlib.util
import io
import json
import urllib.error
from datetime import date as D, datetime, timedelta
from pathlib import Path

from backtest import rolling

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("fno_rolling_fetch", ROOT / "scripts" / "fno_rolling_fetch.py")
F = importlib.util.module_from_spec(spec)
spec.loader.exec_module(F)
START, END = D(2026, 11, 2), D(2026, 11, 7)


def fake(log, fail_from=()):
    def do_call(body):
        log.append((body["fromDate"], body["toDate"]))
        if body["fromDate"] in fail_from:
            raise urllib.error.HTTPError("http://x", 500, "boom", {}, io.BytesIO(b"{}"))
        f, t = D.fromisoformat(body["fromDate"]), D.fromisoformat(body["toDate"])
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


def _meta(**kw):
    m = {"underlying": "NIFTY", "type": "CALL", "strike": "ATM", "interval": 5, "expiry_flag": "WEEK", "expiry_code": 1, "from": START.isoformat(), "to": END.isoformat(), "span_days": 1}
    m.update(kw)
    return m


def _first_run(tmp_path, fail_from=("2026-11-04",)):
    path = tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json"
    s = F.fetch_series(fake([], fail_from), "NIFTY", "CALL", "ATM", START, END, 5, "WEEK", 1, pause=0, retries=0, sleep=lambda x: None, say=lambda m: None)
    F.write_series(path, _meta(), s)
    return path, s


def test_only_the_failed_windows_are_asked_again_and_the_series_completes(tmp_path):
    path, s1 = _first_run(tmp_path)
    assert len(s1["failed_windows"]) == 1 and Path(str(path) + ".partial").exists() and not path.exists()
    prior, kind = F.load_prior(path, _meta())
    assert kind == "partial"
    log = []
    todo = [(D.fromisoformat(w[0]), D.fromisoformat(w[1])) for w in prior["failed_windows"]]
    redo = F.fetch_series(fake(log), "NIFTY", "CALL", "ATM", START, END, 5, "WEEK", 1, pause=0, sleep=lambda x: None, say=lambda m: None, only=todo)
    assert log == [("2026-11-04", "2026-11-05")]                                   # one call, not the whole week
    s2 = F.resume_series(prior, redo, START, END)
    ts = [r["t"] for r in s2["rows"]]
    assert s2["failed_windows"] == [] and len(ts) == 77 * 5 and len(set(ts)) == len(ts) and ts == sorted(ts)
    p, what = F.write_series(path, _meta(), s2)
    assert what == "complete" and json.loads(path.read_text())["complete"] is True and not Path(str(path) + ".partial").exists()


def test_a_window_that_fails_again_stays_listed_and_the_file_stays_partial(tmp_path):
    path, _ = _first_run(tmp_path)
    prior, _k = F.load_prior(path, _meta())
    todo = [(D.fromisoformat(w[0]), D.fromisoformat(w[1])) for w in prior["failed_windows"]]
    redo = F.fetch_series(fake([], ("2026-11-04",)), "NIFTY", "CALL", "ATM", START, END, 5, "WEEK", 1, pause=0, retries=0, sleep=lambda x: None, say=lambda m: None, only=todo)
    s = F.resume_series(prior, redo, START, END)
    assert len(s["failed_windows"]) == 1 and len(s["rows"]) == 77 * 4          # 5 Nov is also in the (5, 6) call; only 4 Nov is lost


def test_a_complete_file_is_skipped_and_a_file_for_another_range_or_span_is_not_reused(tmp_path):
    path, _ = _first_run(tmp_path, fail_from=())
    assert F.load_prior(path, _meta())[1] == "complete"
    for other in (_meta(span_days=2), _meta(strike="ATM+1"), _meta(interval=15)):
        doc, why = F.load_prior(path, other)
        assert doc is None and "another span / strike / interval" in why
    assert F.load_prior(path, _meta(to="2026-11-14"))[1] == "complete"          # round 259: another RANGE is fine, it is extended (test_round259)
    assert F.load_prior(tmp_path / "nothing.json", _meta())[0] is None
    path.write_text("not json")
    assert "unreadable" in F.load_prior(path, _meta())[1]


def test_cli_resume_end_to_end(tmp_path, monkeypatch, capsys):
    calls = []
    state = {"fail": True}
    def do(base, cid, tok, body, timeout=30.0):
        return fake(calls, ("2026-11-04",) if state["fail"] else ())(body)
    monkeypatch.setattr(F, "call", do)
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "x")
    argv = ["f", "--from", "2026-11-02", "--to", "2026-11-07", "--out", str(tmp_path), "--pause", "0", "--retries", "0"]
    monkeypatch.setattr("sys.argv", argv)
    assert F.main() == 1                                                          # PARTIAL: a window failed
    first = len(calls)
    state["fail"] = False
    calls.clear()
    monkeypatch.setattr("sys.argv", argv + ["--resume"])
    assert F.main() == 0
    assert len(calls) == 2 and first == 6                                         # CALL and PUT: one failed window each, not the 3 windows of each series
    d = json.loads((tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json").read_text())
    assert d["complete"] is True and len(d["rows"]) == 77 * 5
    calls.clear()
    assert F.main() == 0
    assert len(calls) == 0                                                         # a complete series is skipped
