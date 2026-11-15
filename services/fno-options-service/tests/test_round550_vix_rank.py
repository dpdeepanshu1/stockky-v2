"""Round 550 (P1): India VIX against its own recorded history, for the Chain tab. Read only; the engine never calls it; the chain route gets a separate `vix_rank` key."""
from __future__ import annotations

import json
import os
from datetime import date, timedelta

import pytest

import config
from fno import recorder, vix_rank
from fno.store import Store
from helpers import make_chain

TODAY = "2026-12-31"


def _day(root, day, vixes, und="NIFTY"):
    """One snapshot file: a line per value in `vixes` (None = a line without a VIX), time ascending."""
    p = recorder.snapshot_path(root, day, und)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        for i, v in enumerate(vixes):
            row = {"ts": 1_800_000_000 + i * 60, "chain": {"underlying": und, "spot": 24500.0}}
            if v is not None:
                row["vix"] = v
            fh.write(json.dumps(row) + "\n")


def _days(root, n, start=date(2026, 11, 1), vix=lambda i: 12.0 + i * 0.5):
    out = []
    for i in range(n):
        d = (start + timedelta(days=i)).isoformat()
        _day(root, d, [vix(i) - 1, vix(i)])
        out.append(d)
    return out


def _warm(root):
    """read every past day (history reads a few per call): the state after the route has been polled a few times"""
    for _ in range(50):
        if vix_rank.history(root, TODAY)["pending"] == 0:
            return


def test_history_is_the_last_vix_of_each_past_day_and_today_is_left_out(tmp_path):
    root = str(tmp_path)
    _day(root, "2026-12-29", [13.0, 14.5, None])           # the last line has no VIX: the last usable one counts
    _day(root, "2026-12-30", [15.0])
    _day(root, TODAY, [99.0])
    h = vix_rank.history(root, TODAY)
    assert h["values"] == [14.5, 15.0] and h["days_recorded"] == 2 and h["pending"] == 0


def test_a_day_with_no_nifty_vix_falls_back_to_the_sensex_file_and_an_empty_day_is_skipped(tmp_path):
    root = str(tmp_path)
    _day(root, "2026-12-28", [None], "NIFTY")
    _day(root, "2026-12-28", [16.2], "SENSEX")
    _day(root, "2026-12-29", [None])
    h = vix_rank.history(root, TODAY)
    assert h["values"] == [16.2] and h["days_recorded"] == 2


def test_bad_vix_values_are_never_used(tmp_path):
    root = str(tmp_path)
    for i, bad in enumerate([0, -3, "12", True, float("nan"), None]):
        _day(root, f"2026-12-{10 + i:02d}", [bad])
    assert vix_rank.history(root, TODAY)["values"] == []


def test_each_past_day_is_read_once_then_remembered(tmp_path, monkeypatch):
    root = str(tmp_path)
    _days(root, 5)
    calls = []
    real = recorder.index_day
    monkeypatch.setattr(recorder, "index_day", lambda *a, **k: (calls.append(a[1]), real(*a, **k))[1])
    vix_rank.history(root, TODAY)
    n1 = len(calls)
    assert n1 >= 5 and os.path.isfile(os.path.join(root, vix_rank.CACHE_NAME))
    vix_rank.history(root, TODAY)
    assert len(calls) == n1, "the second call read no snapshot file"


def test_a_new_day_adds_one_read_and_a_deleted_cache_is_rebuilt(tmp_path):
    root = str(tmp_path)
    days = _days(root, 3)
    assert len(vix_rank.history(root, TODAY)["values"]) == 3
    _day(root, "2026-12-01", [20.0])
    assert vix_rank.history(root, TODAY)["values"][-1] == 20.0
    os.remove(os.path.join(root, vix_rank.CACHE_NAME))
    assert len(vix_rank.history(root, TODAY)["values"]) == 4 and days


def test_a_damaged_cache_file_is_ignored(tmp_path):
    root = str(tmp_path)
    _days(root, 3)
    with open(os.path.join(root, vix_rank.CACHE_NAME), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert len(vix_rank.history(root, TODAY)["values"]) == 3


def test_only_a_few_days_are_read_per_call_and_pending_says_how_many_are_left(tmp_path):
    root = str(tmp_path)
    _days(root, vix_rank.SCAN_PER_CALL + 4)
    h1 = vix_rank.history(root, TODAY)
    assert len(h1["values"]) == vix_rank.SCAN_PER_CALL and h1["pending"] == 4
    h2 = vix_rank.history(root, TODAY)
    assert len(h2["values"]) == vix_rank.SCAN_PER_CALL + 4 and h2["pending"] == 0


def test_the_history_keeps_at_most_a_trading_year(tmp_path):
    root = str(tmp_path)
    _days(root, 30)
    cache = {(date(2026, 11, 1) + timedelta(days=i)).isoformat(): 10.0 for i in range(30)}
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, vix_rank.CACHE_NAME), "w", encoding="utf-8") as fh:
        json.dump(cache, fh)
    assert len(vix_rank.history(root, TODAY)["values"]) == 30
    assert vix_rank.MAX_DAYS >= 250


def test_too_little_history_gives_no_rank_and_says_how_much_there_is(tmp_path):
    root = str(tmp_path)
    _days(root, 12)
    _warm(root)
    r = vix_rank.read(root, TODAY, 15.0)
    assert r["ready"] is False and r["rank"] is None and r["percentile"] is None and r["days"] == 12 and r["need"] == 20
    assert "12 of 20" in r["note"] and r["vix"] == 15.0 and r["low"] is not None


