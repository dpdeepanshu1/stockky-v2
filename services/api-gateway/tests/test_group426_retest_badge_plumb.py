"""group426: the Buy Sniper card carries structure.retest_entry from the decision's structure block (display only)."""
import buy_sniper as bs


def test_zones_from_decision_structure_block():
    assert bs._retest_entry_zones({"structure": {"retest_entry": ["ob", "fvg"]}}) == ["ob", "fvg"]


def test_none_when_branch_not_used_or_malformed():
    for row in (None, {}, {"structure": None}, {"structure": {"retest_entry": None}}, {"structure": {"retest_entry": []}},
                {"structure": {"retest_entry": "ob"}}, {"structure": "x"}, {"structure": {"retest_entry": [1, None, ""]}}):
        assert bs._retest_entry_zones(row) is None


def test_non_string_entries_dropped():
    assert bs._retest_entry_zones({"structure": {"retest_entry": ["ob", 3, None, "fib"]}}) == ["ob", "fib"]
