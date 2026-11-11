"""E437 (round 420): pin the key-order rule of normalize_feed_payload that round 379 note (1) listed as a risk.

`price` / `close` / `cmp` / `prevclose` / `prev_close` all map onto `prev_close`, and the LAST one in the dict wins. The Repair worker
(`_patch_single_stock_feed`) depends on it: it copies the stored (already normalized) row, which has `prev_close`, and then sets `price`,
`close` and `cmp` AFTER it, so the repaired price replaces the old prev_close. Making an explicit `prev_close` win would silently drop every
repaired price. If you ever change this, change repair to write `prev_close` itself first.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from data_feed import normalize_feed_payload  # noqa: E402


def test_later_price_key_replaces_an_earlier_prev_close():
    row = {"symbol": "A", "prev_close": 100.0, "price": 123.0, "close": 123.0, "cmp": 123.0}
    assert normalize_feed_payload(row)["prev_close"] == 123.0


def test_later_prev_close_replaces_an_earlier_price():
    row = {"symbol": "A", "price": 123.0, "prev_close": 100.0}
    assert normalize_feed_payload(row)["prev_close"] == 100.0


def test_previous_close_is_a_separate_key():
    out = normalize_feed_payload({"symbol": "A", "price": 123.0, "previous_close": 99.0})
    assert out["prev_close"] == 123.0 and out["previous_close"] == 99.0
