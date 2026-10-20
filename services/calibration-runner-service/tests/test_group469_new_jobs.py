"""group469: the new Real Money Calibration jobs (entry slippage review, calibration snapshot / compare) and their packs. Offline, stdlib only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import jobs as J  # noqa: E402

SCRIPTS = HERE.parent.parent / "scripts"
CTX = dict(python=sys.executable, scripts=str(SCRIPTS), bars="/nonexistent-bars", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw",
           analysis="http://an", decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t", run="/r", cal="/c", exp="/e")


def _steps(jid, params=None):
    j = J.JOBS[jid]
    return j["builder"](CTX, J.coerce_params(j, params or {}))


def test_new_jobs_exist_in_the_right_groups_and_packs():
    for jid in ("entry_slippage", "calibration_snapshot", "calibration_compare"):
        assert jid in J.JOBS and J.JOBS[jid]["group"] == "weekly", jid
    friday = [j for j, _ in J.PACK_MAP["friday_review"]["jobs"]]
    weekly = [j for j, _ in J.PACK_MAP["weekly_calibration"]["jobs"]]
    assert "entry_slippage" in friday and friday.index("export_records") < friday.index("entry_slippage")     # needs the saved records first
    assert weekly[-2:] == ["calibration_snapshot", "calibration_compare"] and weekly.index("calibration_status") < weekly.index("calibration_snapshot")


def test_entry_slippage_step_reads_the_saved_records_and_writes_json_to_the_run():
    (st,) = _steps("entry_slippage", {"min_n": 40})
    a = st["argv"]
    assert a[1].endswith("entry_slippage_review.py") and "--min-n" in a and a[a.index("--min-n") + 1] == "40"
    assert a[-1] == os.path.join("/e", "rt_latest.json") and os.path.join("/r", "entry_slippage.json") in a
    assert st["attention"] == [1] and "TOKEN" not in st["env"]


def test_snapshot_step_gets_the_token_from_env_never_argv_and_both_service_urls():
    (st,) = _steps("calibration_snapshot", {"label": "before-change"})
    a = st["argv"]
    assert a[2] == "snapshot" and a[a.index("--label") + 1] == "before-change"
    assert a[a.index("--rt-url") + 1] == "http://rt" and a[a.index("--ps-url") + 1] == "http://ps"
    assert a[a.index("--store") + 1] == os.path.join("/c", "snapshots") and st["env"] == {"TOKEN": "{token}"}
    assert not any("tok" in x.lower() and x != "--token" for x in a if x.startswith("{"))
    (cmp_,) = _steps("calibration_compare")
    assert cmp_["argv"][2] == "compare-last" and "snapshot_compare.json" in " ".join(cmp_["argv"])


def test_flags_are_accepted_by_the_scripts_help_and_steps_serialise():
    for jid in ("entry_slippage", "calibration_snapshot", "calibration_compare"):
        for st in _steps(jid):
            json.dumps(st)
            assert all(isinstance(x, str) for x in st["argv"])
            helptext = subprocess.run([sys.executable, st["argv"][1], "--help"], capture_output=True, text=True, timeout=60).stdout
            for x in st["argv"][2:]:
                if x.startswith("--"):
                    assert x in helptext, (jid, x)


def test_end_to_end_the_scripts_run_the_way_the_runner_calls_them():
    with tempfile.TemporaryDirectory() as t:
        exp, cal, run = Path(t) / "exp", Path(t) / "cal", Path(t) / "run"
        for d in (exp, cal / "ab_7", run):
            d.mkdir(parents=True)
        (cal / "ab_7" / "ab_report.json").write_text(json.dumps({"verdict": "no_edge", "edge_pp": 0.4, "bootstrap": {"ci90": [-1, 2]}, "kept_pct": 60,
                                                                 "signals": {"baseline": 100, "candidate": 60, "rejected": 40}}))
        recs = [{"mode": "REAL", "position_id": i, "entry_slippage_pct": 0.05, "entry_price": 100.0, "quantity": 10, "gross_pnl": 50.0, "entry_hour_ist": "10",
                 "source_tab": "x", "origin": "system", "net_pnl": 40.0, "opened_at": "2026-10-0%d" % (1 + i % 5)} for i in range(40)]
        (exp / "rt_latest.json").write_text(json.dumps({"records": recs}))
        ctx = dict(CTX, run=str(run), cal=str(cal), exp=str(exp))
        for jid in ("entry_slippage", "calibration_snapshot", "calibration_snapshot", "calibration_compare"):
            j = J.JOBS[jid]
            (st,) = j["builder"](ctx, J.coerce_params(j, {}))
            argv = [x for x in st["argv"]]
            if jid == "calibration_snapshot":                       # no live services here: drop the URLs, keep the rest
                for flag in ("--rt-url", "--ps-url"):
                    i = argv.index(flag)
                    del argv[i:i + 2]
            r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            assert r.returncode in st["ok"] + st["attention"], (jid, r.returncode, r.stdout[-300:], r.stderr[-300:])
        assert len(list((cal / "snapshots").glob("*.json"))) >= 1          # the snapshot job wrote its file (a second one in the same second reuses the name)
        # one snapshot only: compare-last must say so and ask for attention (exit 1), never crash
        (cmp_,) = J.JOBS["calibration_compare"]["builder"](ctx, {})
        assert subprocess.run(cmp_["argv"], capture_output=True, text=True, timeout=60).returncode in (0, 1)
