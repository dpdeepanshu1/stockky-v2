"""Round 697 (E719): the technical-score floor is the fifth adaptive-policy gate (HARD / SOFT). The scorecard's one LOOSEN verdict (QUALITY_GATE:technical_score) could not be
acted on before: the nightly job only managed four gates."""
import calendar
from datetime import datetime, timezone

import pytest

import config
from orders import gate_policy as gp
from orders import gate_policy_job as job

T = gp.TECH


@pytest.fixture(autouse=True)
def _clean():
    gp.clear_cache()
    yield
    gp.clear_cache()


def _sig(**kw):
    from screening.quality_gate import QualitySignal
    return QualitySignal(symbol="X", **kw)


def test_gate_is_registered_with_hard_and_soft_only_and_defaults_to_hard():
    assert T == "QUALITY_GATE:technical_score" and T in gp.GATES
    assert gp.GATES[T][1] == (gp.HARD, gp.SOFT)                    # no OFF: the floor itself stays
    assert gp.static_state(T) == gp.HARD and gp.state(T) == gp.HARD and not gp.tech_half_size()
    gp.set_cache({(T, "LONG"): "OFF"})                             # an OFF row is not allowed for this gate: ignored
    assert gp.state(T) == gp.HARD


def test_hard_refuses_soft_passes_and_static_mode_is_the_rollback(monkeypatch):
    monkeypatch.setattr(config, "MIN_TECHNICAL_SCORE", 30.0)
    s = _sig(technical_score=25.0)
    ok, why = s.passes()
    assert not ok and why.startswith("technical_score 25 < floor 30")
    assert job.gate_key("QUALITY_GATE:" + why, "SKIPPED") == T     # the refusal's reason keys to the gate the scorecard measures
    gp.set_cache({(T, "LONG"): "SOFT"})
    assert s.tech_below_floor() and s.passes()[0]                  # SOFT: not refused here (entry.py halves the size)
    monkeypatch.setattr(config, "GATE_POLICY_MODE", "static")
    assert not s.passes()[0]
    monkeypatch.setattr(config, "GATE_POLICY_MODE", "all_hard")
    assert not s.passes()[0]


def test_soft_never_lets_through_what_other_checks_refuse(monkeypatch):
    monkeypatch.setattr(config, "MIN_TECHNICAL_SCORE", 30.0)
    monkeypatch.setattr(config, "MIN_FUNDAMENTAL_SCORE", 20.0)
    monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", True)
    gp.set_cache({(T, "LONG"): "SOFT"})
    assert not _sig(technical_score=10.0, negative_news="NEWS_NEGATIVE:regulatory action").passes()[0]
    assert not _sig(technical_score=10.0, fundamental_score=5.0).passes()[0]          # the fundamental floor is not part of this gate
    assert not _sig(technical_score=10.0, market_cap_cr=1.0).passes()[0] or config.MIN_MARKET_CAP_CR <= 1.0


def test_a_missing_or_passing_score_is_unaffected():
    gp.set_cache({(T, "LONG"): "SOFT"})
    assert not _sig(technical_score=None).tech_below_floor() and _sig(technical_score=None).passes()[0]
    assert not _sig(technical_score=config.MIN_TECHNICAL_SCORE).tech_below_floor()
    assert not _sig(technical_score=95.0).tech_below_floor()


def test_job_moves_the_gate_to_soft_on_evidence_and_leaves_it_hard_without(monkeypatch):
    def rows(vals):
        return [(f"2026-11-{10 + (i % 6):02d}", f"S{i % 25:02d}", v) for i, v in enumerate(vals)]
    good = rows([1.0 + (0.1 if i % 2 else -0.1) for i in range(40)])
    d = job.decide_all({(T, "LONG"): good}, {}, "2026-11-28")
    assert d[(T, "LONG")]["state"] == gp.SOFT and d[(T, "LONG")]["changed"]
    bad = rows([-0.5] * 40)
    d = job.decide_all({(T, "LONG"): bad}, {}, "2026-11-28")
    assert d[(T, "LONG")]["state"] == gp.HARD and not d[(T, "LONG")]["changed"]
    few = rows([2.0] * 10)
    assert job.decide_all({(T, "LONG"): few}, {}, "2026-11-28")[(T, "LONG")]["state"] == gp.HARD
    # and back: a SOFT gate whose SOFT-row outcomes turn clearly negative returns to HARD
    d = job.decide_all({(T, "LONG"): bad}, {(T, "LONG"): {"state": "SOFT", "last_change_day": "2026-11-01"}}, "2026-11-28")
    assert d[(T, "LONG")]["state"] == gp.HARD and d[(T, "LONG")]["changed"]


def test_soft_rows_and_skipped_rows_both_reach_the_job_as_this_gate():
    now = datetime(2026, 11, 28, 11, 0, tzinfo=timezone.utc)
    ts = calendar.timegm(now.timetuple())
    seen = []
    rows = [
        {"symbol": "AAA", "decision": "SKIPPED", "reason": "QUALITY_GATE:technical_score 22 < floor 30", "created_ts": ts},
        {"symbol": "BBB", "decision": "SOFT", "reason": "QUALITY_GATE:technical_score 25 < floor 30 (size penalty 1.0, SOFT)", "created_ts": ts},
    ]
    bars = lambda sym, day: seen.append(sym) or []
    out, counts = job.samples_from_rows(rows, bars, 0.15)
    assert sorted(seen) == ["AAA", "BBB"] and counts["rows"] == 2           # both were keyed to an adaptable gate and looked up (no bars here, so no samples)
    assert out == {}


def test_entry_applies_the_penalty_before_the_setup_engine_sizes():
    e = open("orders/entry.py").read()
    assert "gate_policy.tech_half_size()" in e
    i = e.index('_penalties["tech_low"]')
    assert i < e.index("_pen_early = dict(_penalties)")                    # the setup engine sizes with it, like event_unchecked
    assert "QUALITY_GATE:technical_score" in e[i - 600:i + 600] and "_log_policy_soft(" in e[i:i + 600]
    q = open("screening/quality_gate.py").read()
    assert "gate_policy.tech_half_size()" in q


def test_catalog_text_names_five_gates():
    c = open("control/catalog.py").read()
    assert "five gates" in c and "technical score under the floor" in c
