"""Round 222: a previous close that is not the reference today's prices are on (split / bonus / demerger day) must not become a
"-80 % day". Generic checks only: the exchange circuit band (symmetric around the exchange's own reference) and today's own prints."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dhan_data import prev_close_guard as g  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    g.reset_for_tests()
    for k in ("PREV_CLOSE_GUARD_MODE", "PREV_CLOSE_GUARD_BAND_TOL_PCT", "PREV_CLOSE_GUARD_GAP_PCT",
              "PREV_CLOSE_GUARD_TRIP_PCT", "PREV_CLOSE_GUARD_TRIP_MIN", "PREV_CLOSE_GUARD_TRIP_S"):
        monkeypatch.delenv(k, raising=False)
    yield
    g.reset_for_tests()


# KMEW 6 Nov: open / high / low / ltp 641.0 / 647.9 / 606.0 / 608.3, previous close 3143.3 (old scale)
KMEW = dict(open_=641.0, high=647.9, low=606.0)


def test_normal_row_is_confirmed_without_a_band():
    a = g.assess(101.0, 100.0, open_=100.5, high=102.0, low=99.0)
    assert a["status"] == "ok" and a["prev"] == 100.0


def test_band_agrees_with_previous_close():
    a = g.assess(103.0, 100.0, upper=110.0, lower=90.0, open_=101.0)
    assert a["status"] == "ok" and a["reason"] == "circuit_agrees" and a["prev"] == 100.0


def test_band_base_replaces_an_off_scale_previous_close():
    a = g.assess(608.3, 3143.3, upper=729.6, lower=486.4, **KMEW)       # +/-20 % around 608.0
    assert a["status"] == "corrected" and a["prev"] == 608.0 and a["raw"] == 3143.3


def test_no_band_and_no_print_near_previous_close_is_withdrawn():
    a = g.assess(608.3, 3143.3, **KMEW)
    assert a["status"] == "suspect" and a["prev"] is None and a["raw"] == 3143.3


def test_big_genuine_mover_inside_a_band_is_untouched():
    a = g.assess(119.0, 100.0, upper=120.0, lower=80.0, open_=110.0, high=119.5, low=108.0)
    assert a["status"] == "ok" and a["prev"] == 100.0


def test_big_gap_with_prints_near_previous_close_is_untouched():
    a = g.assess(82.0, 100.0, open_=92.0, high=93.0, low=80.0)
    assert a["status"] == "ok"


def test_lower_circuit_lock_is_untouched():
    a = g.assess(80.0, 100.0, open_=80.0, high=80.0, low=80.0)
    assert a["status"] == "ok"


def test_needs_price_plus_two_of_open_high_low():
    a = g.assess(608.3, 3143.3, high=647.9)
    assert a["status"] == "ok"


def test_band_that_does_not_contain_the_price_cannot_vouch():
    a = g.assess(608.3, 3143.3, upper=3300.0, lower=3000.0, **KMEW)
    assert a["status"] == "suspect"


def test_dummy_band_is_ignored():
    a = g.assess(101.0, 100.0, upper=1000.0, lower=1.0, open_=100.0, high=102.0, low=99.0)
    assert a["status"] == "ok" and a["reason"] is None


def test_idempotent():
    a = g.assess(608.3, 3143.3, upper=729.6, lower=486.4, **KMEW)
    b = g.assess(608.3, a["prev"], upper=729.6, lower=486.4, **KMEW)
    assert b["status"] == "ok" and b["prev"] == a["prev"]


def test_missing_or_bad_inputs_never_raise():
    for price, prev in ((None, 100.0), (100.0, None), (0, 100.0), ("x", "y"), (float("nan"), 1.0)):
        assert g.assess(price, prev, open_="z", upper="q", lower=None)["status"] == "unchecked"


def test_mode_off(monkeypatch):
    monkeypatch.setenv("PREV_CLOSE_GUARD_MODE", "off")
    a = g.assess(608.3, 3143.3, **KMEW)
    assert a["status"] == "unchecked" and a["prev"] == 3143.3


def test_thresholds_are_settable(monkeypatch):
    monkeypatch.setenv("PREV_CLOSE_GUARD_GAP_PCT", "90")
    assert g.assess(608.3, 3143.3, **KMEW)["status"] == "ok"


def test_day_change_pct():
    assert g.day_change_pct(110.0, 100.0) == 10.0
    assert g.day_change_pct(110.0, None) is None


def test_systemic_mismatch_trips_and_fails_open():
    for i in range(400):
        bad = i < 80                   # round 499: 20 % flagged (the trip is now above 10 % over at least 300 symbols)
        g.assess(608.3 if bad else 101.0, 3143.3 if bad else 100.0, symbol=f"S{i}",
                 open_=641.0 if bad else 100.5, high=647.9 if bad else 102.0, low=606.0 if bad else 99.0)
    assert g.tripped() is True
    a = g.assess(608.3, 3143.3, symbol="NEW", **KMEW)
    assert a["status"] == "unchecked" and a["prev"] == 3143.3


def test_a_handful_of_corporate_actions_does_not_trip():
    for i in range(300):
        bad = i < 3
        g.assess(608.3 if bad else 101.0, 3143.3 if bad else 100.0, symbol=f"S{i}",
                 open_=641.0 if bad else 100.5, high=647.9 if bad else 102.0, low=606.0 if bad else 99.0)
    assert g.tripped() is False


def _feed(n, every):
    for i in range(n):
        bad = i % every == 0           # flagged names spread evenly through the feed
        g.assess(608.3 if bad else 101.0, 3143.3 if bad else 100.0, symbol=f"S{i}",
                 open_=641.0 if bad else 100.5, high=647.9 if bad else 102.0, low=606.0 if bad else 99.0)


def test_round499_a_normal_busy_day_of_about_five_percent_does_not_switch_the_guard_off():
    _feed(600, 19)                     # 5.3 %: a day with many splits / bonuses / new listings (the 14 Nov review item 13 saw 5.1 %)
    assert g.tripped() is False


def _feed_rolled(n, every):
    """After the close the feed's previous close is the last price; the band (95 / 105 around a base of 100) still shows the real reference."""
    for i in range(n):
        if i % every == 0:
            r = g.assess(105.9, 105.9, symbol=f"R{i}", upper=105.0, lower=95.0)      # moved 5.9 %: corrected to the band base
            assert r["status"] == "corrected" and r["prev"] == 100.0
        else:
            g.assess(101.0, 100.0, symbol=f"R{i}", upper=105.0, lower=95.0)


