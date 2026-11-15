"""E578 round 553: the runner job `strategy_registry` (scripts/strategy_registry.py) - registered, in the daily pack only, keeps its own state, moves no switch."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs as J

CTX = {"python": "python3", "scripts": "/app/scripts", "exp": "/data/runner/exports", "run": "/data/runner/run", "cal": "/data/runner/cal",
       "rt": "http://rt", "ps": "http://ps", "md": "http://md", "gw": "http://gw"}


def test_job_is_registered_light_and_says_it_never_moves_a_switch():
    j = J.JOBS["strategy_registry"]
    assert j["needs"] == ["export_records"] and j["group"] == "daily" and not j["heavy"] and j["timeout_s"] <= 300
    assert "never moves a switch" in j["after"] and "YOU decide" in j["after"]
    assert {"mode", "origin", "min_n", "since"} <= {p["name"] for p in j["params"]}


def test_builder_makes_its_own_evidence_then_updates_the_state_file():
    steps = J.JOBS["strategy_registry"]["builder"](CTX, {})
    assert len(steps) == 3 and all(s["kind"] == "cmd" and s["ok"] == [0] for s in steps)                       # round 554: evidence, drift, registry
    assert steps[0]["attention"] == [1] and steps[1]["attention"] == [1, 2] and steps[2]["attention"] == [1]
    a, b = steps[0]["argv"], steps[2]["argv"]
    assert a[1].endswith("scripts/evidence_report.py") and a[a.index("--json") + 1] == "/data/runner/run/strategy_evidence.json"
    assert a[a.index("--trials-file") + 1] == "/data/runner/agent/trials.json" and a[-1] == "/data/runner/exports/rt_latest.json"
    assert b[1].endswith("scripts/strategy_registry.py")
    assert b[b.index("--evidence") + 1] == "/data/runner/run/strategy_evidence.json" and b[b.index("--drift") + 1] == "/data/runner/run/strategy_drift.json"
    d = steps[1]["argv"]
    assert d[1].endswith("scripts/drift_monitor.py") and d[d.index("--json") + 1] == "/data/runner/run/strategy_drift.json" and d[-1] == "/data/runner/exports/rt_latest.json"
    assert b[b.index("--store") + 1] == "/data/runner/state/strategy_registry.json"
    assert b[b.index("--json") + 1] == "/data/runner/run/strategy_registry.json"
    with_since = J.JOBS["strategy_registry"]["builder"](CTX, {"since": "2026-11-01", "min_n": 50})[0]["argv"]
    assert with_since[with_since.index("--since") + 1] == "2026-11-01" and with_since[with_since.index("--min-n") + 1] == "50"


def test_scripts_exist_in_the_folder_the_image_copies():
    here = os.path.dirname(os.path.abspath(__file__))
    for f in ("strategy_registry.py", "evidence_report.py", "drift_monitor.py"):
        assert os.path.isfile(os.path.join(here, "..", "..", "..", "scripts", f))


def test_daily_pack_runs_it_after_the_evidence_report_and_the_friday_pack_does_not():
    daily = [j for j, _ in J.PACK_MAP["daily_close"]["jobs"]]
    friday = [j for j, _ in J.PACK_MAP["friday_review"]["jobs"]]
    assert daily.index("strategy_registry") == daily.index("evidence_report") + 1
    assert "strategy_registry" not in friday, "streaks count days: one run a day, from the daily pack"


def test_it_writes_only_its_own_run_files_and_reads_the_saved_records():
    steps = J.JOBS["strategy_registry"]["builder"](CTX, {})
    lk = J.derive_locks(J.JOBS["strategy_registry"], steps, {"exp": "/data/runner/exports", "run": "/data/runner/run"})
    assert "exp/rt_latest.json" in lk["shared"] and "run/strategy_registry.json" in lk["excl"] and "run/strategy_evidence.json" in lk["excl"] and not lk["solo"]
