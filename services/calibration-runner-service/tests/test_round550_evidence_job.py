"""E575 round 550: the runner job `evidence_report` (scripts/evidence_report.py) - registered, wired into the daily and Friday packs, read-only."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs as J

CTX = {"python": "python3", "scripts": "/app/scripts", "exp": "/data/runner/exports", "run": "/data/runner/run", "cal": "/data/runner/cal",
       "rt": "http://rt", "ps": "http://ps", "md": "http://md", "gw": "http://gw"}


def test_job_is_registered_and_needs_the_saved_records():
    j = J.JOBS["evidence_report"]
    assert j["needs"] == ["export_records"] and j["group"] == "daily" and not j["heavy"] and j["timeout_s"] <= 300
    assert "never moves a switch" in j["after"]
    names = {p["name"] for p in j["params"]}
    assert {"mode", "origin", "min_n", "since"} <= names


def test_builder_is_read_only_and_points_at_the_agent_trial_count():
    steps = J.JOBS["evidence_report"]["builder"](CTX, {})
    assert len(steps) == 1 and steps[0]["kind"] == "cmd"
    argv = steps[0]["argv"]
    assert argv[1].endswith("scripts/evidence_report.py")
    assert argv[argv.index("--trials-file") + 1] == "/data/runner/agent/trials.json"
    assert argv[argv.index("--json") + 1] == "/data/runner/run/evidence_report.json"
    assert argv[-1] == "/data/runner/exports/rt_latest.json"
    assert "--since" not in argv
    assert steps[0]["attention"] == [1] and steps[0]["ok"] == [0]            # "no usable trades" is "needs a look", never a crash
    with_since = J.JOBS["evidence_report"]["builder"](CTX, {"since": "2026-11-01", "min_n": 50, "mode": "DEMO"})[0]["argv"]
    assert with_since[with_since.index("--since") + 1] == "2026-11-01" and with_since[with_since.index("--min-n") + 1] == "50" and "DEMO" in with_since


def test_script_exists_in_the_scripts_folder_the_image_copies():
    here = os.path.dirname(os.path.abspath(__file__))
    assert os.path.isfile(os.path.join(here, "..", "..", "..", "scripts", "evidence_report.py"))


def test_packs_run_it_right_after_the_records_are_saved():
    daily = [j for j, _ in J.PACK_MAP["daily_close"]["jobs"]]
    friday = [j for j, _ in J.PACK_MAP["friday_review"]["jobs"]]
    assert daily.index("evidence_report") == daily.index("export_records") + 1
    assert friday.index("evidence_report") > friday.index("export_records")


def test_it_only_reads_the_exports_and_writes_its_own_json():
    steps = J.JOBS["evidence_report"]["builder"](CTX, {})
    lk = J.derive_locks(J.JOBS["evidence_report"], steps, {"exp": "/data/runner/exports", "run": "/data/runner/run"})
    assert "exp/rt_latest.json" in lk["shared"] and "run/evidence_report.json" in lk["excl"] and not lk["solo"]
