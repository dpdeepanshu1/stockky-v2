"""Round 302: the chain recorder survives a cut-short line, an unserialisable value and a damaged file; one underlying that fails does not stop the other, and a failing engine cycle does not stop recording."""
from __future__ import annotations

import os

import pytest

from fno import recorder, runtime
from fno.store import Store
from test_engine_paper import NOW, FakeProvider, engine, make_cfg, make_chain

DAY = "2026-11-10"


def _chain():
    return make_chain(fetched_at=NOW.timestamp())


def test_a_cut_short_line_loses_only_itself_not_the_next_snapshot(tmp_path):
    root = str(tmp_path)
    path = recorder.record(root, _chain(), NOW.timestamp())
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"ts":1,"chain":{"under')                           # a write cut short: no newline
    recorder.record(root, _chain(), NOW.timestamp() + 60)
    rows = list(recorder.read_day(root, DAY, "NIFTY"))
    assert [r["ts"] for r in rows] == [NOW.timestamp(), NOW.timestamp() + 60]


def test_a_value_that_cannot_be_written_is_a_miss_not_an_exception(tmp_path):
    ch = _chain()
    ch.to_json = lambda: {"x": object()}
    assert recorder.record(str(tmp_path), ch, NOW.timestamp()) is None
    assert not os.path.exists(recorder.snapshot_path(str(tmp_path), DAY, "NIFTY"))      # nothing half-written


def test_a_damaged_file_is_skipped_line_by_line(tmp_path):
    root = str(tmp_path)
    path = recorder.record(root, _chain(), NOW.timestamp())
    with open(path, "ab") as fh:
        fh.write(b"123\nnull\n[1,2]\n\xff\xfe not json\n")
    recorder.record(root, _chain(), NOW.timestamp() + 60)
    assert len(list(recorder.read_day(root, DAY, "NIFTY"))) == 2


def test_a_file_that_cannot_be_opened_yields_nothing(tmp_path):
    os.makedirs(recorder.snapshot_path(str(tmp_path), DAY, "NIFTY"))                  # a directory where the file should be
    assert list(recorder.read_day(str(tmp_path), DAY, "NIFTY")) == []
    assert list(recorder.read_day(str(tmp_path), "2020-01-01", "NIFTY")) == []        # no file at all, as before


class _OneBadProvider(FakeProvider):
    def chain(self, u, e):
        if u == "NIFTY":
            raise RuntimeError("provider blew up")
        return super().chain(u, e)


def test_one_underlying_failing_does_not_stop_the_other_from_being_recorded(tmp_path):
    db = Store(":memory:")
    e = engine(db, _OneBadProvider(), make_cfg(FNO_UNDERLYINGS="BOTH"))
    r = runtime.Runner(e, e.cfg, str(tmp_path))
    r.snapshot(NOW)
    assert recorder.list_days(str(tmp_path)) == [DAY] and len(list(recorder.read_day(str(tmp_path), DAY, "SENSEX"))) == 1
    assert list(recorder.read_day(str(tmp_path), DAY, "NIFTY")) == [] and r.snapshots_today >= 1
    db.close()


def test_recording_goes_on_when_the_engine_cycle_raises(tmp_path, monkeypatch):
    db = Store(":memory:")
    e = engine(db, FakeProvider(), make_cfg(FNO_UNDERLYINGS="NIFTY", FNO_ENGINE_ENABLED=True))
    r = runtime.Runner(e, e.cfg, str(tmp_path))
    calls = []
    monkeypatch.setattr(runtime, "now_ist", lambda: NOW)
    monkeypatch.setattr(runtime.capital, "publish", lambda *a, **k: None)
    monkeypatch.setattr(runtime.readiness, "publish", lambda *a, **k: None)
    monkeypatch.setattr(e, "tick", lambda *a, **k: (r._stop.set(), (_ for _ in ()).throw(RuntimeError("tick failed")))[1])
    monkeypatch.setattr(r, "snapshot", lambda now: calls.append("snap"))
    r._loop()
    assert calls == ["snap"] and r.last_error and "tick failed" in r.last_error
    db.close()
