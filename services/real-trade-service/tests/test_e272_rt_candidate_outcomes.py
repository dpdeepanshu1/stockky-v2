"""E272 round 8: Real Trade candidate outcomes (entry_engine/candidate_outcomes.py), the wanted-symbols list, and the position-stocks recorder reading it.
Plain-function runner: python3 tests/test_e272_rt_candidate_outcomes.py (also collected by pytest). No sqlalchemy needed: the module is loaded by path, the position-stocks recorder is
loaded by path, files go to a temp directory, wiring is checked by source text."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(ROOT))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


co = _load("rt_co", os.path.join(ROOT, "entry_engine", "candidate_outcomes.py"))
rec = _load("ps_rec", os.path.join(REPO, "services", "position-stocks-service", "feed", "bar_recorder.py"))
T0 = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc).timestamp()      # 09:30 IST
DAY = "2026-11-02"


def bars(start, closes, step=60):
    return [[start + i * step, c, c * 1.002, c * 0.998, c, 1000] for i, c in enumerate(closes)]


def _write_bars(root, sym, closes):
    d = os.path.join(root, DAY)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{sym}.json"), "w") as f:
        json.dump({"bars": bars(T0 - 60, closes)}, f)


def test_label_is_long_forward_move_and_never_guesses():
    r = co.label(bars(T0, [100.0] + [101.0] * 70), T0)
    assert r["status"] == "ok" and r["r5"] == 1.0 and r["r60"] == 1.0
    assert co.label(None, T0) == {"status": "no_bars"}
    assert co.label(bars(T0, [100.0] * 12), T0)["r60"] is None


def test_label_rows_dedupes_and_counts_no_bars():
    rows = [{"symbol": "AAA", "decision": "SKIPPED", "reason": "WAIT: cost", "created_ts": T0, "side": "LONG"},
            {"symbol": "AAA", "decision": "SKIPPED", "reason": "WAIT: cost", "created_ts": T0 + 30},          # same 5-minute bucket
            {"symbol": "BBB", "decision": "ENTERED", "reason": "", "created_ts": T0}]
    res = co.label_rows(rows, lambda s, d: bars(T0, [100.0] * 70) if s == "AAA" else None)
    assert res["counts"] == {"rows": 3, "labelled": 1, "no_bars": 1, "deduped": 1}


def test_write_wanted_is_a_capped_union_and_writes_only_when_it_grows():
    with tempfile.TemporaryDirectory() as d:
        co._wanted_seen.clear()
        p = co.write_wanted(d, DAY, ["aaa", "BBB.NS", "", None])
        assert p and co.read_wanted(d, DAY) == ["AAA", "BBB"]
        assert co.write_wanted(d, DAY, ["AAA"]) is None                         # nothing new: no write
        co.write_wanted(d, DAY, ["CCC"])
        assert co.read_wanted(d, DAY) == ["AAA", "BBB", "CCC"]
        co._wanted_seen.clear()                                                  # a restart re-reads the file, nothing is lost
        co.write_wanted(d, DAY, ["DDD"])
        assert co.read_wanted(d, DAY) == ["AAA", "BBB", "CCC", "DDD"]
        co._wanted_seen.clear()
        assert len(co.write_wanted(d, "2026-11-03", [f"S{i}" for i in range(50)], cap=10) and co.read_wanted(d, "2026-11-03")) == 10
        assert os.listdir(d) == ["wanted"]                                       # only its own subfolder


def test_write_wanted_never_raises_on_an_unwritable_dir():
    co._wanted_seen.clear()
    assert co.write_wanted("/proc/nonexistent/x", DAY, ["AAA"]) is None


def test_run_day_rt_reads_ps_bars_and_writes_only_under_the_rt_dir():
    with tempfile.TemporaryDirectory() as d:
        rt = os.path.join(d, "rt")
        _write_bars(d, "AAA", [100.0] * 5 + [102.0] * 70)
        rows = [{"symbol": "AAA", "decision": "SKIPPED", "reason": "x", "created_ts": T0, "side": "LONG"}]
        out = co.run_day_rt(rows, d, rt, DAY)
        assert out["labelled"] == 1 and out["path"] == os.path.join(rt, "outcomes", f"{DAY}.json")
        assert not os.path.exists(os.path.join(d, "outcomes"))                   # position-stocks' folder untouched


def test_note_candidates_respects_the_switch():
    with tempfile.TemporaryDirectory() as d:
        cfg = types.ModuleType("config")
        cfg.RT_BAR_WANTED_ENABLED, cfg.BAR_RECORDS_DIR, cfg.RT_BAR_WANTED_MAX = False, d, 400
        sys.modules["config"] = cfg
        try:
            co._wanted_seen.clear()
            c = types.SimpleNamespace(symbol="AAA")
            assert co.note_candidates([c], T0) is None
            cfg.RT_BAR_WANTED_ENABLED = True
            assert co.note_candidates([c], T0) and co.read_wanted(d, DAY) == ["AAA"]
        finally:
            sys.modules.pop("config", None)


def test_ps_recorder_notes_the_wanted_list_without_deadlock():
    with tempfile.TemporaryDirectory() as d:
        co._wanted_seen.clear()
        co.write_wanted(os.path.join(d, "rt"), DAY, ["WANTA", "WANTB"])
        assert rec.read_wanted(d, DAY) == ["WANTA", "WANTB"] and rec.read_wanted(d, "2026-11-09") == []
        cfg = types.ModuleType("config")
        cfg.BAR_RECORD_DIR, cfg.BAR_RECORD_MAX_SYMBOLS, cfg.BAR_WANTED_ENABLED = d, 1, True
        sys.modules["config"] = cfg
        try:
            rec.reset()
            dn = int((T0 + rec.IST_OFFSET_S) // 86400)
            store = {s: [[T0 - 60 * (30 - i), 10.0, 11.0, 9.0, 10.0, 5] for i in range(30)] for s in ("TOP", "WANTA", "WANTB")}
            did = rec.run_once(T0, True, bars_fn=lambda s: store[s], symbols_fn=lambda: list(store), day_fn=lambda: dn)
            assert did["wrote"] == 3 and rec.stats()["wanted_read"] == 2
            names = sorted(os.listdir(os.path.join(d, DAY)))
            assert names == ["TOP.json", "WANTA.json", "WANTB.json"]
            cfg.BAR_WANTED_ENABLED = False
            rec.reset()
            os.remove(os.path.join(d, DAY, "WANTB.json")); os.remove(os.path.join(d, DAY, "WANTA.json"))
            rec.run_once(T0, True, bars_fn=lambda s: store[s], symbols_fn=lambda: list(store), day_fn=lambda: dn)
            assert sorted(os.listdir(os.path.join(d, DAY))) == ["TOP.json"]      # switch off: back to the turnover cut
        finally:
            sys.modules.pop("config", None)


def test_wiring_by_source_text():
    ent = open(os.path.join(ROOT, "entry_engine", "entry.py"), encoding="utf-8").read()
    assert "note_candidates(candidates)" in ent
    cfg = open(os.path.join(ROOT, "config.py"), encoding="utf-8").read()
    assert 'RT_BAR_WANTED_ENABLED = _bs_bool("RT_BAR_WANTED_ENABLED", "true")' in cfg
    cat = open(os.path.join(ROOT, "control", "catalog.py"), encoding="utf-8").read()
    assert 'Flag("RT_BAR_WANTED_ENABLED"' in cat
    pcat = open(os.path.join(REPO, "services", "position-stocks-service", "control", "catalog.py"), encoding="utf-8").read()
    assert 'Flag("BAR_WANTED_ENABLED"' in pcat
    assert os.path.exists(os.path.join(REPO, "scripts", "rt_candidate_outcomes_job.py"))


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"{n} passed")
