"""Round 556 (E581): Agent.evidence_view() behind GET /agent/evidence (the Self Improve > Evidence panel). Offline, stdlib only.
Plain asserts or pytest (python tests/test_round556_evidence_view.py)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import agent as G  # noqa: E402
from test_agent import NOW, make_agent, make_llm  # noqa: E402
from test_round551_book_evidence import REPORT, _auto_with  # noqa: E402
from test_round555_strategy_status import put, strat  # noqa: E402


def test_view_carries_both_blocks_with_a_sentence_and_no_rupee_field():
    llm, _ = make_llm([])
    ag, d = make_agent(llm, evidence=_auto_with(REPORT))
    put(d, {"volume_shock | WATCH": strat("demoted", "DECAY", "NEGATIVE", n=61), "a | b": strat("proven", "STABLE", "POSITIVE")})
    v = ag.evidence_view()
    assert v["book_evidence"]["status"] == "ok" and "Real trades: 84 closed" in v["book_evidence"]["text"]
    assert v["strategy_status"]["status"] == "ok" and v["strategy_status"]["counts"]["demoted"] == 1 and "1 need(s) a look" in v["strategy_status"]["text"]
    flat = json.dumps(v)
    for bad in ("12345", "98765", "max_drawdown", "net_if_charges", "history", "streak"):
        assert bad not in flat, bad
    assert "(hidden)" in [w["key"] for w in v["book_evidence"]["weakest"]], "names still go through the privacy filter"


def test_nothing_yet_is_a_status_with_a_sentence_never_an_error():
    llm, _ = make_llm([])
    ag, d = make_agent(llm)
    v = ag.evidence_view()
    assert v["book_evidence"]["status"] == "none" and "No real-trade evidence report yet" in v["book_evidence"]["text"]
    assert v["strategy_status"]["status"] == "none" and "No strategy registry yet" in v["strategy_status"]["text"]


def test_a_part_that_raises_becomes_unreadable_and_the_other_part_still_shows(monkeypatch=None):
    llm, _ = make_llm([])
    ag, d = make_agent(llm, evidence=_auto_with(REPORT))
    real = G.strategy_status
    G.strategy_status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        v = ag.evidence_view()
    finally:
        G.strategy_status = real
    assert v["strategy_status"]["status"] == "unreadable" and "could not be read" in v["strategy_status"]["text"]
    assert v["book_evidence"]["status"] == "ok"


def test_it_reads_only_and_a_stale_registry_is_labelled_stale():
    llm, _ = make_llm([])
    ag, d = make_agent(llm)
    from test_round555_strategy_status import iso
    put(d, {"a | b": strat("watch")}, updated=iso(20))
    v = ag.evidence_view()
    assert v["strategy_status"]["status"] == "stale" and "not used" in v["strategy_status"]["text"]


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
