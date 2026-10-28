"""E108 (T6): scripts/dhan_sell_probe.py (offline, fake endpoint) plus the runner job wiring. The job is the ONLY one that places a real order, so these tests pin
that nothing is sent without the explicit tick, that the confirm phrase matches the service's, and that the job cannot be run outside market hours."""
from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO / "scripts"))

import jobs as J  # noqa: E402
import dhan_sell_probe as S  # noqa: E402

RESULT = {"verdict": "PASS", "line": "SELL PROBE: PASS - clean round trip on ITC", "steps": ["SELL 1 ITC ref 100.00"], "rules": ["The stop leg rests at the broker"]}


class Fake:
    def __init__(self, code=200, doc=None):
        self.code, self.doc, self.calls = code, RESULT if doc is None else doc, []

    def __call__(self, method, url, body=None):
        self.calls.append((method, url, body))
        return self.code, self.doc


def test_without_yes_nothing_is_sent():
    f = Fake()
    rc, out = S.run(f, "http://ps:8006", yes=False)
    assert rc == 0 and f.calls == [] and "Nothing was sent" in out and out.rstrip().endswith("SELL PROBE: SKIPPED - not confirmed")


def test_with_yes_it_posts_the_confirm_phrase_and_prints_steps_rules_and_the_verdict():
    f = Fake()
    rc, out = S.run(f, "http://ps:8006/", yes=True, symbol="ITC")
    assert rc == 0 and f.calls == [("POST", "http://ps:8006/short/probe", {"symbol": "ITC", "confirm": "PLACE_ONE_REAL_SELL_ORDER"})]
    assert "step  SELL 1 ITC" in out and "dhan  The stop leg" in out and out.rstrip().endswith("SELL PROBE: PASS - clean round trip on ITC")


def test_fail_exits_one_and_unusable_answers_are_failures_not_passes():
    assert S.run(Fake(doc={"verdict": "FAIL", "line": "SELL PROBE: FAIL - Dhan refused"}), "http://x", yes=True)[0] == 1
    assert S.run(Fake(code=500, doc="boom"), "http://x", yes=True)[0] == 1
    rc, out = S.run(Fake(code=0, doc="ConnectError"), "http://x", yes=True)
    assert rc == 1 and "CHECK DHAN" in out, "a lost answer after a possible order must tell you to look at Dhan"
    assert S.run(Fake(code=401, doc=None), "http://x", yes=True)[0] == 1
    assert S.run(Fake(doc={"nothing": 1}), "http://x", yes=True)[0] == 1
    assert S.run(Fake(doc={"verdict": "SKIPPED", "line": "SELL PROBE: SKIPPED - outside the probe window"}), "http://x", yes=True)[0] == 0


def test_status_reads_only():
    f = Fake()
    rc, out = S.run(f, "http://ps", yes=False, status_only=True)
    assert rc == 0 and f.calls == [("GET", "http://ps/short/probe", None)]


def test_confirm_phrase_is_the_same_as_the_services():
    svc = (REPO / "services" / "position-stocks-service" / "orders" / "sell_probe.py").read_text(encoding="utf-8")
    m = re.search(r'CONFIRM_PHRASE = "([A-Z_]+)"', svc)
    assert m and m.group(1) == S.CONFIRM_PHRASE


def _ctx():
    return {"python": "py", "scripts": "/s", "ps": "http://ps:8006", "rt": "http://rt", "md": "http://md", "gw": "http://gw"}


def test_job_sends_nothing_unless_the_box_is_ticked():
    argv = J.b_sell_probe(_ctx(), {})[0]["argv"]
    assert "--yes" not in argv and argv[1].endswith("dhan_sell_probe.py")
    assert "--yes" in J.b_sell_probe(_ctx(), {"yes": True, "symbol": "ntpc"})[0]["argv"]
    a = J.b_sell_probe(_ctx(), {"yes": True, "symbol": "ntpc"})[0]["argv"]
    assert a[a.index("--symbol") + 1] == "NTPC"
    st = J.b_sell_probe(_ctx(), {"status_only": True, "yes": True})[0]["argv"]
    assert "--status" in st and "--yes" not in st, "status_only wins: reading never places"


def test_job_is_registered_market_hours_only_and_carries_the_token():
    j = J.JOBS["dhan_sell_probe"]
    assert j["window"] == "open" and j["group"] == "ops" and not j["heavy"]
    assert any(p["name"] == "yes" and p["kind"] == "bool" and p["default"] is False for p in j["params"])
    assert J.b_sell_probe(_ctx(), {})[0]["env"] == {"TOKEN": "{token}"}


def test_the_probe_is_in_no_pack_and_no_schedule():
    for pk in J.PACKS:
        assert all(jid != "dhan_sell_probe" for jid, _ in pk["jobs"]), f"pack {pk['id']} would place a real order unattended"
    sched = (HERE / "scheduler.py").read_text(encoding="utf-8")
    assert "dhan_sell_probe" not in sched and "sell_probe" not in sched


def test_a_fail_prints_what_dhan_answered_and_a_pass_does_not():
    """E242: on a FAIL the compact Dhan rows are printed (raw lines) so a field mismatch can be read off the run; a PASS stays short."""
    failed = {"verdict": "FAIL", "line": "SELL PROBE: FAIL - x", "steps": [], "rules": [], "answers": {"row_after_fill": {"orderStatus": "PENDING"}, "short_after_cancel": 1}}
    rc, out = S.render(failed)
    assert rc == 1 and 'raw   row_after_fill: {"orderStatus": "PENDING"}' in out and "raw   short_after_cancel: 1" in out and out.rstrip().endswith("SELL PROBE: FAIL - x")
    assert "raw " not in S.render(dict(RESULT, answers={"row_after_fill": {}}))[1]
