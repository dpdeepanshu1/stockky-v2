"""Round 307: probecheck never lets nan / inf or a garbled file turn the charges comparison into a 500."""
from __future__ import annotations

import json

from fno import probecheck as pc


def _probe(**detail):
    d = {"brokerage": 40, "stt": 1.0, "exchange": 1.0, "sebi": 0.0, "gst": 7.0, "stamp": 0.1}
    d.update(detail)
    return {"live": True, "underlying": "NIFTY", "orders": [{"side": "BUY", "filled_qty": 65}, {"side": "SELL", "filled_qty": 65}],
            "pnl": {"charges_detail": d, "qty": 65, "bought_avg": 100.0, "sold_avg": 101.0}}


def test_nan_and_inf_are_not_numbers():
    assert pc._num("nan") is None and pc._num("inf") is None and pc._num("-inf") is None and pc._num("1e999") is None
    assert pc._num("1,234.5") == 1234.5 and pc._num("Rs 20") == 20.0


def test_a_nan_note_line_counts_as_missing_and_the_answer_is_valid_json():
    r = pc.compare(_probe(), {"brokerage": "nan", "stt": "inf"})
    json.dumps(r, allow_nan=False)                      # what the web framework does
    assert r["verdict"] == "NO_NOTE" and all(x["note"] is None for x in r["lines"])


def test_a_garbled_charges_line_is_not_comparable_not_a_crash():
    r = pc.compare(_probe(stt="abc"), {"stt": "1"})
    assert r["verdict"] == "NOT_COMPARABLE"
    assert pc.expected_lines(_probe(stt="abc")) is None


def test_a_clean_probe_still_compares():
    r = pc.compare(_probe(), {k: v for k, v in {"brokerage": 40, "stt": 1.0, "exchange": 1.0, "sebi": 0.0, "gst": 7.0, "stamp": 0.1}.items()})
    assert r["verdict"] == "MATCHED"
