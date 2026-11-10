"""Round 290: a time that came in through the environment at boot (the page's guard never saw it) is shown in the pipeline check and the overview, not only in the skip log. No network."""
from __future__ import annotations

import re

from fno import gates
from fno.params import Params


def _p(**kw):
    import config
    return Params.from_config(config).with_(**kw)


def test_a_fitting_setup_is_ok_and_shows_the_window():
    s = gates.settings_step(_p(entry_start="09:30", entry_end="14:30", squareoff_at="15:15"))
    assert s["step"] == "settings fit together" and s["ok"] is True
    assert "09:30-14:30" in s["detail"] and "15:15" in s["detail"]


def test_an_entry_end_after_the_squareoff_is_a_problem_with_the_reason():
    s = gates.settings_step(_p(entry_end="15:20", squareoff_at="15:15"))
    assert s["ok"] is False and "refuses every entry" in s["detail"] and "FNO_ENTRY_END" in s["detail"]


def test_every_reason_is_listed():
    s = gates.settings_step(_p(entry_start="14:50", entry_end="14:40", squareoff_at="14:30"))
    assert s["ok"] is False and s["detail"].count(";") >= 1


def test_a_bad_number_is_shown_too():
    s = gates.settings_step(_p(sl_pct=150.0))
    assert s["ok"] is False and "FNO_SL_PCT" in s["detail"]


def test_it_is_the_same_list_the_engine_uses():
    p = _p(entry_end="15:20", squareoff_at="15:15")
    assert gates.settings_step(p)["detail"].endswith("; ".join(gates.param_problems(p)))
    ok, why = gates.entry_gates(__import__("datetime").datetime(2026, 11, 11, 10, 30), __import__("datetime").date(2026, 11, 12), 14.0, 5.0, gates.DayState(), p)
    assert not ok and any("FNO_ENTRY_END" in w for w in why)


def test_main_wires_it_into_the_pipeline_and_the_overview():
    src = open(__file__.replace("tests/test_round290_settings_step.py", "main.py").replace("tests\\test_round290_settings_step.py", "main.py"), encoding="utf-8").read()
    assert re.search(r"steps\.append\(gates\.settings_step\(liveparams\.current\(\)\)\)", src)
    assert '"param_problems": gates.param_problems(liveparams.current())' in src
    assert re.search(r"from fno import [^\n]*\bgates\b", src)
