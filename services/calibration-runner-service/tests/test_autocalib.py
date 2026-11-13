"""group476: auto-calibration of the switches from test results. Offline, stdlib only. Plain asserts or pytest (python tests/test_autocalib.py)."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
import autocalib as A  # noqa: E402
import jobs as J  # noqa: E402

NOW = 1_760_000_000.0


def row(key, kind="bool", effective="true", looser="off", **kw):
    d = {"key": key, "title": key.title(), "kind": kind, "effective": effective, "looser": looser, "choices": [], "lo": None, "hi": None}
    d.update(kw)
    return d


class FakeControl:
    def __init__(self, flags):
        self.f = {svc: {r["key"]: dict(r) for r in rows} for svc, rows in flags.items()}
        self.sets, self.down, self.fail = [], set(), {}

    def flags(self, svc):
        if svc in self.down:
            raise A.ControlHTTPError(0, "connection refused")
        return {k: dict(v) for k, v in self.f[svc].items()}

    def set(self, svc, key, value, confirm, note):
        if key in self.fail:
            raise A.ControlHTTPError(500, self.fail[key])
        r = self.f[svc][key]
        if A.local_change(r, r["effective"], value) == "LOOSENS" and not confirm:
            raise A.ControlHTTPError(409, {"needs_confirm": True})
        self.sets.append((svc, key, value, confirm, note))
        r["effective"] = value
        return {"ok": True, "applied_live": True, "restart_required": False}


class FakeEvidence:
    def __init__(self):
        self.items = {}

    def put(self, job, run_id, text="", files=None, finished=NOW):
        self.items[job] = A.Ev(job, run_id, finished, text, lambda n, f=files or {}: f.get(n))

    def get(self, job):
        return self.items.get(job)


def make(flags, ev, rules=None, **kw):
    d = tempfile.mkdtemp()
    sent = []
    clock = {"t": NOW}
    ac = A.AutoCalibrator(os.path.join(d, "state", "autocalib.json"), FakeControl(flags), ev, notify=lambda t, m, u: sent.append((t, m, u)),
                          rules=rules or A.RULES, now_fn=lambda: clock["t"], **kw)
    return ac, sent, clock


RT = A.RT
PS = A.PS


# Task 4: the real 52-week rule was removed, so these engine tests use a test-only rule with the same shape (legacy_gate evidence, tighten / loosen by verdict).
TEST_RULE_KEY = "TEST_RULE_KEY"


def r_test_rule(evs, cur, ctx):
    rep = evs["legacy_gate"].json("gate_report.json") or {}
    g = ((rep.get("gates") or {}).get("range_52w_legacy")) or {}
    v = g.get("verdict_robust") or g.get("verdict")
    if v == "hurts":
        return A.Decision("true", "test rule: verdict hurts -> loosen", 2)
    if v == "helps":
        return A.Decision("false", "test rule: verdict helps -> tighten", 1)
    return A.Decision(None, f"test rule verdict is {v or 'missing'}: no change")


TEST_RULES = [A.Rule(TEST_RULE_KEY, A.RT, "Test rule", ("legacy_gate",), "legacy gate test verdict", r_test_rule)]


def legacy(v):
    return {"gates": {"range_52w_legacy": {"verdict": "x", "verdict_robust": v}}}


def test_every_rule_points_at_a_real_switch_and_a_real_test():
    cat = {}
    for svc, path in ((RT, "real-trade-service"), (PS, "position-stocks-service")):
        src = (HERE.parent / path / "control" / "catalog.py").read_text(encoding="utf-8")
        cat[svc] = set(re.findall(r'Flag\("([A-Z0-9_]+)"', src))
    for r in A.RULES:
        assert r.key in cat[r.service], f"{r.key} is not a switch of {r.service}"
        assert r.key not in A.PROTECTED
        for t in r.tests:
            assert t in J.JOBS, f"{r.key}: unknown test {t}"
    assert len({(r.service, r.key) for r in A.RULES}) == len(A.RULES)
    # group490: a protected key is real when it is a control-panel switch OR an env setting the service config really reads
    # (SIZE_LADDER_MAX_LEVEL is a number read in real-trade's config.py, not a catalog Flag); a typo is still caught
    cfg_src = "".join((HERE.parent / path / "config.py").read_text(encoding="utf-8")
                      for path in ("real-trade-service", "position-stocks-service"))
    for k in A.PROTECTED:
        assert (any(k in c for c in cat.values()) or re.search(r'(?:getenv|env_\w+_safe)\(\s*"%s"' % re.escape(k), cfg_src)   # E512: config.py now reads numbers through env_int_safe / env_float_safe (E503)
                or k.endswith("_DEMO_MODE") or k.startswith("PS_")), k


def test_loss_review_guards_are_protected():
    for k in ("STOPOUT_GUARD_ENABLED", "STOPOUT_DAY_PAUSE_N", "STOPOUT_SYMBOL_BLOCK", "ENTRY_MIN_QTY", "STOP_OVERSHOOT_ALLOWANCE_PCT", "ORB_MODE"):
        assert k in A.PROTECTED and not any(r.key == k for r in A.RULES)


def test_a_protected_switch_can_never_get_a_rule():
    bad = A.Rule("BROKER_STOP_ENABLED", RT, "x", ("legacy_gate",), "x", r_test_rule)
    try:
        A.AutoCalibrator(os.path.join(tempfile.mkdtemp(), "a.json"), FakeControl({}), FakeEvidence(), rules=[bad])
        raise AssertionError("accepted a protected rule")
    except ValueError:
        pass


def test_tightening_applies_at_once_and_sends_one_telegram():
    ev = FakeEvidence()
    ev.put("legacy_gate", "run1", files={"gate_report.json": legacy("helps")})
    ac, sent, _ = make({RT: [row("TEST_RULE_KEY", effective="true", looser="on")], PS: []}, ev,
                       rules=TEST_RULES)
    rep = ac.evaluate("t")
    assert rep["changed"] == 1 and ac.control.sets[0][:3] == (RT, "TEST_RULE_KEY", "false") and ac.control.sets[0][4].startswith("autocalib:")
    assert len(sent) == 1 and "TEST_RULE_KEY: true → false" in sent[0][1] and "legacy_gate" in sent[0][1]
    ac.evaluate("t")                                            # already at the value: nothing more, no second message
    assert len(sent) == 1 and len(ac.control.sets) == 1


def test_loosening_needs_two_different_test_runs():
    ev = FakeEvidence()
    ev.put("legacy_gate", "run1", files={"gate_report.json": legacy("hurts")})
    ac, sent, clock = make({RT: [row("TEST_RULE_KEY", effective="false", looser="on")], PS: []}, ev,
                            rules=TEST_RULES)
    r1 = ac.evaluate("t")
    assert r1["rows"][0]["status"] == "waiting" and not ac.control.sets and not sent
    ac.evaluate("t")                                            # same run again must not count twice
    assert not ac.control.sets
    ev.put("legacy_gate", "run2", files={"gate_report.json": legacy("hurts")}, finished=NOW + 7 * 86400)
    r3 = ac.evaluate("t")
    assert r3["rows"][0]["status"] == "changed" and ac.control.sets[0][2:4] == ("true", True) and len(sent) == 1
    assert "LOOSENS" in sent[0][1]


def test_a_disagreeing_run_resets_the_count():
    ev = FakeEvidence()
    ac, _, _ = make({RT: [row("TEST_RULE_KEY", effective="false", looser="on")], PS: []}, ev, rules=TEST_RULES)
    ev.put("legacy_gate", "run1", files={"gate_report.json": legacy("hurts")})
    ac.evaluate("t")
    ev.put("legacy_gate", "run2", files={"gate_report.json": legacy("no_edge")})
    ac.evaluate("t")
    assert "TEST_RULE_KEY" not in ac.state["pending"]
    ev.put("legacy_gate", "run3", files={"gate_report.json": legacy("hurts")})
    assert ac.evaluate("t")["rows"][0]["status"] == "waiting" and not ac.control.sets


def test_no_evidence_or_unclear_verdict_changes_nothing():
    ev = FakeEvidence()
    ac, sent, _ = make({RT: [row("TEST_RULE_KEY", effective="false", looser="on")], PS: []}, ev, rules=TEST_RULES)
    assert ac.evaluate("t")["rows"][0]["status"] == "no_evidence"
    ev.put("legacy_gate", "r", files={"gate_report.json": legacy("insufficient_sample")})
    assert ac.evaluate("t")["rows"][0]["status"] == "hold"
    ev.put("legacy_gate", "r2", files={})                                   # unreadable report
    assert ac.evaluate("t")["rows"][0]["status"] == "hold" and not ac.control.sets and not sent


def test_numbers_move_in_steps_and_stay_in_range():
    pack = ("A/B verdicts: pass, pass, pass  -> all `pass`.\n- chase: SUGGEST 0.5 (OOS edge 0.4 pp on 300 passed / 200 rejected; fold picks [0.5]) -> real-trade: STRUCTURE_MAX_CHASE_ATR=0.5 (now 1.5)\n"
            "- stretch: unstable (OOS edge 1)\n")
    ev = FakeEvidence()
    ev.put("calibration_pack", "p1", pack)
    rules = [r for r in A.RULES if r.key in ("STRUCTURE_MAX_CHASE_ATR", "STRUCTURE_MAX_STRETCH_ATR")]
    ac, sent, _ = make({RT: [row("STRUCTURE_MAX_CHASE_ATR", "float", "1.5", "higher", lo=0, hi=10), row("STRUCTURE_MAX_STRETCH_ATR", "float", "3", "higher", lo=0, hi=12)], PS: []}, ev, rules=rules)
    rep = ac.evaluate("t")
    # 1.5 -> 0.5 is a TIGHTENING (lower chase limit); step limit 25 % of 1.5 = 0.375 -> 1.12 (rounded)
    assert ac.control.sets == [(RT, "STRUCTURE_MAX_CHASE_ATR", "1.12", False, ac.control.sets[0][4])]
    assert [r["status"] for r in rep["rows"]] == ["changed", "hold"] and len(sent) == 1


def test_walk_forward_lines_are_ignored_unless_every_ab_verdict_passes():
    pack = "A/B verdicts: pass, fail  -> not all `pass`: treat every line below as a hypothesis.\n- chase: SUGGEST 0.5 (x)\n"
    ev = FakeEvidence()
    ev.put("calibration_pack", "p1", pack)
    ac, _, _ = make({RT: [row("STRUCTURE_MAX_CHASE_ATR", "float", "1.5", "higher", lo=0, hi=10)], PS: []}, ev, rules=[r for r in A.RULES if r.key == "STRUCTURE_MAX_CHASE_ATR"])
    assert ac.evaluate("t")["rows"][0]["status"] == "hold" and not ac.control.sets


def test_master_switch_dry_run_pin_and_cooldown():
    ev = FakeEvidence()
    ev.put("legacy_gate", "run1", files={"gate_report.json": legacy("helps")})
    rules = TEST_RULES
    fl = {RT: [row("TEST_RULE_KEY", effective="true", looser="on")], PS: []}
    ac, sent, clock = make(fl, ev, rules=rules, enabled=False)
    assert ac.evaluate("t")["ran"] is False and not ac.control.sets
    f = ac.evaluate("t", force=True)                                         # forced while off = report only
    assert f["dry_run"] is True and f["rows"][0]["status"] == "would_change" and not ac.control.sets
    ac.configure(enabled=True, dry_run=True)
    assert ac.evaluate("t")["rows"][0]["status"] == "would_change" and not ac.control.sets
    ac.configure(dry_run=False, pinned=["TEST_RULE_KEY"])
    assert ac.evaluate("t")["rows"][0]["status"] == "pinned" and not ac.control.sets
    ac.configure(pinned=[])
    assert ac.evaluate("t")["changed"] == 1
    ac.control.f[RT]["TEST_RULE_KEY"]["effective"] = "true"         # somebody flipped it back by hand
    ev.put("legacy_gate", "run2", files={"gate_report.json": legacy("helps")})
    assert ac.evaluate("t")["rows"][0]["status"] == "cooldown" and len(ac.control.sets) == 1
    clock["t"] += 25 * 3600
    assert ac.evaluate("t")["changed"] == 1
    try:
        ac.configure(pinned=["BROKER_STOP_ENABLED"])
        raise AssertionError("pinned an unknown switch")
    except ValueError:
        pass
    # settings survive a restart
    ac2 = A.AutoCalibrator(ac.path, ac.control, ev, rules=rules)
    assert ac2.settings()["enabled"] is True and ac2.state["history"]


def test_service_down_and_apply_errors_are_reported_once():
    ev = FakeEvidence()
    ev.put("legacy_gate", "run1", files={"gate_report.json": legacy("helps")})
    rules = TEST_RULES
    ac, sent, _ = make({RT: [row("TEST_RULE_KEY", effective="true", looser="on")], PS: []}, ev, rules=rules)
    ac.control.down.add(RT)
    assert ac.evaluate("t")["rows"][0]["status"] == "service_unreachable" and not sent
    ac.control.down.clear()
    ac.control.fail["TEST_RULE_KEY"] = "HTTP 500 boom"
    assert ac.evaluate("t")["rows"][0]["status"] == "error" and len(sent) == 1 and "Could not change" in sent[0][1]
    ac.evaluate("t")
    assert len(sent) == 1                                                    # same error is not repeated every evaluation


def test_decision_helpers_for_every_test():
    sec = lambda t: A.Ev("x", "r", NOW, t)  # noqa: E731
    k = A.r_kronos({"kronos_backtest": sec("VERDICT: VETO HELPS: vetoed rows are clearly worse")}, "log", {})
    assert (k.value, k.confirms) == ("enforce", 2)
    assert A.r_kronos({"kronos_backtest": sec("VERDICT: NOT PROVEN: x")}, "enforce", {}).value == "log"
    assert A.r_kronos({"kronos_backtest": sec("VERDICT: NOT PROVEN: x")}, "off", {}).value is None
    assert A.r_tsfm({"tsfm_combine": sec('# hdr\n{\n "gate": "A",\n "status": "PASS"\n}')}, "off", {}).value == "log"
    assert A.r_tsfm({"tsfm_combine": sec('{"status": "FAIL"}')}, "log", {}).value == "off"
    assert A.r_tsfm({"tsfm_combine": sec("SKIPPED: not running")}, "off", {}).value is None
    g = sec("Market-structure promotion gates\n\nG2: FAIL\n   - x\nG3: PASS\n   - y\nG4: NOT_ENOUGH_DATA\n")
    assert A.r_g3_stop({"gate_check": g}, "off", {}).value == "on" and A.r_g2_gate({"gate_check": g}, "enforce", {}).value is None
    assert A.r_g2_exit({"gate_check": g}, "exit", {}).value == "tighten" and A.r_g2_exit({"gate_check": g}, "tighten", {}).value is None and A.r_g2_exit({"gate_check": g}, "log", {}).value is None
    ok = sec("G2: PASS\nG3: FAIL\n")
    assert A.r_g2_gate({"gate_check": ok}, "off", {}).value == "enforce" and A.r_g3_stop({"gate_check": ok}, "on", {}).value == "off"
    hb = lambda s: A.r_overnight({"hold_bucket": sec("VERDICT: " + s)}, "true", {})  # noqa: E731
    assert hb("1 day vs same day: no clear difference (+0.1 pp); 2-5 days vs same day: worse (-1.2 pp)").value == "false"
    assert hb("2-5 days vs same day: better (+1.2 pp, CI 0.1..2.0)").value is None          # never switched ON by a report (group 470 default is off)
    assert hb("NO VERDICT: every bucket has fewer than 20 closed trades").value is None
    ig = A.r_intraday("vwap_gate", "VWAP gate")
    assert ig({"intraday_gates": A.Ev("i", "r", NOW, "", lambda n: {"vwap_gate": {"verdict": "hurts"}})}, "true", {}).value == "false"
    assert ig({"intraday_gates": A.Ev("i", "r", NOW, "", lambda n: {"vwap_gate": {"verdict": "insufficient_sample"}})}, "true", {}).value is None


def test_setup_backtest_job_is_registered_and_in_the_monthly_pack():
    j = J.JOBS["setup_backtest"]
    assert j["heavy"] is True and j["window"] == "closed" and any(p["name"] == "days" for p in j["params"]), j
    argv = j["builder"]({"python": "py", "scripts": "/s", "bars": "/data/bars", "run": "/r"}, {})[0]["argv"]
    assert argv[1].endswith("setup_backtest.py") and "/data/bars" in argv and "/r/setup_backtest" in argv
    assert ["setup_backtest", {}] in J.PACK_MAP["monthly_evidence"]["jobs"]
    assert "setup_backtest" in A.EVIDENCE_JOBS


def test_setup_rules_only_ever_switch_a_setup_off():
    rules = [r for r in A.RULES if r.key.startswith("SETUP_")]
    assert {(r.key, r.service) for r in rules} == {("SETUP_S2_ENABLED", PS), ("SETUP_S3_ENABLED", PS)}   # the replay is intraday: never evidence for real-trade's multi-day holds
    ev = lambda v: {"setup_backtest": A.Ev("b", "r", NOW, "", lambda n: {"S2_RS_BREAKOUT_LONG": {"verdict": v}})}   # noqa: E731
    f = A.r_setup("S2_RS_BREAKOUT_LONG", "S2 long")
    d = f(ev("no_edge"), "true", {})
    assert (d.value, d.confirms) == ("false", 2)
    assert f(ev("no_edge"), "false", {}).value is None                     # already off
    for v in ("edge", "unclear", "insufficient_sample", None):
        assert f(ev(v), "true", {}).value is None and f(ev(v), "false", {}).value is None   # a report never switches a setup ON
    assert f({"setup_backtest": A.Ev("b", "r", NOW, "")}, "true", {}).value is None         # no file = no change


def test_setup_rule_reads_what_setup_backtest_really_writes():
    import importlib.util
    spec = importlib.util.spec_from_file_location("sb", str(HERE.parent.parent / "scripts" / "setup_backtest.py"))
    sb = importlib.util.module_from_spec(spec); spec.loader.exec_module(sb)
    trades = [{"setup": sb.se.S2, "side": "LONG", "date": f"2026-10-{d:02d}", "net": -1.0 - i * 0.01} for d in range(1, 9) for i in range(5)]
    rep = sb.summarize(trades, min_n=30, min_days=5, n_boot=300)
    assert rep["S2_RS_BREAKOUT_LONG"]["verdict"] == "no_edge", rep
    d = A.r_setup("S2_RS_BREAKOUT_LONG", "S2 long")({"setup_backtest": A.Ev("b", "r", NOW, "", lambda n: json.loads(json.dumps(rep)))}, "true", {})
    assert d.value == "false"


def test_section_splits_a_pack_output():
    t = "# pack\n\n=====\n## JOB Hold  (hold_bucket)\n=====\nVERDICT: a\n\n=====\n## JOB Kronos  (kronos_backtest)\n=====\nVERDICT: b\n"
    assert "VERDICT: a" in A.section(t, "hold_bucket") and "VERDICT: b" not in A.section(t, "hold_bucket")
    assert A.section(t, "nope") == "" and A.section("plain output", "x") == "plain output"


class FakeRunner:
    def __init__(self, runs, base):
        self.runs, self.base = runs, base

    def latest(self, target):
        return self.runs.get(target)

    def files(self, rid):
        out = []
        for root, _, names in os.walk(os.path.join(self.base, rid)):
            out += [{"name": os.path.relpath(os.path.join(root, n), os.path.join(self.base, rid))} for n in names]
        return out

    def file_path(self, rid, name):
        p = os.path.join(self.base, rid, name)
        return p if os.path.isfile(p) else None


def test_runner_evidence_reads_job_and_pack_runs_and_ignores_failed_or_old():
    base = tempfile.mkdtemp()
    os.makedirs(os.path.join(base, "p1", "legacy"))
    json.dump(legacy("helps"), open(os.path.join(base, "p1", "legacy", "gate_report.json"), "w"))
    open(os.path.join(base, "p1", "output.txt"), "w").write("## JOB Legacy  (legacy_gate)\nok\n")
    iso = lambda t: __import__("datetime").datetime.fromtimestamp(t, A.IST).isoformat()  # noqa: E731
    runs = {"monthly_evidence": {"id": "p1", "kind": "pack", "status": "attention", "finished_at": iso(NOW - 3600),
                                 "jobs": [{"id": "legacy_gate", "status": "success"}, {"id": "kronos_backtest", "status": "failed"}]}}
    ev = A.RunnerEvidence(FakeRunner(runs, base), J.PACK_MAP, 40 * 86400, now_fn=lambda: NOW)
    e = ev.get("legacy_gate")
    assert e and e.run_id == "p1" and e.json("gate_report.json")["gates"]["range_52w_legacy"]["verdict_robust"] == "helps" and "ok" in e.text
    assert ev.get("kronos_backtest") is None                                  # that job failed inside the pack
    assert ev.get("hold_bucket") is None                                      # never run
    old = A.RunnerEvidence(FakeRunner(runs, base), J.PACK_MAP, 60, now_fn=lambda: NOW)
    assert old.get("legacy_gate") is None                                     # too old to be evidence
    runs["legacy_gate"] = {"id": "p1", "kind": "job", "status": "timeout", "finished_at": iso(NOW)}
    assert ev.get("legacy_gate").run_id == "p1"                               # a timed-out job run is skipped, the pack result is used


def test_on_run_finished_only_reacts_to_evidence_runs():
    ev = FakeEvidence()
    ac, _, _ = make({RT: [], PS: []}, ev)
    assert ac.on_run_finished({"status": "success", "kind": "job", "target": "services_health"}, J.PACK_MAP) is None
    assert ac.on_run_finished({"status": "failed", "kind": "job", "target": "kronos_backtest"}, J.PACK_MAP) is None
    assert ac.on_run_finished({"status": "success", "kind": "job", "target": "kronos_backtest"}, J.PACK_MAP)["ran"] is True
    assert ac.on_run_finished({"status": "success", "kind": "pack", "target": "monthly_evidence"}, J.PACK_MAP)["ran"] is True
    assert ac.on_run_finished({"status": "success", "kind": "pack", "target": "monday_open"}, J.PACK_MAP) is None


def test_coverage_lists_automated_protected_and_manual():
    ev = FakeEvidence()
    fl = {RT: [row("KRONOS_GATE_MODE", "enum", "off"), row("BROKER_STOP_ENABLED"), row("ENTRY_VWAP_TOLERANCE_PCT", "float", "0.15", "higher")], PS: []}
    ac, _, _ = make(fl, ev)
    c = ac.coverage()
    st = {x["key"]: x["status"] for x in c["switches"]}
    assert st == {"KRONOS_GATE_MODE": "automated", "BROKER_STOP_ENABLED": "protected", "ENTRY_VWAP_TOLERANCE_PCT": "manual"}
    assert c["counts"] == {"automated": 1, "protected": 1, "manual": 1}
    ac.control.down.add(PS)
    assert ac.coverage()["unreachable"][0]["service"] == PS


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
