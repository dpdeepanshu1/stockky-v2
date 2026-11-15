"""E575 round 550: scripts/model_gate.py (champion / challenger gate for the nightly prediction model). Pure; synthetic meta files."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import model_gate as mg


def meta(f1=0.30, top_p=0.34, target=4.0, look=10, recall=0.30, rate=0.22, top_rate=0.10, top_recall=0.15):
    return {"target_gain_pct": target, "lookahead_days": look,
            "threshold_stats": {"chosen": {"f1": f1, "precision": 0.30, "recall": recall, "pred_pos_rate": rate},
                                "top_quantile": {"top_9pct": {"precision": top_p, "recall": top_recall, "pred_pos_rate": top_rate},
                                                 "top_19pct": {"precision": 0.30, "recall": 0.27, "pred_pos_rate": 0.19}}}}


def test_metrics_and_lift():
    m = mg.metrics(meta())
    assert m["f1"] == 0.30 and m["top_precision"] == 0.34
    assert abs(m["lift"] - round(0.30 / 0.22, 3)) < 1e-9 and abs(m["top_lift"] - 1.5) < 1e-9     # the TOP slice is the smallest pred_pos_rate
    assert mg.metrics({}) == {"f1": None, "top_precision": None, "lift": None, "top_lift": None, "top_slice": None}
    assert m["top_slice"] == 0.10
    assert mg.metrics({"threshold_stats": "bad"})["f1"] is None


def test_promote_when_not_worse_or_a_little_worse_within_the_allowed_drop():
    assert mg.decide(meta(), meta())["decision"] == "promote"
    assert mg.decide(meta(), meta(f1=0.29, top_p=0.33))["decision"] == "promote"           # -0.01 each: inside 0.02
    assert mg.decide(meta(), meta(f1=0.35, top_p=0.40))["decision"] == "promote"


def test_keep_old_when_clearly_worse():
    r = mg.decide(meta(), meta(f1=0.25))
    assert r["decision"] == "keep_old" and "f1" in r["reason"]
    r2 = mg.decide(meta(), meta(top_p=0.30))
    assert r2["decision"] == "keep_old" and "top_precision" in r2["reason"]


def test_promote_with_a_note_when_not_comparable_or_no_champion_or_no_metric():
    r = mg.decide(meta(target=4.0), meta(target=5.0, f1=0.10))
    assert r["decision"] == "promote" and "not comparable" in r["reason"]
    assert mg.decide(meta(look=10), meta(look=5, f1=0.10))["decision"] == "promote"
    assert mg.decide(None, meta())["decision"] == "promote"
    assert mg.decide({"target_gain_pct": 4.0}, {"target_gain_pct": 4.0})["decision"] == "promote"


def test_unreadable_challenger_is_never_promoted():
    r = mg.decide(meta(), None)
    assert r["decision"] == "keep_old" and "unreadable" in r["reason"]
    assert mg.decide(None, None)["decision"] == "keep_old"


def test_main_exit_codes_and_json(tmp_path):
    old, new, out = tmp_path / "old.json", tmp_path / "new.json", tmp_path / "d" / "gate.json"
    old.write_text(json.dumps(meta()))
    new.write_text(json.dumps(meta(f1=0.10)))
    assert mg.main(["--old", str(old), "--new", str(new), "--json", str(out)]) == 3
    assert json.loads(out.read_text())["decision"] == "keep_old"
    new.write_text(json.dumps(meta(f1=0.31)))
    assert mg.main(["--old", str(old), "--new", str(new)]) == 0
    assert mg.main(["--old", str(tmp_path / "missing.json"), "--new", str(new)]) == 0        # no champion yet
    assert mg.main(["--old", str(old), "--new", str(tmp_path / "missing.json")]) == 3         # challenger unreadable
    new.write_text("not json")
    assert mg.main(["--old", str(old), "--new", str(new)]) == 3


def test_current_repo_meta_reads_and_lifts_are_above_one():
    here = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(here, "..", "services", "decision-prediction-service", "prediction", "model_meta.json")
    m = mg.read_meta(p)
    assert m is not None
    x = mg.metrics(m)
    assert x["f1"] and x["lift"] and x["lift"] > 1.0 and x["top_lift"] > x["lift"]
    assert mg.decide(m, m)["decision"] == "promote"


# ----------------------------------------------------------------------------------------------- E589 round 564: drift floor (anchor)
def test_staircase_without_anchor_walks_down_and_with_anchor_stops():
    """Each night is 0.02 worse than the champion (inside 0.03). Without the floor every night is promoted; with it the walk stops at the floor."""
    champ, n_promoted = meta(f1=0.30, top_p=0.34), 0
    for _ in range(10):
        f1 = champ["threshold_stats"]["chosen"]["f1"] - 0.02
        cand = meta(f1=f1, top_p=0.34)
        if mg.decide(champ, cand, 0.03)["decision"] == "promote":
            champ, n_promoted = cand, n_promoted + 1
    assert n_promoted == 10 and champ["threshold_stats"]["chosen"]["f1"] < 0.11          # the old behaviour: 0.30 down to 0.10
    champ, anchor, n_promoted = meta(f1=0.30, top_p=0.34), None, 0
    for _ in range(10):
        f1 = champ["threshold_stats"]["chosen"]["f1"] - 0.02
        cand = meta(f1=f1, top_p=0.34)
        r = mg.decide(champ, cand, 0.03, anchor=anchor, max_total_drop=0.05)
        if r["decision"] == "promote":
            champ, anchor, n_promoted = cand, r["anchor_after"], n_promoted + 1
    assert n_promoted == 2 and abs(champ["threshold_stats"]["chosen"]["f1"] - 0.26) < 1e-9     # 0.28 and 0.26 pass; 0.24 is 0.06 below the anchor 0.30 and is refused every night
    assert anchor["f1"] == 0.30                                                           # the anchor never moved down


def test_drift_floor_reason_names_the_anchor_and_champion_check_comes_first():
    r = mg.decide(meta(f1=0.26), meta(f1=0.245), 0.03, anchor={"f1": 0.30, "top_precision": 0.34, "target_gain_pct": 4.0, "lookahead_days": 10}, max_total_drop=0.05)
    assert r["decision"] == "keep_old" and "best accepted" in r["reason"] and "f1" in r["reason"]
    r2 = mg.decide(meta(f1=0.30), meta(f1=0.20), 0.03, anchor={"f1": 0.30}, max_total_drop=0.05)
    assert r2["decision"] == "keep_old" and "challenger is worse" in r2["reason"]           # the champion check answers first


def test_anchor_moves_up_only_and_starts_from_the_champion_when_missing():
    r = mg.decide(meta(f1=0.30, top_p=0.34), meta(f1=0.32, top_p=0.33), 0.03, anchor=None, max_total_drop=0.05)
    assert r["decision"] == "promote" and r["anchor_after"]["f1"] == 0.32 and r["anchor_after"]["top_precision"] == 0.34
    r2 = mg.decide(meta(f1=0.32), meta(f1=0.31), 0.03, anchor=r["anchor_after"], max_total_drop=0.05)
    assert r2["decision"] == "promote" and r2["anchor_after"]["f1"] == 0.32                 # a lower number does not pull the anchor down
    assert "anchor_after" not in mg.decide(meta(), meta(), 0.03)                           # no floor requested: round 550 behaviour, no extra key


def test_anchor_resets_when_the_question_changes_or_there_is_no_champion():
    r = mg.decide(meta(target=4.0, f1=0.30), meta(target=5.0, f1=0.10), 0.03, anchor={"f1": 0.30, "target_gain_pct": 4.0, "lookahead_days": 10}, max_total_drop=0.05)
    assert r["decision"] == "promote" and r["anchor_after"]["f1"] == 0.10 and r["anchor_after"]["target_gain_pct"] == 5.0
    r2 = mg.decide(meta(target=5.0, f1=0.10), meta(target=5.0, f1=0.09), 0.03,
                   anchor={"f1": 0.30, "target_gain_pct": 4.0, "lookahead_days": 10}, max_total_drop=0.05)      # stale anchor from the old question is ignored
    assert r2["decision"] == "promote" and r2["anchor_after"]["f1"] == 0.10
    r3 = mg.decide(None, meta(f1=0.30), 0.03, anchor=None, max_total_drop=0.05)
    assert r3["decision"] == "promote" and r3["anchor_after"]["f1"] == 0.30


def test_main_writes_the_anchor_only_on_a_promotion(tmp_path):
    old, new, anc = tmp_path / "old.json", tmp_path / "new.json", tmp_path / "state" / "anchor.json"
    old.write_text(json.dumps(meta(f1=0.30)))
    new.write_text(json.dumps(meta(f1=0.29)))
    assert mg.main(["--old", str(old), "--new", str(new), "--anchor", str(anc), "--max-drop", "0.03"]) == 0
    assert json.loads(anc.read_text())["f1"] == 0.30                                        # started from the champion, the lower challenger did not lower it
    new.write_text(json.dumps(meta(f1=0.245)))
    old.write_text(json.dumps(meta(f1=0.27)))
    before = anc.read_text()
    assert mg.main(["--old", str(old), "--new", str(new), "--anchor", str(anc), "--max-drop", "0.03"]) == 3     # 0.245 is 0.055 below the anchor, only 0.025 below the champion
    assert anc.read_text() == before                                                        # a rejection never touches the file
    anc.write_text("not json")                                                              # a broken anchor file is the same as none
    assert mg.main(["--old", str(old), "--new", str(new), "--anchor", str(anc), "--max-drop", "0.03"]) == 0
    assert mg.main(["--old", str(old), "--new", str(new), "--max-drop", "0.03"]) == 0       # no --anchor: no floor, no file


# ----------------------------------------------------------------------------------------------- E593 round 568: compare the same slice only
def _only_20pct(f1=0.30, p20=0.30):
    m = meta(f1=f1)
    m["threshold_stats"]["top_quantile"] = {"top_19pct": {"precision": p20, "recall": 0.27, "pred_pos_rate": 0.20}}      # the 10 % slice failed to compute
    return m


def test_top_precision_of_different_slices_is_not_compared():
    """Champion's top slice is 10 % (precision 0.34); the challenger only has the 20 % slice (0.30): 0.04 lower, but a different question. Not a reason to refuse."""
    assert mg.metrics(_only_20pct())["top_slice"] == 0.20
    r = mg.decide(meta(f1=0.30, top_p=0.34), _only_20pct(f1=0.30, p20=0.30), 0.03)
    assert r["decision"] == "promote"
    r2 = mg.decide(meta(f1=0.30, top_p=0.34), meta(f1=0.30, top_p=0.30), 0.03)                       # the same slice, really 0.04 lower
    assert r2["decision"] == "keep_old" and "top_precision" in r2["reason"]
    assert mg.same_slice(None, 0.10) and mg.same_slice(0.10, 0.10) and not mg.same_slice(0.10, 0.20)


def test_f1_is_still_checked_when_the_slices_differ():
    r = mg.decide(meta(f1=0.30, top_p=0.34), _only_20pct(f1=0.20), 0.03)
    assert r["decision"] == "keep_old" and "f1" in r["reason"]


def test_anchor_does_not_mix_slices():
    anchor = {"f1": 0.30, "top_precision": 0.34, "top_slice": 0.10, "target_gain_pct": 4.0, "lookahead_days": 10}
    r = mg.decide(meta(f1=0.30), _only_20pct(f1=0.30, p20=0.25), 0.03, anchor=anchor, max_total_drop=0.05)       # 0.25 would be 0.09 below 0.34 if compared
    assert r["decision"] == "promote"
    assert r["anchor_after"]["top_precision"] == 0.25 and r["anchor_after"]["top_slice"] == 0.20                  # the challenger's own slice stands
    same = mg.decide(meta(f1=0.30), meta(f1=0.30, top_p=0.36), 0.03, anchor=anchor, max_total_drop=0.05)
    assert same["anchor_after"]["top_precision"] == 0.36 and same["anchor_after"]["top_slice"] == 0.10
