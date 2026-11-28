"""Round 701 (E723): the outcomes labeller and GET /candidates/log treat a placed short (SHORT_ENTERED) as an entry and SHORT_SKIP as a skip."""
from orders import candidate_outcomes as co


def test_outcomes_gate_key_reads_a_placed_short_as_entered():
    assert co._gate_key("SELL 3 @ 209.93 SUPER_ORDER=1", "SHORT_ENTERED") == "ENTERED" and co._gate_key("x", "ENTERED") == "ENTERED"
    assert co._gate_key("SHORT_DAY_FALL:down 9%", "SHORT_SKIP") == "SHORT_DAY_FALL:down 9%"


def test_candidates_log_filter_groups_the_short_decisions():
    src = open("main.py", encoding="utf-8").read()
    i = src.index('def candidates_log(')
    body = src[i:i + 3500]
    assert '"ENTERED": ("ENTERED", "SHORT_ENTERED")' in body and '"SKIPPED": ("SKIPPED", "SHORT_SKIP")' in body
    assert "filter_by(decision=_df)" in body                                 # SOFT / SHORT_ENTERED / SHORT_SKIP still match exactly
