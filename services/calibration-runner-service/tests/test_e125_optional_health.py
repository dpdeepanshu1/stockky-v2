"""E125: an optional service that is switched off (kronos / timesfm docker profile) is 'skipped', not a failed step. Offline, stdlib only."""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import jobs as J  # noqa: E402
import test_runner_engine as T  # noqa: E402


def test_services_health_marks_only_kronos_and_timesfm_optional():
    steps = J.b_services_health(T._ctx("/x"), {})
    opt = {s["title"] for s in steps if s.get("optional")}
    assert opt == {"health: kronos", "health: timesfm"}
    assert all(s.get("soft") for s in steps)


def test_no_answer_from_optional_step_is_skipped_but_core_stays_failed():
    r, d, sc = T._runner({})
    r.jobs = {"o": T._job("o", lambda c, p: [J._http("opt", "GET", "http://127.0.0.1:1/x") | {"optional": True}]),
              "c": T._job("c", lambda c, p: [J._http("core", "GET", "http://127.0.0.1:1/x") | {"soft": True}])}
    ro, rc = r.submit("job", "o"), r.submit("job", "c")
    assert r.drain(60)
    so = r.index[ro.d["id"]].d
    assert so["status"] == "success" and so["steps"][0]["status"] == "success" and "skipped" in so["steps"][0]["detail"]
    sc_ = r.index[rc.d["id"]].d
    assert sc_["steps"][0]["status"] == "failed"      # a core service with no answer is still reported red
    txt = open(os.path.join(d, "data", "runs", ro.d["id"], "output.txt")).read()
    assert "optional service" in txt
    r.shutdown()
