"""group362: calibrate() - advisory calibration from observed outcomes (rows as the services store them)."""
import json, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from market_structure.observation import CAL_GRIDS, calibrate, samples_from_rows

TS0 = 1_700_000_000


def _row(i, chase, ret, tqs=50, as_json=True):
    sm = {"ms_chase_atr": chase, "ms_tqs": tqs, "ms_rr": 2.0, "ms_room_atr": 2.0, "ms_stretch_atr": 1.0}
    r = {"obs_ts": TS0 + i * 3600, "ret_1d": ret / 4, "ret_5d": ret, "ret_10d": ret, "ret_20d": ret}
    r["summary_json" if as_json else "summary"] = json.dumps(sm) if as_json else sm
    return r


def _rows(n=120):
    rows = []
    for i in range(n):
        bad = i % 3 == 0                                   # a third are chased (> 1.5 ATR) and do worse
        rows.append(_row(i, 2.5 if bad else 0.5, -2.0 if bad else 1.5, tqs=30 if bad else 70 + (i % 5)))
    return rows


def test_samples_from_rows_drops_rows_without_summary_or_ts_and_accepts_both_shapes():
    good = [_row(1, 1.0, 1.0), _row(2, 1.0, 1.0, as_json=False)]
    bad = [{"obs_ts": TS0, "ret_5d": 1.0}, {"summary_json": "{not json", "obs_ts": TS0}, {"summary_json": "{}", "obs_ts": TS0},
           {"summary_json": '{"ms_tqs": 1}'}, "junk", None]
    out = samples_from_rows(good + bad)
    assert len(out) == 2 and out[0]["summary"]["ms_tqs"] == 50 and out[0]["outcome"]["ret_5d"] == 1.0


def test_chase_rule_helps_when_chased_setups_do_worse_and_is_advisory():
    c = calibrate(_rows(), min_n=20)
    assert c["advisory"] is True and c["samples"] == 120 and c["horizon"] == 20 and c["note"] is None
    r = c["rules"]["chase"]
    assert r["verdict"] == "helps" and r["advice"] == "keep" and r["edge"] > 3 and r["rejected"]["n"] == 40 and r["passed"]["n"] == 80
    assert c["rules"]["rr"]["rejected"]["n"] == 0 and c["rules"]["rr"]["advice"] == "collect_more"
    assert c["combined"]["edge"] > 3


def test_rule_that_rejects_the_better_setups_is_flagged_for_review():
    rows = []
    for i in range(120):
        chased = i % 3 == 0
        rows.append(_row(i, 2.5 if chased else 0.5, 3.0 if chased else -1.0))   # the chased ones actually did BETTER
    r = calibrate(rows, min_n=20)["rules"]["chase"]
    assert r["verdict"] == "hurts" and r["advice"] == "review_loosen" and r["edge"] < -3


def test_score_buckets_rank_outcomes():
    s = calibrate(_rows(), min_n=20)["score"]
    assert s["n"] == 120 and len(s["bins"]) == 3 and s["monotonic"] is True and s["spread"] > 3


def test_small_sample_says_so_and_never_suggests():
    c = calibrate(_rows(10), min_n=20)
    assert c["note"] == "insufficient_sample" and c["horizon"] == 5 and c["suggestions"] == []
    assert all(v["advice"] == "collect_more" for v in c["rules"].values() if v["verdict"] == "insufficient_sample")
    e = calibrate([], min_n=20)
    assert e["samples"] == 0 and e["note"] == "insufficient_sample" and e["score"]["note"] == "insufficient_sample"
    assert calibrate(["x", None, {}], min_n=20)["samples"] == 0


def test_threshold_suggestion_needs_stable_out_of_sample_gain_and_a_different_value():
    # shipped chase threshold 3.0 lets 2.0-ATR chases through, but those do worse -> a lower threshold should be suggested out-of-sample
    rows = []
    for i in range(200):
        ch = [0.3, 0.8, 1.2, 2.0, 2.2][i % 5]
        rows.append(_row(i, ch, -2.0 if ch >= 2.0 else 1.5))
    c = calibrate(rows, thresholds={"chase_atr": 3.0}, min_n=20)
    r = c["rules"]["chase"]
    assert r["verdict"] == "insufficient_sample" and r["suggested"] in (1.5, 2.0) and r["walk_forward"]["stable"] is True
    assert c["suggestions"] and c["suggestions"][0]["rule"] == "chase" and c["suggestions"][0]["from"] == 3.0
    # already at the best threshold -> nothing suggested
    assert calibrate(rows, thresholds={"chase_atr": 1.5}, min_n=20)["rules"]["chase"]["suggested"] in (None, 2.0)


def test_explicit_horizon_and_grids_cover_the_four_threshold_rules():
    assert set(CAL_GRIDS) == {"chase", "stretch", "room", "rr"}
    assert calibrate(_rows(), horizon=5, min_n=20)["horizon"] == 5
    assert calibrate(_rows(), horizon=7, min_n=20)["horizon"] == 20     # unknown horizon falls back to the auto pick
