"""E579 round 554: the runner job `drift_monitor` (scripts/drift_monitor.py) - registered, in the Friday pack, read-only, exit 2 reads as "needs a look"."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import jobs as J

CTX = {"python": "python3", "scripts": "/app/scripts", "exp": "/data/runner/exports", "run": "/data/runner/run", "cal": "/data/runner/cal",
       "rt": "http://rt", "ps": "http://ps", "md": "http://md", "gw": "http://gw"}


def test_job_is_registered_light_and_says_it_never_moves_a_switch():
    j = J.JOBS["drift_monitor"]
    assert j["needs"] == ["export_records"] and j["group"] == "weekly" and not j["heavy"] and j["timeout_s"] <= 300
    assert "never moves a switch" in j["after"] and {"mode", "origin", "recent", "min_base"} <= {p["name"] for p in j["params"]}


def test_builder_is_read_only_and_a_drift_alarm_is_attention_not_failure():
    steps = J.JOBS["drift_monitor"]["builder"](CTX, {})
    assert len(steps) == 1 and steps[0]["kind"] == "cmd" and steps[0]["ok"] == [0] and steps[0]["attention"] == [1, 2]
    argv = steps[0]["argv"]
    assert argv[1].endswith("scripts/drift_monitor.py") and argv[-1] == "/data/runner/exports/rt_latest.json"
    assert argv[argv.index("--json") + 1] == "/data/runner/run/drift_monitor.json"
    custom = J.JOBS["drift_monitor"]["builder"](CTX, {"recent": 30, "min_base": 50})[0]["argv"]
    assert custom[custom.index("--recent") + 1] == "30" and custom[custom.index("--min-base") + 1] == "50"


def test_friday_pack_runs_it_after_the_records_are_saved_and_it_writes_only_its_own_json():
    friday = [j for j, _ in J.PACK_MAP["friday_review"]["jobs"]]
    assert friday.index("drift_monitor") > friday.index("export_records")
    steps = J.JOBS["drift_monitor"]["builder"](CTX, {})
    lk = J.derive_locks(J.JOBS["drift_monitor"], steps, {"exp": "/data/runner/exports", "run": "/data/runner/run"})
    assert "exp/rt_latest.json" in lk["shared"] and "run/drift_monitor.json" in lk["excl"] and not lk["solo"]
