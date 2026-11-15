"""Round 551 (E576): the agent reads the real-trade evidence report (aggregates only, context only). Offline, stdlib only.
Plain asserts or pytest (python tests/test_round551_book_evidence.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import agent as G  # noqa: E402
import autocalib as A  # noqa: E402
from test_agent import NOW, FakeEvidence, ev, gemini_ok, make_agent, make_llm  # noqa: E402

REPORT = {
    "book": {"n": 84, "net": 12345.6, "charges": 999.0, "charges_pct_of_gross": 18.2, "win_pct": 51.0, "profit_factor": 1.2, "mean_net_ret_pct": 0.21, "lower90_pct": -0.05,
             "dsr_prob": 0.62, "verdict": "NOT_PROVEN", "net_if_charges_x2": 5000.0, "max_drawdown": 3000.0},
    "weakest": [{"group": "tier_name", "key": "tier_c", "n": 33, "net": -800.0, "mean_net_ret_pct": -0.4, "lower90_pct": -0.9, "verdict": "NOT_PROVEN"},
                {"group": "source_tab", "key": "balance sheet pick", "n": 31, "net": -10.0, "mean_net_ret_pct": -0.1, "lower90_pct": -0.5, "verdict": "NOT_PROVEN"}],
    "verdict_counts": {"NOT_PROVEN": 5, "NOT_ENOUGH_DATA": 7},
}


def _auto_with(report, age_days=1.0):
    old = NOW - 3 * 86400
    items = {"calibration_pack": ev("calibration_pack", old, "A/B verdicts: seeds 7, 11, 13 all `pass`.\n- chase: SUGGEST 1.2\n")}
    if report is not None:
        items[G.BOOK_JOB] = ev(G.BOOK_JOB, NOW - age_days * 86400, files={"evidence_report.json": report})
    return FakeEvidence(items)


def test_fresh_report_becomes_aggregates_only():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm, evidence=_auto_with(REPORT))
    b = G.book_evidence(ag.auto, NOW)
    assert b["status"] == "ok" and b["trades"] == 84 and b["verdict"] == "NOT_PROVEN" and b["lower90_pct"] == -0.05 and b["dsr_prob"] == 0.62
    flat = json.dumps(b)
    for rupee_field in ("12345", "net_if_charges", "max_drawdown", "profit_factor", "charges\""):
        assert rupee_field not in flat, rupee_field
    assert [w["key"] for w in b["weakest"]] == ["tier_c", "(hidden)"], "a group name that would trip the privacy guard is hidden, not sent"


def test_missing_stale_and_unreadable_are_statuses_never_errors():
    llm, _ = make_llm([])
    ag, _ = make_agent(llm, evidence=_auto_with(None))
    assert G.book_evidence(ag.auto, NOW)["status"] == "none"
    ag, _ = make_agent(llm, evidence=_auto_with(REPORT, age_days=20))
    assert G.book_evidence(ag.auto, NOW) == {"status": "stale", "age_days": 20.0}
    ag, _ = make_agent(llm, evidence=_auto_with({"oops": 1}))
    assert G.book_evidence(ag.auto, NOW)["status"] == "unreadable"
    ag, _ = make_agent(llm, evidence=_auto_with({"book": {"verdict": "<script>", "n": "x", "mean_net_ret_pct": float("nan")}}))
    b = G.book_evidence(ag.auto, NOW)
    assert b["verdict"] == "UNKNOWN" and b["trades"] == 0 and b["mean_net_ret_pct"] is None

    class Boom:
        def get(self, job):
            raise RuntimeError("down")
    ag, _ = make_agent(llm, evidence=Boom())
    assert G.book_evidence(ag.auto, NOW)["status"] == "none"


def test_only_a_fresh_readable_report_reaches_the_whitelist():
    ok = {"date": "d", "switches": [], "book_evidence": {"status": "ok", "trades": 5, "verdict": "NEGATIVE", "secret_extra": "x", "net": 99}}
    assert G.whitelist(ok)["book_evidence"] == {"status": "ok", "trades": 5, "verdict": "NEGATIVE"}
    for st in ("none", "stale", "unreadable"):
        assert "book_evidence" not in G.whitelist({"date": "d", "switches": [], "book_evidence": {"status": st}})
    assert "book_evidence" not in G.whitelist({"date": "d", "switches": []})


def test_the_prompt_carries_the_block_and_passes_the_privacy_guard():
    llm, _ = make_llm([gemini_ok({"ideas": []})])
    ag, d = make_agent(llm, evidence=_auto_with(REPORT))
    rep = ag.run("report", trigger="manual")
    assert llm.post.calls, "the model was asked"
    sent = json.dumps(llm.post.calls)
    assert rep["book_evidence"]["status"] == "ok" and "Real trades: 84 closed, verdict NOT_PROVEN" in rep["book_evidence"]["text"]
    assert not any("privacy guard" in h for h in rep["health"])
    assert "book_evidence\\\":{" in sent and "lower90_pct" in sent
    assert "12345.6" not in sent and "12346" not in sent, "no rupee figure is sent"
    assert "book_evidence" in ag.status()["last_report"]


def test_the_report_says_so_when_there_is_no_evidence_yet_and_nothing_is_sent():
    llm, _ = make_llm([gemini_ok({"ideas": []})])
    ag, d = make_agent(llm, evidence=_auto_with(None))
    rep = ag.run("report", trigger="manual")
    assert llm.post.calls, "the model was asked"
    assert rep["book_evidence"]["status"] == "none" and "No real-trade evidence report yet" in rep["book_evidence"]["text"]
    assert "book_evidence\\\":{" not in json.dumps(llm.post.calls) and "lower90_pct" not in json.dumps(llm.post.calls)


def test_book_evidence_never_adds_a_switch_or_changes_the_allow_list():
    llm, _ = make_llm([gemini_ok({"ideas": [{"switch": "BROKER_STOP_ENABLED", "direction": "down", "why": "book is NEGATIVE"}]})])
    ag, d = make_agent(llm, evidence=_auto_with({**REPORT, "book": {**REPORT["book"], "verdict": "NEGATIVE"}}))
    rep = ag.run("report", trigger="manual")
    assert rep["ideas"] == [] and any(x.get("reason") == "not in the allow-list" for x in rep["dropped"])
    assert not set(ag.rules) & set(A.PROTECTED)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
