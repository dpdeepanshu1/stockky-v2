"""Round 318: (1) read_day only yields rows the replay can use (finite numeric ts, chain object), so one odd line no longer crashes a whole replay day; the recorder's `extra`
can never overwrite ts / chain. (2) the real-order probe refuses a live run on an exchange holiday, not only on a weekend."""
from __future__ import annotations

import os

import pytest

from backtest import replay
from fno import recorder
from test_engine_paper import NOW as ENOW, make_chain
from test_round261 import NOW, FakeBroker, go

DAY = "2026-11-10"


def _write(root, *lines):
    p = recorder.snapshot_path(root, DAY, "NIFTY")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


@pytest.mark.parametrize("bad", ['{"chain":{"spot":1}}', '{"ts":1762750000}', '{"ts":"x","chain":{}}', '{"ts":1762750000,"chain":5}',
                                 '{"ts":NaN,"chain":{}}', '{"ts":Infinity,"chain":{}}', '{"ts":true,"chain":{}}', '{"ts":null,"chain":{}}'])
def test_a_line_the_replay_cannot_use_is_skipped_by_read_day(tmp_path, bad):
    good = '{"ts":1762750100,"chain":{"underlying":"NIFTY"}}'
    _write(str(tmp_path), bad, good)
    assert [r["ts"] for r in recorder.read_day(str(tmp_path), DAY, "NIFTY")] == [1762750100]


@pytest.mark.parametrize("bad", ['{"chain":{"spot":1}}', '{"ts":"x","chain":{}}', '{"ts":NaN,"chain":{}}'])
def test_the_replay_does_not_crash_on_such_a_line(tmp_path, bad):
    _write(str(tmp_path), bad)
    r = replay.run(str(tmp_path), [DAY], {}, None, ["NIFTY"])
    assert r["days"] == 0                                    # nothing usable that day: skipped, not raised


def test_extra_cannot_overwrite_ts_or_chain(tmp_path):
    ch = make_chain(fetched_at=ENOW.timestamp())
    path = recorder.record(str(tmp_path), ch, ENOW.timestamp(), {"vix": 14.0, "ts": 5, "chain": "x"})
    rows = list(recorder.read_day(str(tmp_path), recorder.os.path.basename(os.path.dirname(path)), "NIFTY"))
    assert len(rows) == 1 and rows[0]["ts"] == ENOW.timestamp() and isinstance(rows[0]["chain"], dict) and rows[0]["vix"] == 14.0


def test_probe_refuses_live_on_an_exchange_holiday_but_not_on_other_days():
    b = FakeBroker(["fill", "fill"])
    r = go(b, holidays=(NOW.date().isoformat(),))
    assert r["exit_code"] == 2 and b.placed == [] and "exchange holiday" in r["reason"]
    assert go(FakeBroker(["fill", "fill"]), holidays=("2026-11-24",))["exit_code"] == 0
    assert "exchange holiday" in "\n".join(go(FakeBroker([]), live=False, holidays=(NOW.date().isoformat(),))["lines"])      # a dry run says it would be refused
