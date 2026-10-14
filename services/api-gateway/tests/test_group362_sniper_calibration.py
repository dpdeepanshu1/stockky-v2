"""tests/test_group362_sniper_calibration.py -- advisory calibration inside the Buy Sniper edge report (group 362).

No network, no real kv store, no clock. Run from services/api-gateway:  python3 -m pytest tests/test_group362_sniper_calibration.py -v
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import sniper_observation as so  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 9, 1, 11, 0, tzinfo=IST).timestamp()
DAY = 86400.0


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("SNIPER_OBS_") or k.startswith("STRUCTURE_"):
            monkeypatch.delenv(k, raising=False)
    so.reset_stats()
    yield
    so.reset_stats()


class FakeKv:
    def __init__(self):
        self.d = {}
        self.store = so.KvStore(self.get, self.set)

    def get(self, k):
        v = self.d.get(k)
        return json.loads(json.dumps(v)) if v is not None else None

    def set(self, k, v, ttl=None):
        self.d[k] = json.loads(json.dumps(v))


def seed(kv, n_days=12, per_day=10, chased_ret=-2.0, ok_ret=1.5):
    """Finished rows: every third symbol is chased (2.5 ATR, rejected_enforced) and does worse; the rest pass."""
    obs, candles = so._ms()
    idx = {}
    for d in range(n_days):
        ts = T0 + d * DAY
        rows = {}
        for i in range(per_day):
            chased = i % 3 == 0
            sm = {"ms_quality": "ok", "ms_trend": "bullish", "ms_tqs": 30 if chased else 70 + i, "ms_rr": 2.0, "ms_chase_atr": 2.5 if chased else 0.5,
                  "ms_stretch_atr": 1.0, "ms_room_atr": 2.0}
            r = obs.new_observation(f"S{d}X{i}", so.SOURCE, ts + i, 100.0, "rejected_enforced" if chased else "passed", stop=95, target=110, atr=2.0,
                                    gate_mode="enforce", reasons=["chase"] if chased else [], summary=sm)
            ret = chased_ret if chased else ok_ret
            r.update(ret_1d=ret / 4, ret_5d=ret, ret_10d=ret, ret_20d=ret, mfe_pct=abs(ret), mae_pct=-1.0, first_touch="stop" if chased else "target",
                     bars_seen=20, complete=True)
            rows[r["symbol"]] = r
        day = candles.ist_date(ts)
        kv.set(so.DAY_PREFIX + day, {"rows": rows})
        idx[day] = {"n": per_day, "open": 0}
    kv.set(so.INDEX_KEY, {"days": idx})


def test_empty_report_carries_an_insufficient_calibration():
    out = so.report(FakeKv().store, now_ts=T0)
    c = out["calibration"]
    assert c["advisory"] is True and c["samples"] == 0 and c["note"] == "insufficient_sample" and c["suggestions"] == []


def test_report_calibration_says_the_chase_rule_helps_when_chased_setups_did_worse():
    kv = FakeKv()
    seed(kv)
    out = so.report(kv.store, days=60, recent=2, min_n=20, now_ts=T0 + 30 * DAY)
    c = out["calibration"]
    assert c["samples"] == 120 and c["horizon"] == 20 and c["note"] is None
    r = c["rules"]["chase"]
    assert r["verdict"] == "helps" and r["advice"] == "keep" and r["edge"] > 3 and r["rejected"]["n"] == 48 and r["passed"]["n"] == 72
    assert c["score"]["monotonic"] is True
    assert out["stats"]["conclusion"] == "gate_helps"              # the aggregate verdict is untouched by the new block
    assert "summary_json" not in out["recent"][0]


def test_calibration_uses_the_sniper_gates_own_thresholds(monkeypatch):
    kv = FakeKv()
    seed(kv, n_days=2)
    assert so.report(kv.store, now_ts=T0 + 30 * DAY)["calibration"]["thresholds"]["chase_atr"] == 1.5
    monkeypatch.setenv("STRUCTURE_MAX_CHASE_ATR", "3.0")
    monkeypatch.setenv("STRUCTURE_MIN_RR", "1.5")
    th = so.report(kv.store, now_ts=T0 + 30 * DAY)["calibration"]["thresholds"]
    assert th["chase_atr"] == 3.0 and th["rr"] == 1.5 and th["stretch_atr"] == 3.0
    monkeypatch.setenv("STRUCTURE_MAX_CHASE_ATR", "nan")           # garbage falls back to the shipped default
    assert so.report(kv.store, now_ts=T0 + 30 * DAY)["calibration"]["thresholds"]["chase_atr"] == 1.5


def test_a_calibration_failure_never_breaks_the_edge_report(monkeypatch):
    kv = FakeKv()
    seed(kv, n_days=2)
    real = so._ms

    class Boom:
        def __init__(self, o):
            self._o = o

        def __getattr__(self, n):
            if n == "calibrate":
                raise RuntimeError("boom")
            return getattr(self._o, n)

    monkeypatch.setattr(so, "_ms", lambda: (Boom(real()[0]), real()[1]))
    out = so.report(kv.store, now_ts=T0 + 30 * DAY)
    assert out["stats"]["total"] == 20 and out["calibration"]["advisory"] is True and "boom" in out["calibration"]["error"]
    assert "error" not in out


def test_nothing_is_applied_automatically():
    kv = FakeKv()
    seed(kv)
    before = json.dumps(kv.d, sort_keys=True)
    so.report(kv.store, now_ts=T0 + 30 * DAY)
    assert json.dumps(kv.d, sort_keys=True) == before              # read-only
