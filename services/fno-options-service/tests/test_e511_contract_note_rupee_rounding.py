"""E511: Dhan's contract note prints every charge line in whole rupees (halves up). costs.rupee / costs.on_contract_note model that and
probecheck.compare accepts a note line that equals either the exact figure or its whole-rupee form, while still flagging a wrong rate."""
from __future__ import annotations

from fno import costs, probecheck
from fno.params import Params
from tests.test_round265_probecheck import by, make_probe, note_of

P = Params()


def test_rupee_rounds_halves_up_not_to_even():
    assert [costs.rupee(x) for x in (0.0, 0.119, 0.5, 0.6, 1.5, 2.5, 5.0465, 5.855)] == [0.0, 0.0, 1.0, 1.0, 2.0, 3.0, 5.0, 6.0]
    assert round(2.5) == 2 and costs.rupee(2.5) == 3.0            # the difference from the builtin
    assert costs.rupee(float("nan")) != costs.rupee(float("nan")) or costs.rupee(float("nan")) == 0.0   # never raises


def test_on_contract_note_rounds_the_six_lines_and_keeps_the_exact_total():
    c = costs.round_trip("NIFTY", 61.10, 60.05, 65, P)
    n = costs.on_contract_note(c)
    for f in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp"):
        assert getattr(n, f) == costs.rupee(getattr(c, f)) and getattr(n, f) == int(getattr(n, f))
    assert n.total == c.total


def test_a_note_printed_in_whole_rupees_is_matched():
    pr = make_probe(buy=61.10, sell=60.05)
    exact = costs.round_trip("NIFTY", 61.10, 60.05, 65, P)
    note = {f: costs.rupee(getattr(exact, f)) for f in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp")}
    c = probecheck.compare(pr, {**note, "total": round(exact.total, 2)}, P)
    assert c["verdict"] == "MATCHED", c


def test_a_note_to_the_paisa_still_matches():
    pr = make_probe()
    assert probecheck.compare(pr, note_of(pr), P)["verdict"] == "MATCHED"


def test_a_wrong_stt_rate_still_differs_even_with_rupee_rounding():
    pr = make_probe(buy=61.10, sell=60.05)
    exact = costs.round_trip("NIFTY", 61.10, 60.05, 65, P)
    note = {f: costs.rupee(getattr(exact, f)) for f in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp")}
    note["stt"] = note["stt"] - 2.0
    c = probecheck.compare(pr, note, P)
    r = by(c)["stt"]
    assert c["verdict"] == "DIFFERS" and r["ok"] is False and r["param"] == "FNO_STT_SELL_PCT" and r["diff"] == -2.0
