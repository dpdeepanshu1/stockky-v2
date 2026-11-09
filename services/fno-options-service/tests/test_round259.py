"""Round 259: scripts/fno_rolling_fetch.py --resume EXTENDS a series: a later --to (or an earlier --from) fetches only the missing days and the file then covers the union. No network."""
from __future__ import annotations

import importlib.util
import json
from datetime import date as D
from pathlib import Path

from test_round257 import F, fake, _meta

WEEK1 = (D(2026, 11, 2), D(2026, 11, 7))
M1 = _meta()


def _argv(tmp_path, frm, to, extra=()):
    return ["f", "--from", frm, "--to", to, "--out", str(tmp_path), "--pause", "0", "--retries", "0", *extra]


def _run(monkeypatch, tmp_path, frm, to, extra=(), fail=()):
    calls = []
    monkeypatch.setattr(F, "call", lambda base, cid, tok, body, timeout=30.0: fake(calls, fail)(body))
    monkeypatch.setenv("DHAN_CLIENT_ID", "1")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "x")
    monkeypatch.setattr("sys.argv", _argv(tmp_path, frm, to, extra))
    rc = F.main()
    return rc, calls


def _doc(tmp_path):
    return json.loads((tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json").read_text())


def test_plan_extension_is_the_union_and_fills_a_gap():
    prior = {"from": "2026-11-02", "to": "2026-11-07"}
    assert F.plan_extension(prior, D(2026, 11, 3), D(2026, 11, 6)) == (D(2026, 11, 2), D(2026, 11, 7), [])
    assert F.plan_extension(prior, D(2026, 11, 2), D(2026, 11, 14)) == (D(2026, 11, 2), D(2026, 11, 14), [(D(2026, 11, 7), D(2026, 11, 14))])
    assert F.plan_extension(prior, D(2026, 10, 26), D(2026, 11, 7)) == (D(2026, 10, 26), D(2026, 11, 7), [(D(2026, 10, 26), D(2026, 11, 2))])
    assert F.plan_extension(prior, D(2026, 11, 16), D(2026, 11, 20)) == (D(2026, 11, 2), D(2026, 11, 20), [(D(2026, 11, 7), D(2026, 11, 20))])      # a gap (7..16) is fetched too
    assert F.plan_extension(prior, D(2026, 10, 12), D(2026, 10, 20)) == (D(2026, 10, 12), D(2026, 11, 7), [(D(2026, 10, 12), D(2026, 11, 2))])


def test_a_later_to_fetches_only_the_new_days(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, "2026-11-02", "2026-11-07")[0] == 0
    rc, calls = _run(monkeypatch, tmp_path, "2026-11-02", "2026-11-14", ["--resume"])
    assert rc == 0
    assert min(c[0] for c in calls) >= "2026-11-06" and len(calls) == 2 * 3          # CALL and PUT, only 7..13 (a 7-day range is (7,8) (9,10) (11,12) (13 -> 12,13))
    d = _doc(tmp_path)
    ts = [r["t"] for r in d["rows"]]
    assert d["complete"] is True and d["from"] == "2026-11-02" and d["to"] == "2026-11-14" and len(ts) == 77 * 10 and len(set(ts)) == len(ts) and ts == sorted(ts)
    assert _run(monkeypatch, tmp_path, "2026-11-02", "2026-11-14", ["--resume"]) == (0, [])      # now covered: skipped


def test_an_earlier_from_is_added_in_front(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, "2026-11-09", "2026-11-14")[0] == 0
    rc, calls = _run(monkeypatch, tmp_path, "2026-11-02", "2026-11-14", ["--resume"])
    d = _doc(tmp_path)
    assert rc == 0 and d["from"] == "2026-11-02" and len(d["rows"]) == 77 * 10 and max(c[1] for c in calls) <= "2026-11-08"


def test_a_failed_extension_keeps_the_old_rows_and_the_next_resume_continues_it(monkeypatch, tmp_path):
    assert _run(monkeypatch, tmp_path, "2026-11-02", "2026-11-07")[0] == 0
    rc, calls = _run(monkeypatch, tmp_path, "2026-11-02", "2026-11-14", ["--resume"], fail=("2026-11-11",))
    assert rc == 1
    assert (tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json.partial").exists() and _doc(tmp_path)["to"] == "2026-11-07"          # the complete file is untouched
    part = json.loads((tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json.partial").read_text())
    assert part["to"] == "2026-11-14" and len(part["failed_windows"]) == 1 and len(part["rows"]) > 77 * 5
    rc, calls = _run(monkeypatch, tmp_path, "2026-11-02", "2026-11-14", ["--resume"])
    assert rc == 0 and len(calls) == 2                                                    # only the one failed window per series, not the extension again
    d = _doc(tmp_path)
    assert d["complete"] is True and d["to"] == "2026-11-14" and len(d["rows"]) == 77 * 10 and not (tmp_path / "NIFTY_CALL_ATM_WEEK1_5m.json.partial").exists()
