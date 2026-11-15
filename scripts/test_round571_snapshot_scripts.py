"""round 571 (E596): calibration_snapshot.py and control_snapshot.py. Plain asserts or pytest."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import calibration_snapshot as cs  # noqa: E402
import control_snapshot as c  # noqa: E402


def _snap(label, created, settings=None, seeds=None):
    return {"label": label, "created_at": created, "settings": settings or {}, "seeds": seeds if seeds is not None else {"7": {"verdict": "pass", "complete": True}}}


def test_two_newest_uses_created_at_across_offsets():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        # the file-name stamp is the writer's own clock: "T150000" sorts AFTER "T100000" by name, but 15:00 IST is 09:30 UTC, the oldest
        (store / "20261116T150000_ist.json").write_text(json.dumps(_snap("ist-oldest", "2026-11-16T15:00:00+05:30")))
        (store / "20261116T100000_utc1.json").write_text(json.dumps(_snap("utc-mid", "2026-11-16T10:00:00+00:00")))
        (store / "20261116T103000_utc2.json").write_text(json.dumps(_snap("utc-newest", "2026-11-16T10:30:00+00:00")))
        older, newer = cs.two_newest(store)
        assert (older["label"], newer["label"]) == ("utc-mid", "utc-newest")


def test_two_newest_unreadable_created_at_keeps_file_order():
    with tempfile.TemporaryDirectory() as d:
        store = Path(d)
        (store / "20261116T100000_a.json").write_text(json.dumps(_snap("first", "garbage")))
        (store / "20261116T110000_b.json").write_text(json.dumps(_snap("second", "garbage")))
        older, newer = cs.two_newest(store)
        assert (older["label"], newer["label"]) == ("first", "second")


def test_service_unreadable_in_one_snapshot_is_one_warning_not_a_list_of_changes():
    a = _snap("a", "2026-11-16T10:00:00+00:00", {"ENTRY_X": "1", "real-trade:K1": "true", "real-trade:K2": "5", "position-stocks:P1": "on"})
    b = _snap("b", "2026-11-16T11:00:00+00:00", {"ENTRY_X": "2", "position-stocks:P1": "off"})  # real-trade could not be read
    d = cs.compare(a, b)
    assert d["settings"]["removed"] == {}
    assert d["settings"]["changed"] == {"ENTRY_X": ["1", "2"], "position-stocks:P1": ["on", "off"]}
    assert any("real-trade switches are only in the before snapshot" in w for w in d["warnings"])


def test_both_snapshots_with_the_service_still_compare_per_key():
    a = _snap("a", "2026-11-16T10:00:00+00:00", {"real-trade:K1": "true"})
    b = _snap("b", "2026-11-16T11:00:00+00:00", {"real-trade:K1": "false", "real-trade:K3": "1"})
    d = cs.compare(a, b)
    assert d["settings"]["changed"] == {"real-trade:K1": ["true", "false"]} and d["settings"]["added"] == {"real-trade:K3": "1"}
    assert not any("only in the" in w and "switches" in w for w in d["warnings"])


def test_markdown_shows_the_ci_and_keeps_column_count():
    seeds = {"7": {"verdict": "pass", "edge_pp": 0.4, "ci90": [0.1, 0.7], "kept_pct": 50, "candidate_n": 40, "candidate_mean_net": 0.2, "complete": True}}
    md = cs.to_markdown(cs.compare(_snap("a", "2026-11-16T10:00:00+00:00", seeds=seeds), _snap("b", "2026-11-16T11:00:00+00:00", seeds=seeds)))
    assert "90% CI (before -> after)" in md and "[0.1, 0.7] -> [0.1, 0.7]" in md
    rows = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert len({ln.count("|") for ln in rows}) == 1
    only = cs.to_markdown(cs.compare(_snap("a", "2026-11-16T10:00:00+00:00", seeds=seeds), _snap("b", "2026-11-16T11:00:00+00:00", seeds={"8": seeds["7"]})))
    assert len({ln.count("|") for ln in only.splitlines() if ln.startswith("|")}) == 1


def test_control_render_survives_a_flag_without_source_and_a_non_dict_override():
    doc = {"service": "svc", "flags": [{"key": "A", "title": "Gate A", "group": "G", "effective": "1", "live": True, "override": True}]}
    t = c.render(doc)
    assert "Gate A" in t and "[A]" in t and "OVERRIDE by None" in t


def test_control_file_mode_closes_the_file(tmp_path=None):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "f.json"
        p.write_text(json.dumps({"service": "svc", "flags": []}))
        assert c._load(str(p))["service"] == "svc"


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
