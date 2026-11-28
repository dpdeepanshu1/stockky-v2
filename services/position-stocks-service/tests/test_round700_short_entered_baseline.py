"""Round 700 (E722): a placed short (decision SHORT_ENTERED, reason "SELL 3 @ ...") is an entry on the short side, not "OTHER:SELL" measured as a long."""
import calendar
from datetime import datetime, timedelta, timezone

from orders import gate_policy_job as job, scorecard as sc

IST = timezone(timedelta(hours=5, minutes=30))
T0 = datetime(2026, 11, 26, 10, 0, tzinfo=IST).timestamp()
SELL = "SELL 3 @ 209.93 stop 212.00 target 205.00 SUPER_ORDER=123"


def test_gate_key_reads_a_placed_short_as_the_baseline():
    assert job.gate_key(SELL, "SHORT_ENTERED") == "ENTERED (baseline)" and job.gate_key("x", "ENTERED") == "ENTERED (baseline)"
    assert job.gate_key("SHORT_DAY_FALL:down 9%", "SHORT_SKIP") == "SHORT_DAY_FALL"          # a refusal keeps its own key


def test_side_sign_uses_the_decision_in_auto_only():
    assert sc.side_sign(SELL, "auto", "SHORT_ENTERED") == -1
    assert sc.side_sign(SELL, "auto", "ENTERED") == 1 and sc.side_sign(SELL, "long", "SHORT_ENTERED") == 1 and sc.side_sign(SELL, "short") == -1
    assert sc.side_sign("RANGE_GATE", "auto") == 1                                              # old two-argument callers unchanged


def test_a_placed_short_that_fell_is_a_short_baseline_win_not_a_long_other_sell():
    bars = [[T0 - 600 + 60 * i, 100.0, 100.0, 100.0, 100.0, 10] for i in range(11)] + [[T0 + 60 * (i + 1), 98.0, 98.0, 98.0, 98.0, 10] for i in range(80)]
    rows = [{"symbol": "AAA", "decision": "SHORT_ENTERED", "reason": SELL, "created_ts": T0}]
    res = sc.build(rows, lambda s, d: bars, lambda d: ["AAA"], cost=0.15, direction="auto", baseline_k=0)
    by = {(g["gate"], g["side"]): g for g in res["groups"]}
    assert ("ENTERED (baseline)", "SHORT") in by and not any(k[0] == "OTHER:SELL" for k in by)
    assert by[("ENTERED (baseline)", "SHORT")]["stats"]["mean"] > 1.0                          # price fell 2%: the short gained
    assert by[("ENTERED (baseline)", "SHORT")]["verdict"] == "baseline"
