"""Round 555 (E580): the agent reads the strategy registry and drift (aggregates only, context only). Offline, stdlib only.
Plain asserts or pytest (python tests/test_round555_strategy_status.py)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tests"))
import agent as G  # noqa: E402
import autocalib as A  # noqa: E402
from test_agent import NOW, gemini_ok, make_agent, make_llm  # noqa: E402


def iso(age_days=0.5):
    return datetime.fromtimestamp(NOW - age_days * 86400, timezone.utc).isoformat(timespec="seconds")


def strat(status="testing", drift=None, verdict="NOT_PROVEN", n=44, **extra):
    s = {"status": status, "last": {"n": n, "verdict": verdict, "mean_net_ret_pct": 0.3, "lower90_pct": -0.1, "dsr_prob": 0.4, "win_pct": 51.0, "net": 98765.4},
         "streak": {"verdict": verdict, "days": 2, "last_day": "2026-11-14"}, "history": [{"date": "2026-11-01", "from": "testing", "to": status, "why": "x"}], "first_seen": "2026-11-01"}
    if drift:
        s["drift"] = {"state": drift, "z": -3.1, "day": "2026-11-14"}
    s.update(extra)
    return s


def put(d, strategies, updated=None):
    os.makedirs(os.path.join(d, "state"), exist_ok=True)
    json.dump({"version": 1, "strategies": strategies, "updated": updated or iso()}, open(os.path.join(d, "state", "strategy_registry.json"), "w"))


def test_a_fresh_registry_becomes_counts_and_a_short_attention_list(tmp_path):
    d = str(tmp_path)
    put(d, {"swing | BUY NOW": strat("proven", "STABLE", "POSITIVE"), "volume_shock | WATCH": strat("demoted", "DECAY", "NEGATIVE", n=61), "intraday | X": strat("proven", "WARNING"),
            "gap | Y": strat("testing", "STABLE"), "old | Z": strat("retired")})
    r = G.strategy_status(d, NOW)
    assert r["status"] == "ok" and r["counts"] == {"testing": 1, "proven": 2, "watch": 0, "demoted": 1, "retired": 1}
    assert [a["strategy"] for a in r["attention"]] == ["volume_shock | WATCH", "intraday | X"], "demoted first, then a proven one with a drift warning; steady ones are not listed"
    assert r["attention"][0] == {"strategy": "volume_shock | WATCH", "status": "demoted", "drift": "DECAY", "verdict": "NEGATIVE", "n": 61}
    flat = json.dumps(r)
    for bad in ("98765", "dsr_prob", "history", "streak", "mean_net_ret_pct"):
        assert bad not in flat, bad
    assert "2 proven" in G.strategy_line(r) and "2 need(s) a look" in G.strategy_line(r)


def test_at_most_six_rows_and_names_that_trip_the_privacy_guard_are_hidden(tmp_path):
    d = str(tmp_path)
    rows = {f"tab{i} | L": strat("watch") for i in range(9)}
    rows["balance sheet | pick"] = strat("demoted")
    put(d, rows)
    r = G.strategy_status(d, NOW)
    assert len(r["attention"]) == 6 and r["attention"][0]["strategy"] == "(hidden)" and r["counts"]["watch"] == 9


def test_missing_stale_and_unreadable_are_statuses_never_errors(tmp_path):
    d = str(tmp_path)
    assert G.strategy_status(d, NOW) == {"status": "none"}
    put(d, {"a | b": strat("watch")}, updated=iso(20))
    assert G.strategy_status(d, NOW) == {"status": "stale", "age_days": 20.0}
    put(d, {"a | b": strat("watch")}, updated="not a date")
    assert G.strategy_status(d, NOW)["status"] == "unreadable"
    open(os.path.join(d, "state", "strategy_registry.json"), "w").write("{broken")
    assert G.strategy_status(d, NOW) == {"status": "none"}
    put(d, {"a | b": "garbage", "c | d": {"status": "bogus", "drift": "nope", "last": "x"}, "e | f": {"status": "watch", "drift": {"state": "alien"}, "last": {"verdict": "<b>", "n": "x"}}})
    r = G.strategy_status(d, NOW)
    assert r["status"] == "ok" and r["counts"]["testing"] == 1 and r["counts"]["watch"] == 1
    assert r["attention"] == [{"strategy": "e | f", "status": "watch", "drift": None, "verdict": None, "n": 0}]
    assert "No strategy registry yet" in G.strategy_line({"status": "none"}) and "not used" in G.strategy_line({"status": "stale", "age_days": 9})


def test_only_a_fresh_readable_registry_reaches_the_whitelist():
    ok = {"date": "d", "switches": [], "strategy_status": {"status": "ok", "age_days": 1.0, "counts": {"proven": 1}, "secret": "x",
                                                          "attention": [{"strategy": "a | b", "status": "watch", "drift": "DECAY", "verdict": "NEGATIVE", "n": 5, "net": 99}]}}
    w = G.whitelist(ok)["strategy_status"]
    assert w == {"status": "ok", "age_days": 1.0, "counts": {"proven": 1}, "attention": [{"strategy": "a | b", "status": "watch", "drift": "DECAY", "verdict": "NEGATIVE", "n": 5}]}
    for st in ("none", "stale", "unreadable"):
        assert "strategy_status" not in G.whitelist({"date": "d", "switches": [], "strategy_status": {"status": st}})


def test_the_prompt_carries_the_block_and_nothing_else_from_the_file():
    llm, _ = make_llm([gemini_ok({"ideas": []})])
    ag, d = make_agent(llm)
    put(d, {"volume_shock | WATCH": strat("demoted", "DECAY", "NEGATIVE", n=61)})
    rep = ag.run("report", trigger="manual")
    sent = json.dumps(llm.post.calls)
    assert rep["strategy_status"]["status"] == "ok" and "1 demoted" in rep["strategy_status"]["text"] and not any("privacy guard" in h for h in rep["health"])
    assert 'strategy_status\\":{' in sent and "volume_shock | WATCH" in sent and "98765" not in sent
    assert "strategy_status" in ag.status()["last_report"]


def test_no_registry_means_nothing_is_sent_and_the_report_says_so():
    llm, _ = make_llm([gemini_ok({"ideas": []})])
    ag, d = make_agent(llm)
    rep = ag.run("report", trigger="manual")
    assert rep["strategy_status"]["status"] == "none" and "No strategy registry yet" in rep["strategy_status"]["text"]
    assert 'strategy_status\\":{' not in json.dumps(llm.post.calls)


def test_telegram_message_names_strategies_that_need_a_look():
    sent = []
    llm, _ = make_llm([gemini_ok({"ideas": []})])
    ag, d = make_agent(llm, notify=lambda t, m, u="normal": sent.append(m) or "sent")
    put(d, {"volume_shock | WATCH": strat("demoted", "DECAY", "NEGATIVE", n=61)})
    ag.run("report", trigger="manual")
    body = "\n".join(sent)
    assert "Strategy registry" in body and "volume_shock | WATCH: demoted, drift DECAY" in body


def test_it_never_adds_a_switch_or_changes_the_allow_list():
    llm, _ = make_llm([gemini_ok({"ideas": [{"switch": "SHORT_MODE", "direction": "down", "why": "a strategy is demoted"}]})])
    ag, d = make_agent(llm)
    put(d, {"x | y": strat("demoted", "DECAY", "NEGATIVE")})
    rep = ag.run("report", trigger="manual")
    assert rep["ideas"] == [] and any(x.get("reason") == "not in the allow-list" for x in rep["dropped"])
    assert not set(ag.rules) & set(A.PROTECTED)


if __name__ == "__main__":
    import tempfile
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(Path(tempfile.mkdtemp())) if fn.__code__.co_argcount else fn()
            n += 1
            print("ok", name)
    print(n, "passed")
