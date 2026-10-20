"""group470: fixes found by reading the 19-20 Oct job outputs. Offline, stdlib only.

  * the token printed by vm_calibrate.sh (`--token <JWT>`) was not redacted
  * calibration_pack and gate_check failed (exit 2) because `--ab a b c` is not how an append-style flag is repeated
  * calibration_snapshot / the checkpointed jobs reported "input missing" for the folder they themselves create
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import jobs as J  # noqa: E402
import runner as R  # noqa: E402

TOKEN = "tok-ABCDEF-1234567890"
JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJjYWxpYnJhdGlvbi1ydW5uZXIiLCJyb2xlIjoiYWRtaW4ifQ.abcDEF123_-xyzSIGNATURE456"


def _runner(cal: str):
    d = tempfile.mkdtemp()
    sc = os.path.join(d, "scripts")
    os.makedirs(sc)
    ctx = dict(python=sys.executable, scripts=sc, bars="/nonexistent-bars", rt="http://rt", ps="http://ps", md="http://md", gw="http://gw", analysis="http://an",
               decision="http://dp", notify="http://n", kronos="http://k", tsfm="http://t", run=os.path.join(d, "run"), cal=cal, exp=os.path.join(d, "exp"))
    return R.Runner(os.path.join(d, "data"), sc, ctx, J.JOB_LIST, {}, lambda: TOKEN, nice=0, preflight_s=0), ctx


def test_redactor_hides_a_jwt_however_it_reaches_the_log():
    red = R.Redactor(lambda: [TOKEN])               # the runner only knows its own token, not the one vm_calibrate.sh minted
    for line in (f"--token {JWT}", f"--token={JWT}", f"+ python3 x.py --url u --token {JWT} --flag", f"PS_TOKEN={JWT}", f"TOKEN={JWT}", f"leaked {JWT} here",
                 f"Authorization: Bearer {JWT}", f"--token {TOKEN}"):
        out = red(line)
        assert "eyJ" not in out and "SIGNATURE456" not in out and TOKEN not in out, (line, out)
    assert red("--token") == "--token" and red("no secrets here: 12345") == "no secrets here: 12345"
    assert red("--tokens-per-minute 5000") == "--tokens-per-minute 5000"          # an unrelated flag that merely starts the same


def _make_ab(cal: str, *seeds: int):
    for s in seeds:
        d = os.path.join(cal, f"ab_{s}")
        os.makedirs(d)
        Path(d, "ab_report.json").write_text("{}")


def test_gate_check_and_calibration_pack_repeat_the_ab_flag_once_per_seed():
    cal = tempfile.mkdtemp()
    Path(cal, "backtest.json").write_text("{}")
    _make_ab(cal, 7, 11, 13)
    r, ctx = _runner(cal)
    for jid in ("gate_check", "calibration_pack"):
        steps = J.JOBS[jid]["builder"](ctx, J.coerce_params(J.JOBS[jid], {}))
        st = [s for s in steps if s["kind"] == "cmd"][-1]
        argv, why = r._expand(st, ctx)
        assert argv is not None, (jid, why)
        i = [k for k, a in enumerate(argv) if a == "--ab"]
        assert len(i) == 3, (jid, argv)
        assert [argv[k + 1] for k in i] == sorted(os.path.join(cal, f"ab_{s}", "ab_report.json") for s in (7, 11, 13)), argv
        assert all(not a.startswith("globflag:") for a in argv)


def test_ab_flag_with_no_report_is_still_a_missing_input_not_a_usage_error():
    cal = tempfile.mkdtemp()
    Path(cal, "backtest.json").write_text("{}")
    r, ctx = _runner(cal)
    st = [s for s in J.JOBS["calibration_pack"]["builder"](ctx, {}) if s["kind"] == "cmd"][-1]
    argv, why = r._expand(st, ctx)
    assert argv is None and why.startswith("input missing") and "ab_report.json" in why


def test_the_folder_a_job_creates_is_not_reported_as_missing_input():
    cal = tempfile.mkdtemp()                      # no snapshots/ and no ckpt/ yet: the first ever run
    r, ctx = _runner(cal)
    for jid, params in (("calibration_snapshot", {"label": "weekly"}), ("legacy_gate", {}), ("entry_rules", {}), ("p8_retest", {}), ("ml_v2", {})):
        st = [s for s in J.JOBS[jid]["builder"](ctx, J.coerce_params(J.JOBS[jid], params)) if s["kind"] == "cmd"][-1]
        # the history folder is a real input: give the job one so that only the checkpoint / store path is in question
        for a in st["argv"]:
            if a.startswith(os.path.join(cal, "hist_")):
                os.makedirs(a, exist_ok=True)
                Path(a, "X.json").write_text("{}")
        argv, why = r._expand(st, ctx)
        assert argv is not None, (jid, why)


def test_a_missing_real_input_is_still_reported():
    cal = tempfile.mkdtemp()
    r, ctx = _runner(cal)
    st = [s for s in J.JOBS["legacy_gate"]["builder"](ctx, {}) if s["kind"] == "cmd"][-1]
    argv, why = r._expand(st, ctx)
    assert argv is None and "hist_7" in why


def test_g1_budget_is_the_same_everywhere():
    assert J.JOBS["g1_check"]["builder"](dict(python="p", scripts="/s", bars="/b", run="/r", cal="/c", exp="/e"), {})[0]["argv"].count("15") == 1
    sh = (HERE.parent.parent / "scripts" / "vm_calibrate.sh").read_text()
    assert 'G1_BUDGET_MS:-15' in sh
