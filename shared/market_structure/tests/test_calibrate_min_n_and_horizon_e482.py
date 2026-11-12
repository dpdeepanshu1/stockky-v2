"""E482: a min_n of 0 or below no longer crashes rule_effects / calibrate (an empty side has edge None), and calibrate(horizon=1) counts its T+1 rows."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from market_structure import backtest as B  # noqa: E402
from market_structure import observation as O  # noqa: E402


def _rows(n=60):
    """n observed rows that no gate rule rejects (chase 0.5 ATR, well under every limit), so every rule's rejected side is empty."""
    out = []
    for i in range(n):
        r = (i % 7) - 3.0
        out.append({"obs_ts": 1_700_000_000 + i * 86400, "summary_json": json.dumps({"ms_chase_atr": 0.5, "ms_tqs": i}),
                    "ret_1d": r, "ret_5d": r, "ret_10d": r, "ret_20d": r})
    return out


def test_rule_effects_min_n_zero_or_negative_does_not_raise():
    samples = O.samples_from_rows(_rows())
    for mn in (0, -1):
        eff = B.rule_effects(samples, 10, None, min_n=mn)
        for rule in B.RULES:
            e = eff["rules"][rule]
            assert e["edge"] is None and e["verdict"] == "insufficient_sample"     # nothing was rejected, so there is no edge to judge


def test_calibrate_min_n_zero_or_negative_returns_a_report():
    for mn in (0, -5):
        out = O.calibrate(_rows(), min_n=mn)
        assert out["advisory"] is True and out["samples"] == 60
        assert all(item["verdict"] == "insufficient_sample" for item in out["rules"].values())
        assert out["suggestions"] == []


def test_normal_min_n_verdicts_are_unchanged():
    # a side with enough rows still gets a real verdict; rejected rows do worse by 1.0 -> the rule "helps"
    samples = []
    for i in range(30):
        samples.append({"ts": i, "summary": {"ms_chase_atr": 2.0}, "outcome": {"ret_10d": -1.0}})     # chase 2.0 > 1.5 -> rejected
        samples.append({"ts": i, "summary": {"ms_chase_atr": 0.5}, "outcome": {"ret_10d": 1.0}})
    e = B.rule_effects(samples, 10, None, min_n=20)["rules"]["chase"]
    assert e["edge"] == 2.0 and e["verdict"] == "helps"
    assert B.rule_effects(samples, 10, None, min_n=40)["rules"]["chase"]["verdict"] == "insufficient_sample"


def test_calibrate_horizon_one_reports_its_finished_rows():
    out = O.calibrate(_rows(), horizon=1, min_n=20)
    assert out["horizon"] == 1 and out["finished"] == 60 and out["note"] is None


def test_calibrate_automatic_horizon_still_skips_t_plus_1():
    rows = _rows()
    for r in rows:
        r["ret_5d"] = r["ret_10d"] = r["ret_20d"] = None          # only ret_1d exists
    out = O.calibrate(rows, min_n=20)
    assert out["horizon"] == 5 and out["finished"] == 0 and out["note"] == "insufficient_sample"
    full = O.calibrate(_rows(), min_n=20)
    assert full["horizon"] == 20 and full["finished"] == 60          # the longest horizon with enough rows, as before
