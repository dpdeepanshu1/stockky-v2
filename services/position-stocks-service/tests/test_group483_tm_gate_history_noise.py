"""E285 round 130: the Time Machine gate's real-history vote needs more than the history's own noise (standard error of its average net R), not a fixed -0.10 R."""
import math
from pathlib import Path

import pytest

from timemachine import engine as E


class _Rule:
    def be_win(self): return 0.34
    def be_win_net(self): return 0.36


def _hist(n, avg, sd):
    return {"n": n, "target": 3, "p_target": 0.2, "avg_r_net": avg, "sd_r_net": sd}


def _sim(avg):
    return {"n": 3000, "p_target": 0.25, "avg_r_net": avg}


def test_margin_is_fixed_without_the_switch_or_without_spread():
    assert E.hist_margin(_hist(15, -0.2, 1.3), 0.0) == E.MARGIN_R_HIST
    assert E.hist_margin({"n": 15, "avg_r_net": -0.2}, 1.0) == E.MARGIN_R_HIST
    assert E.hist_margin({"n": 1, "sd_r_net": 1.0}, 1.0) == E.MARGIN_R_HIST


def test_margin_is_its_own_standard_error():
    assert E.hist_margin(_hist(16, -0.2, 1.2), 1.0) == pytest.approx(1.2 / math.sqrt(16))        # 0.30
    assert E.hist_margin(_hist(400, -0.2, 1.2), 1.0) == pytest.approx(E.MARGIN_R_HIST)           # a long steady history keeps the fixed band
    assert E.hist_margin(_hist(16, -0.2, 1.2), 2.0) == pytest.approx(0.6)


def test_noisy_negative_history_no_longer_says_against():
    v_old = E.verdict(_sim(-0.12), _hist(16, -0.2, 1.2), _Rule(), 20)
    assert v_old["code"] == "AGAINST"                                                    # old behaviour: the default is unchanged
    v = E.verdict(_sim(-0.12), _hist(16, -0.2, 1.2), _Rule(), 20, hist_se_mult=1.0)
    assert v["code"] == "MIXED" and v["hist_margin_r"] == pytest.approx(0.30)
    assert "noisy" in v["line"]


def test_a_clearly_negative_history_still_says_against():
    v = E.verdict(_sim(-0.12), _hist(16, -0.55, 1.2), _Rule(), 20, hist_se_mult=1.0)
    assert v["code"] == "AGAINST"


def test_clearly_positive_history_can_still_favour():
    assert E.verdict(_sim(0.2), _hist(16, 0.6, 1.2), _Rule(), 20, hist_se_mult=1.0)["code"] == "FAVOURS"


def test_history_under_ten_trades_still_does_not_vote():
    v = E.verdict(_sim(-0.2), _hist(6, -0.9, 1.2), _Rule(), 20, hist_se_mult=1.0)
    assert v["code"] == "AGAINST" and "does not vote" in v["line"]                          # unchanged: the simulation alone decides


def test_summarize_rows_reports_the_spread():
    rows = [{"outcome": "target", "day": "d1", "r_gross": 2.0, "r_net": 1.9, "net_pct": 1.0},
            {"outcome": "stop", "day": "d2", "r_gross": -1.0, "r_net": -1.1, "net_pct": -1.0},
            {"outcome": "flat", "day": "d3", "r_gross": 0.0, "r_net": -0.1, "net_pct": 0.0}]
    s = E.summarize_rows(rows, _Rule())
    mean = (1.9 - 1.1 - 0.1) / 3
    assert s["sd_r_net"] == pytest.approx(math.sqrt(((1.9 - mean) ** 2 + (-1.1 - mean) ** 2 + (-0.1 - mean) ** 2) / 2))
    assert E.summarize_rows(rows[:1], _Rule())["sd_r_net"] is None


def test_gate_passes_the_switch_and_both_copies_match():
    here = Path(__file__).resolve().parents[1]
    src = (here / "timemachine" / "gate_core.py").read_text()
    assert 'hist_se_mult=_num(_cfg("TM_GATE_HIST_SE_MULT", 0.0)' in src
    rt = here.parent / "real-trade-service" / "timemachine"
    for f in ("engine.py", "gate_core.py"):
        assert (here / "timemachine" / f).read_bytes() == (rt / f).read_bytes()
    import config
    assert config.TM_GATE_HIST_SE_MULT == 1.0
