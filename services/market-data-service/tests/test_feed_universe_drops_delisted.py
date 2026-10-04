"""group125: the feed-universe refresh drops known-delisted symbols (AAKASH, ANNAPURNA, ...)
before re-pointing the AngelOne/Yahoo feeds, so the feeds do not subscribe to names that can
never produce a tick."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m


def test_known_delisted_names_are_dropped_and_reported_sorted():
    syms, dropped = m._clean_feed_universe(["TCS", "ANNAPURNA", "AAKASH.NS", "INFY"])
    assert syms == ["TCS", "INFY"]
    assert dropped == ["AAKASH", "ANNAPURNA"]


def test_suffixes_are_stripped_and_case_is_normalised_order_kept():
    syms, dropped = m._clean_feed_universe(["wipro.ns", "Hdfcbank.BO", "SBIN"])
    assert syms == ["WIPRO", "HDFCBANK", "SBIN"]
    assert dropped == []


def test_blank_entries_are_skipped():
    assert m._clean_feed_universe(["", None, "TCS"]) == (["TCS"], [])


def test_non_list_input_gives_empty_result():
    for bad in (None, "TCS", {"a": 1}, 5):
        assert m._clean_feed_universe(bad) == ([], [])


def test_all_delisted_gives_empty_symbols():
    syms, dropped = m._clean_feed_universe(["AAKASH", "ANNAPURNA"])
    assert syms == [] and dropped == ["AAKASH", "ANNAPURNA"]
