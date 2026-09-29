"""price_resolver — unified safe-price resolution for scan / lite scan / surprise rows."""
import math

import pytest

import price_resolver as pr


class TestAsPositiveFloat:
    @pytest.mark.parametrize("v,expected", [
        (10, 10.0), ("12.345", 12.35), (1.005, 1.0), (99.999, 100.0),
    ])
    def test_valid_rounded_to_2dp(self, v, expected):
        assert pr._as_positive_float(v) == pytest.approx(expected, abs=0.011)

    @pytest.mark.parametrize("v", [None, 0, 0.0, -1, -0.01, float("nan"), "abc", "", [], {}, object()])
    def test_rejected(self, v):
        assert pr._as_positive_float(v) is None

    def test_infinity_is_accepted_as_positive(self):
        # Documents current behaviour: +inf is > 0 and not NaN, so it passes through.
        assert pr._as_positive_float(float("inf")) == float("inf")


class TestExtractSafePrice:
    def test_no_inputs_is_zero(self):
        assert pr.extract_safe_price() == 0.0
        assert pr.extract_safe_price("X", tick=None, feed=None, decision=None) == 0.0

    def test_tick_wins_over_decision_and_feed(self):
        assert pr.extract_safe_price("X", tick={"ltp": 101}, decision={"close": 99}, feed={"close": 98}) == 101.0

    def test_tick_key_priority_order(self):
        # "price" is checked before "cmp" and "close"
        assert pr.extract_safe_price(tick={"close": 3, "cmp": 2, "price": 1}) == 1.0

    def test_tick_skips_invalid_values(self):
        assert pr.extract_safe_price(tick={"price": 0, "cmp": None, "ltp": "bad", "last": 55}) == 55.0

    def test_decision_used_when_tick_empty(self):
        assert pr.extract_safe_price(tick={}, decision={"current_price": 44}, feed={"close": 1}) == 44.0

    def test_feed_used_last(self):
        assert pr.extract_safe_price(tick={}, decision={}, feed={"prev_close": 33}) == 33.0

    @pytest.mark.parametrize("nest", ["metrics", "data", "quote", "ohlc", "ticker", "info"])
    def test_feed_nested_shapes(self, nest):
        assert pr.extract_safe_price(feed={nest: {"close": 77.5}}) == 77.5

    def test_feed_top_level_beats_nested(self):
        assert pr.extract_safe_price(feed={"close": 5, "data": {"close": 9}}) == 5.0

    def test_non_dict_nested_ignored(self):
        assert pr.extract_safe_price(feed={"data": "oops", "quote": [1, 2], "info": {"ltp": 12}}) == 12.0

    @pytest.mark.parametrize("bad", ["str", 5, [1], ("a",)])
    def test_non_dict_inputs_ignored(self, bad):
        assert pr.extract_safe_price(tick=bad, feed=bad, decision=bad) == 0.0

    def test_all_invalid_returns_zero(self):
        assert pr.extract_safe_price(tick={"price": -1}, decision={"close": float("nan")}, feed={"ltp": 0}) == 0.0


class TestResolveDisplayPrice:
    def test_alias_of_extract(self):
        kw = dict(tick={"ltp": 10}, feed={"close": 9}, decision={"close": 8})
        assert pr.resolve_display_price("X", **kw) == pr.extract_safe_price("X", **kw) == 10.0

    def test_positional_call_shape_used_by_callers(self):
        # instant_scanner calls resolve_display_price(sym, {}, feed_item)
        assert pr.resolve_display_price("X", {}, {"close": 4}) == 4.0


class TestApplyPriceAliases:
    def test_stamps_all_aliases_and_mirrors_prev_close(self):
        row = pr.apply_price_aliases({"symbol": "X"}, 123.456)
        assert row == {"symbol": "X", "close": 123.46, "price": 123.46, "cmp": 123.46,
                       "current_price": 123.46, "ltp": 123.46, "last_price": 123.46, "prev_close": 123.46}

    def test_preserves_existing_positive_prev_close(self):
        row = pr.apply_price_aliases({"prev_close": 100}, 110)
        assert row["prev_close"] == 100 and row["close"] == 110.0

    def test_replaces_invalid_prev_close(self):
        assert pr.apply_price_aliases({"prev_close": 0}, 110)["prev_close"] == 110.0

    def test_zero_price_keeps_existing_positive_value(self):
        row = pr.apply_price_aliases({"close": 50, "ltp": 49}, 0)
        assert row["close"] == row["price"] == row["cmp"] == row["ltp"] == 50.0

    def test_zero_price_and_nothing_existing_returns_row_untouched(self):
        row = {"symbol": "X"}
        assert pr.apply_price_aliases(row, 0) == {"symbol": "X"}

    def test_none_price_uses_existing(self):
        assert pr.apply_price_aliases({"cmp": 8}, None)["close"] == 8.0

    @pytest.mark.parametrize("bad_row", [None, "row", 5, [1]])
    def test_non_dict_row_returned_as_is(self, bad_row):
        assert pr.apply_price_aliases(bad_row, 10) is bad_row

    def test_mutates_and_returns_same_dict(self):
        row = {}
        assert pr.apply_price_aliases(row, 5) is row and row["close"] == 5.0


class TestEnsureRowPrice:
    def test_uses_row_itself_as_decision(self):
        out = pr.ensure_row_price({"symbol": "X", "close": 20})
        assert out["ltp"] == 20.0 and out["prev_close"] == 20.0

    def test_tick_overrides_row(self):
        out = pr.ensure_row_price({"symbol": "X", "close": 20}, tick={"ltp": 25})
        assert out["close"] == 25.0

    def test_feed_used_when_row_has_no_price(self):
        out = pr.ensure_row_price({"symbol": "X"}, feed={"close": 15})
        assert out["cmp"] == 15.0

    def test_no_price_anywhere_leaves_row_unchanged(self):
        row = {"symbol": "X", "close": 0}
        assert pr.ensure_row_price(row) == {"symbol": "X", "close": 0}

    def test_missing_symbol_is_fine(self):
        assert pr.ensure_row_price({"price": 3})["close"] == 3.0

    @pytest.mark.parametrize("bad_row", [None, "row", 3])
    def test_non_dict_row_returned_as_is(self, bad_row):
        assert pr.ensure_row_price(bad_row) is bad_row