def test_no_rank_is_given_while_the_history_is_still_being_read(tmp_path):
    root = str(tmp_path)
    _days(root, 25)
    r = vix_rank.read(root, TODAY, 18.0)               # first call: only SCAN_PER_CALL days are read
    assert r["ready"] is False and r["rank"] is None and r["pending"] == 25 - vix_rank.SCAN_PER_CALL and "reading the recorded days" in r["note"]
    _warm(root)
    assert vix_rank.read(root, TODAY, 18.0)["ready"] is True


def test_with_enough_history_rank_and_percentile_are_right(tmp_path):
    root = str(tmp_path)
    _days(root, 25)                                    # VIX 12.0, 12.5 ... 24.0
    _warm(root)
    r = vix_rank.read(root, TODAY, 18.0)
    assert r["ready"] is True and r["days"] == 25 and r["low"] == 12.0 and r["high"] == 24.0
    assert r["rank"] == 50.0                           # (18 - 12) / (24 - 12)
    assert r["percentile"] == 48.0                     # 12 of the 25 past values are below 18
    assert "25 recorded days" in r["note"]
    assert vix_rank.read(root, TODAY, 30.0)["rank"] == 100.0 and vix_rank.read(root, TODAY, 5.0)["rank"] == 0.0     # clamped


@pytest.mark.parametrize("cur", [None, 0, -1, float("nan"), "x", True])
def test_no_usable_current_vix_gives_no_rank_even_with_history(tmp_path, cur):
    root = str(tmp_path)
    _days(root, 25)
    _warm(root)
    r = vix_rank.read(root, TODAY, cur)
    assert r["ready"] is False and r["rank"] is None and r["vix"] is None and "not available" in r["note"] and r["days"] == 25


def test_an_empty_folder_and_a_missing_folder_do_not_raise(tmp_path):
    assert vix_rank.read(str(tmp_path), TODAY, 15.0)["days"] == 0
    r = vix_rank.read(str(tmp_path / "nope"), TODAY, 15.0)
    assert r["ready"] is False and r["days"] == 0


def test_read_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(vix_rank, "history", lambda *a, **k: 1 / 0)
    r = vix_rank.read(str(tmp_path), TODAY, 15.0)
    assert r["ready"] is False and "unavailable" in r["note"]


def test_the_engine_and_the_summary_do_not_use_it():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for name in ("fno/engine.py", "fno/runtime.py", "fno/analytics.py", "fno/signals.py", "fno/strategies.py", "fno/execution.py"):
        with open(os.path.join(here, name), encoding="utf-8") as fh:
            assert "vix_rank" not in fh.read(), name + " must not read the VIX rank"
    src = open(os.path.join(here, "main.py"), encoding="utf-8").read()
    assert src.count("vix_rank.read(") == 1, "only the chain route reads it"
    assert "iv_history" not in src, "the engine's chain_summary calls are untouched"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    monkeypatch.setattr(config, "DATA_DIR", str(tmp_path))
    monkeypatch.setenv("FNO_RUNNER", "0")
    import main
    main.app.dependency_overrides[main.require_admin] = lambda: {"sub": "tester", "admin": True}
    monkeypatch.setattr(main, "store", Store(":memory:"))
    yield TestClient(main.app), main
    main.app.dependency_overrides.clear()


def _wire(main, monkeypatch, vix):
    ch = make_chain()
    monkeypatch.setattr(main.provider, "chain", lambda u, e: ch)
    monkeypatch.setattr(main.provider, "expiries", lambda u: ["2026-11-17"])
    monkeypatch.setattr(main.provider, "daily", lambda u: [])
    monkeypatch.setattr(main.provider, "intraday", lambda u: [])
    monkeypatch.setattr(main.provider, "vix", lambda: vix)
    monkeypatch.setattr(main.engine, "pick_expiry", lambda *a, **k: "2026-11-17")   # the route calls the engine INSTANCE (main.engine); main.eng is the module


def test_chain_route_adds_vix_rank_as_its_own_key_and_leaves_the_summary_alone(client, monkeypatch, tmp_path):
    c, main = client
    _wire(main, monkeypatch, 17.5)
    _days(str(tmp_path), 25, start=date(2026, 1, 5))
    for _ in range(5):
        vix_rank.history(str(tmp_path), "2099-01-01")   # the route reads a few days per call; read them all first
    d = c.get("/fno/chain?underlying=NIFTY").json()
    assert set(("chain", "summary", "bias", "expiries", "age_s", "vix_rank")) <= set(d)
    assert d["vix_rank"]["ready"] is True and d["vix_rank"]["vix"] == 17.5 and d["vix_rank"]["days"] == 25
    assert "vix_rank" not in d["summary"] and "iv_rank" not in d["summary"]


def test_chain_route_still_answers_when_there_is_no_history_or_no_vix(client, monkeypatch):
    c, main = client
    _wire(main, monkeypatch, None)
    d = c.get("/fno/chain?underlying=SENSEX").json()
    assert d["vix_rank"]["ready"] is False and d["vix_rank"]["days"] == 0 and d["chain"]["underlying"] in ("NIFTY", "SENSEX")


def test_chain_route_survives_a_failing_vix_rank(client, monkeypatch):
    c, main = client
    _wire(main, monkeypatch, 15.0)
    monkeypatch.setattr(main.vix_rank, "read", lambda *a, **k: 1 / 0)
    r = c.get("/fno/chain?underlying=NIFTY")
    assert r.status_code == 200 and r.json()["vix_rank"] is None