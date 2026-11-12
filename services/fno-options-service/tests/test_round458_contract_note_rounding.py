"""Round 458 (E479): Dhan's real contract note (11 Nov 2026, NIFTY 17Nov2026 25950 CE, BUY 65 @ 61.10, SELL 65 @ 60.05) prints STT and stamp duty in whole rupees.
The charges check must match that note; before this round STT (5.85 vs 6.00) and stamp (0.12 vs 0.00) were flagged as differing."""
from __future__ import annotations

import pytest

from fno import costs, probecheck
from fno.params import Params

P = Params()
NOTE = {"brokerage": 40.00, "stt": 6.00, "exchange": 2.80, "sebi": 0.01, "gst": 7.71, "stamp": 0.00}      # NCLFO column of the note
NOTE_TOTAL = 56.52


def real_probe():
    c = costs.round_trip("NIFTY", 61.10, 60.05, 65, P)
    pnl = {"bought_avg": 61.10, "sold_avg": 60.05, "qty": 65, "gross": -68.25, "charges": c.total, "charges_detail": c.to_json(), "net": 0.0}
    return {"live": True, "underlying": "NIFTY", "side": "CE", "exit_code": 0, "contract": "NIFTY 17Nov2026 25950 CE", "bought": 65, "sold": 65, "dhan_net_position_after": 0, "pnl": pnl,
            "orders": [{"side": "BUY", "price": 61.10, "qty": 65, "ok": True, "order_id": "B", "status": "TRADED", "filled_qty": 65, "avg_price": 61.10},
                       {"side": "SELL", "price": 60.05, "qty": 65, "ok": True, "order_id": "S", "status": "TRADED", "filled_qty": 65, "avg_price": 60.05}]}


def test_rupee_rounds_halves_up_not_to_even():
    assert [costs.rupee(x) for x in (0.0, 0.119, 0.5, 0.6, 1.5, 2.5, 5.0465, 5.855)] == [0.0, 0.0, 1.0, 1.0, 2.0, 3.0, 5.0, 6.0]
    assert costs.rupee(-3) == 0.0


def test_on_contract_note_matches_the_real_note_line_by_line():
    n = costs.on_contract_note(costs.round_trip("NIFTY", 61.10, 60.05, 65, P)).to_json()
    for k, v in NOTE.items():
        assert n[k] == pytest.approx(v, abs=0.005), k
    assert n["total"] == pytest.approx(NOTE_TOTAL, abs=0.011)


def test_round_trip_itself_stays_exact():
    c = costs.round_trip("NIFTY", 61.10, 60.05, 65, P)
    assert c.stt == pytest.approx(5.8549, abs=1e-3) and c.stamp == pytest.approx(0.1191, abs=1e-3)


def test_the_real_note_is_matched_with_total():
    c = probecheck.compare(real_probe(), {**NOTE, "total": NOTE_TOTAL}, P)
    assert c["verdict"] == "MATCHED", c
    assert all(r["ok"] for r in c["lines"]) and c["total"]["ok"]
    by = {r["line"]: r for r in c["lines"]}
    assert by["stt"]["model"] == 6.0 and by["stt"]["rounded"] and by["stamp"]["model"] == 0.0 and not by["gst"]["rounded"]


def test_a_note_that_shows_the_paisa_figure_also_matches():
    c = probecheck.compare(real_probe(), {**NOTE, "stt": 5.85, "stamp": 0.12}, P)
    assert c["verdict"] == "MATCHED" and c["lines"][1]["diff"] == 0.0


def test_a_wrong_stt_rate_still_differs_even_with_rupee_rounding():
    c = probecheck.compare(real_probe(), {**NOTE, "stt": 4.00}, P)       # 0.1 % of the sell premium rounds to 4, not 6
    r = {x["line"]: x for x in c["lines"]}["stt"]
    assert c["verdict"] == "DIFFERS" and r["ok"] is False and r["param"] == "FNO_STT_SELL_PCT" and r["diff"] == -2.0


def test_a_stamp_of_one_rupee_differs_on_this_trade():
    c = probecheck.compare(real_probe(), {**NOTE, "stamp": 1.00}, P)
    assert c["verdict"] == "DIFFERS" and {x["line"]: x for x in c["lines"]}["stamp"]["ok"] is False