def test_round500_a_post_close_roll_is_corrected_but_never_trips_the_guard():
    _feed_rolled(600, 4)               # 25 % of the names "moved more than 5 %": a roll, not a layout fault
    assert g.tripped() is False


def test_round500_real_mismatches_still_trip_even_with_rolled_rows_in_the_mix():
    _feed_rolled(300, 4)
    _feed(600, 5)                      # 20 % genuinely wrong rows (previous close far from the price) on other symbols
    assert g.tripped() is True


def test_round499_a_small_early_sample_never_trips():
    _feed(250, 2)                      # half flagged, but under 300 distinct symbols so far
    assert g.tripped() is False


def test_round499_just_over_ten_percent_of_a_full_sample_still_trips():
    _feed(600, 9)                      # 11 %
    assert g.tripped() is True


def test_repeated_polls_of_one_symbol_count_once():
    for _ in range(500):
        g.assess(608.3, 3143.3, symbol="KMEW", **KMEW)
    assert g.tripped() is False


# ---- the Dhan row mapper ----
def _item(last, prev, o, h, l, up=None, lw=None):
    it = {"last_price": last, "ohlc": {"open": o, "high": h, "low": l, "close": prev}, "volume": 1000, "net_change": last - prev}
    if up is not None:
        it["upper_circuit_limit"], it["lower_circuit_limit"] = up, lw
    return it


def test_map_item_corrects_with_the_band():
    from dhan_data import quotes as q
    before = dict(q._stats)
    row = q.map_item("KMEW", _item(608.3, 3143.3, 641.0, 647.9, 606.0, 729.6, 486.4))
    assert row["previous_close"] == 608.0 and row["prev_close_flag"] == "corrected" and row["previous_close_raw"] == 3143.3
    assert row["day_change_pct"] == round((608.3 - 608.0) / 608.0 * 100, 2)
    assert q._stats["prev_close_corrected"] == before["prev_close_corrected"] + 1


def test_map_item_withdraws_without_a_band():
    from dhan_data import quotes as q
    row = q.map_item("KMEW", _item(608.3, 3143.3, 641.0, 647.9, 606.0))
    assert row["previous_close"] is None and row["day_change_pct"] is None and row["prev_close_flag"] == "suspect"


def test_map_item_normal_row_has_no_flag_fields():
    from dhan_data import quotes as q
    row = q.map_item("TCS", _item(101.0, 100.0, 100.5, 102.0, 99.0, 110.0, 90.0))
    assert row["previous_close"] == 100.0 and row["day_change_pct"] == 1.0
    assert "prev_close_flag" not in row and "previous_close_raw" not in row


def test_pad_quote_response_guards_non_dhan_rows():
    main = pytest.importorskip("main")
    out = main._pad_quote_response("KMEW", {"symbol": "KMEW", "price": 608.3, "previous_close": 3143.3, "day_change_pct": -80.65,
                                            "day_high": 647.9, "day_low": 606.0, "open": 641.0, "source": "angelone_rest"})
    assert out["previous_close"] is None and out["day_change_pct"] is None and out["prev_close_flag"] == "suspect"
    ok = main._pad_quote_response("TCS", {"symbol": "TCS", "price": 101.0, "previous_close": 100.0, "day_change_pct": 1.0,
                                          "day_high": 102.0, "day_low": 99.0, "source": "angelone_rest"})
    assert ok["previous_close"] == 100.0 and "prev_close_flag" not in ok


def test_pad_quote_response_passes_a_dhan_flag_through():
    main = pytest.importorskip("main")
    out = main._pad_quote_response("KMEW", {"symbol": "KMEW", "price": 608.3, "previous_close": 608.0, "day_change_pct": 0.05,
                                            "source": "dhan", "prev_close_flag": "corrected", "previous_close_raw": 3143.3})
    assert out["previous_close"] == 608.0 and out["prev_close_flag"] == "corrected" and out["previous_close_raw"] == 3143.3
