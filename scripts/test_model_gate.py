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
    assert mg.metrics({}) == {"f1": None, "top_precision": None, "lift": None, "top_lift": None}
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
