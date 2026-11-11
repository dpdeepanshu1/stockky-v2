"""E460: a real flat day (price == previous close) writes day_change_pct 0 over yesterday's value; a bare 0 is still the protected sparse fallback."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import data_feed as df  # noqa: E402

OLD = {"day_change_pct": 2.4, "volume": 1000, "pe_ratio": 20.0}


@pytest.mark.parametrize("key", ["last_price", "current_price", "ltp", "price", "cmp"])
def test_flat_day_overwrites_yesterdays_change(key):
    out = df.merge_feed_payload(OLD, {"previous_close": 100.0, key: 100.0, "day_change_pct": 0})
    assert out["day_change_pct"] == 0


def test_flat_within_rounding_counts():
    out = df.merge_feed_payload(OLD, {"previous_close": 100.0, "last_price": 100.004, "day_change_pct": 0.0})
    assert out["day_change_pct"] == 0.0


def test_a_move_of_one_paisa_on_a_cheap_stock_is_not_flat():
    out = df.merge_feed_payload(OLD, {"previous_close": 10.0, "last_price": 10.01, "day_change_pct": 0})
    assert out["day_change_pct"] == 2.4


def test_bare_zero_is_still_protected():
    assert df.merge_feed_payload(OLD, {"day_change_pct": 0})["day_change_pct"] == 2.4
    assert df.merge_feed_payload(OLD, {"previous_close": 100.0, "day_change_pct": 0})["day_change_pct"] == 2.4
    assert df.merge_feed_payload(OLD, {"last_price": 100.0, "day_change_pct": 0})["day_change_pct"] == 2.4


@pytest.mark.parametrize("prev", [0, -1, None, "x", float("nan"), float("inf")])
def test_unusable_previous_close_is_not_proof(prev):
    out = df.merge_feed_payload(OLD, {"previous_close": prev, "last_price": 100.0, "day_change_pct": 0})
    assert out["day_change_pct"] == 2.4


def test_flat_proof_does_not_unprotect_other_zero_fields():
    out = df.merge_feed_payload(OLD, {"previous_close": 100.0, "last_price": 100.0, "day_change_pct": 0, "volume": 0, "pe_ratio": 0})
    assert out["day_change_pct"] == 0 and out["volume"] == 1000 and out["pe_ratio"] == 20.0


def test_a_nonzero_change_still_always_writes():
    assert df.merge_feed_payload(OLD, {"day_change_pct": -1.2})["day_change_pct"] == -1.2


def test_helper_never_raises_and_rejects_non_dicts():
    assert df._flat_day_is_real(None) is False and df._flat_day_is_real([1]) is False
    assert df._flat_day_is_real({"previous_close": 5, "last_price": "bad", "price": 5}) is True


def test_bhavcopy_flat_record_end_to_end():
    rec = df.bhavcopy_seed({"symbol": "A", "price": 50.0, "last_price": 50.0, "previous_close": 50.0, "day_change_pct": 0.0})
    out = df.merge_feed_payload({"day_change_pct": 3.1, "pe_ratio": 20.0}, rec)
    assert out["day_change_pct"] == 0.0
