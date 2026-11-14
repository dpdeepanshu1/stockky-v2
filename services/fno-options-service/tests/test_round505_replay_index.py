"""Round 505 (E525): the replay reads a recorded day as an index (ts, spot, vix, expiry + byte offset) and parses a snapshot only when the engine asks for its chain, instead of keeping every
parsed snapshot of the day in memory (the 14 Nov VM replay was killed with exit 137). Same answers as the list-based provider, same skipped lines as read_day."""
from __future__ import annotations

import json
import os

from backtest import replay
from fno import recorder

DAY = "2026-11-12"


def _snap(ts, spot=100.0, expiry="2026-11-17", vix=None):
    r = {"ts": ts, "chain": {"underlying": "NIFTY", "expiry": expiry, "spot": spot, "strikes": []}}
    if vix is not None:
        r["vix"] = vix
    return r


def _write(root, lines, und="NIFTY"):
    path = recorder.snapshot_path(root, DAY, und)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        for ln in lines:
            fh.write(ln if isinstance(ln, bytes) else (json.dumps(ln) if not isinstance(ln, str) else ln).encode("utf-8"))
            fh.write(b"\n")
    return path


def test_index_day_yields_the_rows_read_day_yields_and_read_at_loads_the_same_snapshot(tmp_path):
    rows = [_snap(300.0, 101.0, vix=14.0), "not json", b"\xff\xfe broken bytes", _snap(100.0, 99.0), {"ts": "x", "chain": {}}, {"ts": 200.0, "chain": 5}, _snap(200.0, 100.0, "2026-11-24")]
    path = _write(str(tmp_path), rows)
    got_path, meta = recorder.index_day(str(tmp_path), DAY, "NIFTY")
    assert got_path == path and [m[0] for m in meta] == [100.0, 200.0, 300.0]                       # sorted by ts, the four unusable lines skipped
    assert [(m[2], m[3], m[4]) for m in meta] == [(99.0, None, "2026-11-17"), (100.0, None, "2026-11-24"), (101.0, 14.0, "2026-11-17")]
    full = list(recorder.read_day(str(tmp_path), DAY, "NIFTY"))
    assert [recorder.read_at(path, m[1]) for m in meta] == full
    assert recorder.read_at(path, 10 ** 9) is None and recorder.read_at(str(tmp_path / "nope"), 0) is None
    assert recorder.index_day(str(tmp_path), "2020-01-01", "NIFTY")[1] == []


def test_provider_over_a_file_gives_the_same_chain_expiries_bars_and_vix_as_over_a_list(tmp_path):
    t0 = 1_762_920_000.0
    snaps = [_snap(t0 + 60 * i, 24500.0 + 3 * i, "2026-11-17" if i % 3 else "2026-11-24", vix=14.0 + i / 10 if i % 4 == 0 else None) for i in range(40)]
    _write(str(tmp_path), snaps)
    a = replay.ReplayProvider({"NIFTY": snaps}, {})
    b = replay.ReplayProvider({"NIFTY": replay._SnapIndex.from_file(str(tmp_path), DAY, "NIFTY")}, {})
    for i in (0, 1, 5, 13, 39, 20):                                                                 # also goes backwards in time
        for p in (a, b):
            p.set_time(t0 + 60 * i + 5)
        assert a.expiries("NIFTY") == b.expiries("NIFTY") and a.intraday("NIFTY") == b.intraday("NIFTY") and a.vix() == b.vix()
        for ex in ("2026-11-17", "2026-11-24", "2099-01-01"):
            ca, cb = a.chain("NIFTY", ex), b.chain("NIFTY", ex)
            assert (ca is None) == (cb is None) and (ca is None or (ca.fetched_at, ca.spot) == (cb.fetched_at, cb.spot))
    a.set_time(t0 - 1)
    assert a.expiries("NIFTY") == [] and a.chain("NIFTY", "2026-11-17") is None and a.intraday("NIFTY") == []


def test_chain_is_the_newest_snapshot_of_that_expiry_at_or_before_the_time():
    t = 1_000_000.0
    snaps = [_snap(t, 10.0, "E1"), _snap(t + 60, 11.0, "E2"), _snap(t + 120, 12.0, "E1")]
    p = replay.ReplayProvider({"NIFTY": snaps}, {})
    p.set_time(t + 90)
    assert p.chain("NIFTY", "E1").spot == 10.0 and p.chain("NIFTY", "E2").spot == 11.0 and p.expiries("NIFTY") == ["E1", "E2"]
    p.set_time(t + 120)
    assert p.chain("NIFTY", "E1").spot == 12.0 and p.chain("NIFTY", "E1").fetched_at == t + 120
    p.set_time(t + 10)
    assert p.expiries("NIFTY") == ["E1"] and p.chain("NIFTY", "E2") is None


def test_run_never_holds_the_whole_day_in_memory_it_loads_a_snapshot_when_asked(tmp_path, monkeypatch):
    t0 = 1_762_920_000.0
    _write(str(tmp_path), [_snap(t0 + 60 * i, 24500.0 + i) for i in range(30)])
    loaded = []
    real = recorder.read_at
    monkeypatch.setattr(recorder, "read_at", lambda path, off: (loaded.append(off), real(path, off))[1])
    ix = replay._SnapIndex.from_file(str(tmp_path), DAY, "NIFTY")
    assert loaded == [] and len(ix) == 30                                                           # indexing parsed nothing for later use
    p = replay.ReplayProvider({"NIFTY": ix}, {})
    p.set_time(t0 + 600)
    p.chain("NIFTY", "2026-11-17")
    p.chain("NIFTY", "2026-11-17")
    assert len(loaded) == 1                                                                         # one parse, the second call came from the small cache


def test_index_day_skips_the_same_unusable_lines_as_read_day_and_run_does_not_crash(tmp_path):
    for i, bad in enumerate(['{"chain":{"spot":1}}', '{"ts":1762750000}', '{"ts":"x","chain":{}}', '{"ts":1762750000,"chain":5}', '{"ts":NaN,"chain":{}}',
                             '{"ts":Infinity,"chain":{}}', '{"ts":true,"chain":{}}', '{"ts":null,"chain":{}}']):
        root = str(tmp_path / str(i))
        _write(root, [bad, '{"ts":1762750100,"chain":{"underlying":"NIFTY"}}'])
        assert [m[0] for m in recorder.index_day(root, DAY, "NIFTY")[1]] == [r["ts"] for r in recorder.read_day(root, DAY, "NIFTY")] == [1762750100]
        _write(root, [bad])
        assert replay.run(root, [DAY], {}, None, ["NIFTY"])["days"] == 0
