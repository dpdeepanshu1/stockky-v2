"""group468: scripts/calibration_snapshot.py (stdlib only, temp dirs)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import calibration_snapshot as cs


def _ab(out, seed, verdict="no_edge", edge=0.5, base=200, cand=120):
    d = Path(out) / f"ab_{seed}"
    d.mkdir(parents=True)
    (d / "ab_report.json").write_text(json.dumps({
        "verdict": verdict, "edge_pp": edge, "bootstrap": {"ci90": [-1.0, 2.0]}, "kept_pct": 60.0,
        "signals": {"baseline": base, "candidate": cand, "rejected": base - cand},
        "candidate": {"mean_net": 1.2}, "baseline": {"mean_net": 0.7}}))


def _env(path, lines):
    Path(path).write_text("\n".join(lines))


def test_settings_keep_gate_names_and_never_secrets():
    with tempfile.TemporaryDirectory() as t:
        env = Path(t) / ".env"
        _env(env, ["# c", "STRUCTURE_MAX_CHASE_ATR=1.5", "export ENTRY_X='2'", "DHAN_ACCESS_TOKEN=abc", "CANDIDATE_API_KEY=zzz",
                   "PS_STRUCTURE_MIN_ROOM_ATR=\"0.3\"", "UNRELATED=1", "STRUCTURE_TOTP_SEED=nope"])
        s = cs.gate_settings(str(env), environ={"CANDIDATE_MIN_CONVICTION": "55", "STRUCTURE_MAX_CHASE_ATR": "9"})
        assert s == {"CANDIDATE_MIN_CONVICTION": "55", "ENTRY_X": "2", "PS_STRUCTURE_MIN_ROOM_ATR": "0.3", "STRUCTURE_MAX_CHASE_ATR": "1.5"}


def test_seed_metrics_and_completeness():
    with tempfile.TemporaryDirectory() as t:
        _ab(t, "7")
        _ab(t, "11", verdict="no_data", base=0, cand=0)
        m = cs.seed_metrics(Path(t))
        assert m["7"]["complete"] is True and m["7"]["edge_pp"] == 0.5 and m["7"]["ci90"] == [-1.0, 2.0]
        assert m["11"]["complete"] is False


def test_compare_reports_setting_changes_and_metric_movement():
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        _ab(t1, "7", edge=0.5, cand=120)
        _ab(t2, "7", edge=1.25, cand=90)
        e1, e2 = Path(t1) / "e", Path(t2) / "e"
        _env(e1, ["STRUCTURE_MAX_CHASE_ATR=1.5", "STRUCTURE_MIN_RR=0"])
        _env(e2, ["STRUCTURE_MAX_CHASE_ATR=1.2", "ENTRY_NEW=1"])
        a = cs.make_snapshot(Path(t1), str(e1), "before", environ={})
        b = cs.make_snapshot(Path(t2), str(e2), "after", environ={})
        d = cs.compare(a, b)
        assert d["settings"]["changed"] == {"STRUCTURE_MAX_CHASE_ATR": ["1.5", "1.2"]}
        assert d["settings"]["added"] == {"ENTRY_NEW": "1"} and d["settings"]["removed"] == {"STRUCTURE_MIN_RR": "0"}
        assert d["seeds"]["7"]["edge_pp"] == [0.5, 1.25, 0.75] and d["seeds"]["7"]["candidate_n"] == [120, 90]
        assert d["warnings"] == []
        md = cs.to_markdown(d)
        assert "changed `STRUCTURE_MAX_CHASE_ATR`: 1.5 -> 1.2" in md and "0.5 -> 1.25 (0.75)" in md


def test_compare_warns_on_different_seeds_incomplete_and_identical_settings():
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        _ab(t1, "7")
        _ab(t2, "13")
        d = cs.compare(cs.make_snapshot(Path(t1), None, "a", environ={}), cs.make_snapshot(Path(t2), None, "b", environ={}))
        w = " ".join(d["warnings"])
        assert "not comparable" in w and "only in the" in w and "identical" in w
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        _ab(t1, "7")
        _ab(t2, "7", verdict="no_data", base=0, cand=0)
        d = cs.compare(cs.make_snapshot(Path(t1), None, "a", environ={}), cs.make_snapshot(Path(t2), None, "b", environ={}))
        assert any("incomplete" in w for w in d["warnings"])


def test_save_and_cli_roundtrip(capsys=None):
    with tempfile.TemporaryDirectory() as t:
        out, store = Path(t) / "out", Path(t) / "store"
        _ab(out, "7")
        env = Path(t) / "e"
        _env(env, ["STRUCTURE_MIN_RR=0"])
        assert cs.main(["snapshot", "--out-dir", str(out), "--env", str(env), "--label", "run one!", "--store", str(store)]) == 0
        files = sorted(store.glob("*.json"))
        assert len(files) == 1 and "run-one" in files[0].name
        assert cs.main(["compare", str(files[0]), str(files[0])]) == 0
        assert cs.main(["list", "--store", str(store)]) == 0
        assert cs.main(["snapshot", "--out-dir", str(Path(t) / "empty"), "--label", "x", "--store", str(store)]) == 1


def test_live_flags_use_effective_values_and_drop_secrets_and_failures():
    doc = {"flags": [{"key": "STRUCTURE_ENABLED", "effective": True}, {"key": "OPENING_PLAN_ENABLED", "effective": 0},
                     {"key": "DHAN_TOKEN_X", "effective": "s"}, {"key": "NONE_VALUE", "effective": None}, {"key": ""}]}
    got = cs.live_flags("real-trade", "http://x:1/", "tok", getter=lambda url, tok: (url, tok) and doc)
    assert got == {"real-trade:STRUCTURE_ENABLED": "True", "real-trade:OPENING_PLAN_ENABLED": "0"}
    seen = []
    cs.live_flags("ps", "http://x:1/", "tok", getter=lambda url, tok: seen.append((url, tok)) or {"flags": []})
    assert seen == [("http://x:1/control/flags", "tok")]
    assert cs.live_flags("ps", "http://x", "t", getter=lambda u, t: (_ for _ in ()).throw(OSError("down"))) == {}


def test_snapshot_merges_flags_and_compare_last_picks_the_two_newest():
    with tempfile.TemporaryDirectory() as t:
        out, store = Path(t) / "out", Path(t) / "store"
        _ab(out, "7", edge=0.5)
        s1 = cs.make_snapshot(out, None, "one", environ={}, flags={"real-trade:A": "1"})
        assert s1["settings"] == {"real-trade:A": "1"}
        cs.save_snapshot(s1, store)
        assert cs.two_newest(store) is None and cs.main(["compare-last", "--store", str(store)]) == 1     # one snapshot only
        import shutil
        shutil.rmtree(out)
        _ab(out, "7", edge=1.5)
        from datetime import datetime, timedelta, timezone
        s2 = cs.make_snapshot(out, None, "two", environ={}, flags={"real-trade:A": "2"}, now=datetime.now(timezone.utc) + timedelta(minutes=5))
        cs.save_snapshot(s2, store)
        old, new = cs.two_newest(store)
        assert old["label"] == "one" and new["label"] == "two"
        assert cs.main(["compare-last", "--store", str(store), "--json", str(Path(t) / "d.json")]) == 0
        d = json.loads((Path(t) / "d.json").read_text())
        assert d["settings"]["changed"] == {"real-trade:A": ["1", "2"]} and d["seeds"]["7"]["edge_pp"] == [0.5, 1.5, 1.0]
