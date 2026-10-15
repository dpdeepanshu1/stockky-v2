"""Tests for scripts/board_parity_check.py (plain asserts; also collected by pytest)."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import board_parity_check as b  # noqa: E402


def _status(verdict="parity_ok", compared=500, days=4, fallbacks=0, active=True, **ab_over):
    ab = {"verdict": verdict, "compared": compared, "min_sample": 200, "engine_added": 0, "swings_differ": 0, "errors": 0, "last_error": None,
          "engine_dropped": 3, "status_changed": 7, "bounds_changed": 0, "other_changed": 0, "cap_knockon": 2, "served_differs": 2,
          "days": {f"2026-10-{10 + i}": {"compared": 100, "identical": 90} for i in range(days)}}
    ab.update(ab_over)
    return {"enabled": True, "ms_engine": {"requested": active, "vendored": True, "active": active, "engine_version": "1.0.0", "engine_calls": compared,
                                           "legacy_calls": 0, "fallbacks": fallbacks, "last_error": None, "ab": ab}}


def test_go_when_parity_ok_over_enough_comparisons_and_days():
    r = b.evaluate(_status())
    assert r["verdict"] == "GO" and r["numbers"]["compared"] == 500 and r["numbers"]["days"] == 4
    assert r["numbers"]["documented_differences"]["status_changed"] == 7   # shown, not a blocker (the documented one-bar status difference)


def test_accepts_the_bare_ms_engine_block():
    assert b.evaluate(_status()["ms_engine"])["verdict"] == "GO"


def test_not_enough_data_cases():
    assert b.evaluate(_status("insufficient_sample", compared=120))["verdict"] == "NOT_ENOUGH_DATA"
    one_day = b.evaluate(_status(days=1))
    assert one_day["verdict"] == "NOT_ENOUGH_DATA" and "IST day" in one_day["reasons"][0]
    assert b.evaluate(_status(compared=250), min_compared=400)["verdict"] == "NOT_ENOUGH_DATA"
    assert b.evaluate(_status("disabled", compared=0))["verdict"] == "NOT_ENOUGH_DATA"


def test_no_go_cases():
    r = b.evaluate(_status("contract_break", engine_added=2))
    assert r["verdict"] == "NO_GO" and "contract_break" in r["reasons"][0]
    fb = b.evaluate(_status(fallbacks=3))
    assert fb["verdict"] == "NO_GO" and "fell back" in fb["reasons"][0]
    off = b.evaluate(_status(active=False))
    assert off["verdict"] == "NO_GO" and "not serving" in off["reasons"][0]


def test_no_go_beats_not_enough_data():
    assert b.evaluate(_status("insufficient_sample", compared=5, fallbacks=1))["verdict"] == "NO_GO"


def test_no_input():
    assert b.evaluate({})["verdict"] == "NO_INPUT"
    assert b.evaluate(None)["verdict"] == "NO_INPUT"
    assert b.evaluate({"ms_engine": {"active": True}})["verdict"] == "NO_INPUT"


def test_cli_exit_codes_and_file_input():
    with tempfile.TemporaryDirectory() as d:
        ok, bad, few = (os.path.join(d, n) for n in ("ok.json", "bad.json", "few.json"))
        json.dump(_status(), open(ok, "w"))
        json.dump(_status("contract_break"), open(bad, "w"))
        json.dump(_status("insufficient_sample", compared=10), open(few, "w"))
        assert b.main(["--file", ok]) == 0
        assert b.main(["--file", bad]) == 1
        assert b.main(["--file", few]) == 0
        assert b.main(["--file", few, "--strict"]) == 1
        assert b.main(["--file", os.path.join(d, "missing.json")]) == 0
        assert b.main(["--file", os.path.join(d, "missing.json"), "--strict"]) == 1
        assert b.main([]) == 0 and b.main(["--strict"]) == 1


def test_render_mentions_verdict_and_next_step():
    txt = b.render(b.evaluate(_status()))
    assert txt.startswith("Board parity (legacy removal): GO") and "compat_board" in txt
