"""group 362: the structure edge report now carries an advisory calibration block (rule edges, score buckets, out-of-sample threshold hints).
The DB touch point (_since) is replaced by an in-memory fake; nothing here needs sqlalchemy."""
from __future__ import annotations

import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from entry_engine import structure_observation as so

DAY = 86400.0
NOW = 1_700_000_000.0 + 6 * 3600


class FakeRow:
    def __init__(self, **kw):
        self.is_complete = True
        self.bars_seen = 20
        self.reasons_json = "[]"
        self.__dict__.update(kw)


class FakeDb:
    def __init__(self, rows):
        self.rows = rows


def _rows(n=90):
    out = []
    for i in range(n):
        chased = i % 3 == 0
        ret = -2.0 if chased else 1.5
        sm = {"ms_quality": "ok", "ms_trend": "bullish", "ms_tqs": 30 if chased else 70, "ms_chase_atr": 9.0 if chased else 0.5, "ms_rr": 2.0,
              "ms_stretch_atr": 1.0, "ms_room_atr": 2.0}
        out.append(FakeRow(symbol=f"S{i}", source="t", obs_day="d", obs_ts=NOW + i * 3600, price=100.0, stop=95.0, target=110.0, atr=2.0,
                           verdict="rejected_enforced" if chased else "passed", gate_mode="enforce", ret_1d=ret / 4, ret_5d=ret, ret_10d=ret,
                           ret_20d=ret, mfe_pct=abs(ret), mae_pct=-1.0, first_touch="stop" if chased else "target", summary_json=json.dumps(sm), mode="REAL"))
    return out


def _report(rows):
    db = FakeDb(rows)
    with mock.patch.object(so, "_since", lambda d, m, t, limit=5000: sorted([r for r in d.rows if r.mode == m and r.obs_ts >= t], key=lambda r: -r.obs_ts)):
        return so.report(db, "REAL", days=60, recent=3, min_n=20, now_ts=NOW + 30 * DAY)


def test_report_carries_calibration_built_from_stored_summaries():
    out = _report(_rows())
    c = out["calibration"]
    assert c["advisory"] is True and c["samples"] == 90 and c["horizon"] == 20
    assert c["rules"]["chase"]["verdict"] == "helps" and c["rules"]["chase"]["rejected"]["n"] == 30 and c["score"]["monotonic"] is True
    assert out["stats"]["conclusion"] == "gate_helps" and len(out["recent"]) == 3     # existing report content unchanged


def test_calibration_uses_this_gates_own_thresholds():
    th = _report(_rows())["calibration"]["thresholds"]
    assert th["chase_atr"] == float(config.STRUCTURE_MAX_CHASE_ATR) == 1.5


def test_rows_without_a_stored_summary_are_ignored_not_fatal():
    rows = _rows(30)
    for r in rows:
        r.summary_json = None
    c = _report(rows)["calibration"]
    assert c["samples"] == 0 and c["note"] == "insufficient_sample"
    class NoCol:                      # an older row object with no summary_json attribute at all
        pass
    r0 = NoCol()
    r0.__dict__.update({k: v for k, v in vars(_rows(1)[0]).items() if k != "summary_json"})
    assert _report([r0])["calibration"]["samples"] == 0


def test_a_calibration_failure_never_breaks_the_report():
    with mock.patch.object(config, "STRUCTURE_MAX_CHASE_ATR", "not-a-number"):
        out = _report(_rows())
    assert out["calibration"]["advisory"] is True and "error" in out["calibration"] and out["stats"]["total"] == 90
