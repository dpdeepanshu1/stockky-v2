"""Short selling step 5: the mirrored, fail-CLOSED entry gates (orders/short_gates.py). Pure: no network, no DB. A passing set of facts is built once and each
test breaks exactly one thing."""
import types
from datetime import datetime, timezone, timedelta

import config
from orders import short_gates as g

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 10, 23, 10, 30, tzinfo=IST)          # inside 09:45-13:30 (only the clock time matters)


def good(**over):
    f = {
        "ltp": 98.2, "signal_ltp": 98.2, "live": 98.0, "tick_age_s": 1.0,
        "day_open": 100.0, "day_high": 101.0, "day_low": 96.0, "prev_close": 100.0, "vwap": 98.5,
        "quote": {"upper_circuit": 110.0, "lower_circuit": 90.0, "spread_pct": 0.05, "book_value_5": 5_000_000.0, "avg_price": 98.5},
        "board_row": {"side": "bear", "conviction": 70, "chase_atr": 0.4},
        "structure": {"ms_quality": "ok", "ms_asof": (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(), "ms_price": 98.0, "ms_atr": 2.0,
                      "ms_trend": "bearish", "ms_stretch_atr": -0.5, "ms_bos_dir": "bearish", "ms_bos_bars": 2, "ms_bos_failed": False,
                      "ms_bos_vol_ratio": 1.6},
        "quality": types.SimpleNamespace(event_unchecked=False, has_positive_catalyst=False, bulk_deal_flag=False, earnings_date=None),
        "today": "2026-10-23", "tomorrow": "2026-10-24",
    }
    f.update(over)
    return f


def code(f, now=NOW):
    v = g.evaluate(f, now)
    return v[0] if v else None


def test_a_clean_short_passes_every_gate():
    assert g.evaluate(good(), NOW) is None


def test_time_window():
    assert code(good(), datetime(2026, 10, 23, 9, 30, tzinfo=IST)) == "SHORT_TOO_EARLY"
    assert code(good(), datetime(2026, 10, 23, 13, 31, tzinfo=IST)) == "SHORT_CUTOFF"


def test_missing_or_stale_tick_refuses():
    assert code(good(live=None)) == "SHORT_NO_LIVE_TICK"
    if config.ENTRY_MAX_TICK_AGE_S > 0:
        assert code(good(tick_age_s=None)) == "SHORT_STALE_TICK"
        assert code(good(tick_age_s=config.ENTRY_MAX_TICK_AGE_S + 5)) == "SHORT_STALE_TICK"


def test_slippage_is_a_price_BELOW_the_signal():
    if config.ENTRY_MAX_SLIPPAGE_PCT > 0:
        assert code(good(signal_ltp=98.0 * (1 + (config.ENTRY_MAX_SLIPPAGE_PCT + 0.5) / 100.0))) == "SHORT_SLIPPAGE"
    assert code(good(signal_ltp=97.0, live=97.9, vwap=98.0)) is None      # price ABOVE the signal is better for a short, not slippage


def test_day_fall_limit_and_missing_prev_close():
    assert code(good(prev_close=110.0, day_open=108.0)) == "SHORT_DAY_FALL"      # down ~10.9%
    assert code(good(prev_close=None)) == "SHORT_NO_PREV_CLOSE"


def test_near_day_low_and_missing_range():
    assert code(good(live=96.2, signal_ltp=96.2, vwap=96.5)) == "NEAR_DAY_LOW"
    assert code(good(day_low=None)) == "SHORT_NO_DAY_RANGE"


def test_gap_up_still_above_open_refuses_but_gap_down_is_allowed():
    q = {"upper_circuit": 115.0, "lower_circuit": 90.0, "spread_pct": 0.05, "book_value_5": 5e6, "avg_price": 103.4}
    assert code(good(day_open=103.0, prev_close=100.0, day_high=104.0, live=103.5, signal_ltp=103.5, vwap=103.4, quote=q)) == "SHORT_GAP_UP"
    assert code(good(day_open=97.0, prev_close=100.0)) is None
    assert code(good(day_open=None)) == "SHORT_NO_GAP_DATA"


def test_vwap_is_mirrored_and_missing_refuses():
    assert code(good(vwap=96.0)) == "VWAP_ABOVE"                   # price 98 is ~2% ABOVE vwap 96: buyers in control
    assert code(good(vwap=98.0 / (1 + config.SHORT_VWAP_TOLERANCE_PCT / 200.0))) is None   # inside the tolerance
    assert code(good(vwap=None)) == "SHORT_NO_VWAP"
    assert code(good(vwap=130.0)) == "SHORT_VWAP_IMPLAUSIBLE"


def test_circuit_roles_swap_and_missing_band_refuses():
    q = lambda up, lo: {"upper_circuit": up, "lower_circuit": lo, "spread_pct": 0.05, "book_value_5": 5e6, "avg_price": 98.5}
    assert code(good(quote=q(99.0, 90.0))) == "CIRCUIT_UPPER"                       # within 3% of the upper circuit
    assert code(good(quote=q(110.0, 97.0))) == "CIRCUIT_LOWER"                      # within 2% of the lower circuit
    assert code(good(quote=q(None, 90.0))) == "SHORT_NO_CIRCUIT_BAND"
    assert code(good(quote=None)) == "SHORT_NO_CIRCUIT_BAND"
    assert code(good(quote=q(50.0, 40.0))) == "SHORT_CIRCUIT_STALE"


def test_depth_must_be_readable():
    assert code(good(quote={"upper_circuit": 110.0, "lower_circuit": 90.0, "avg_price": 98.5})) == "SHORT_DEPTH_MISSING"
    if config.ENTRY_DEPTH_MAX_SPREAD_PCT > 0:
        wide = {"upper_circuit": 110.0, "lower_circuit": 90.0, "spread_pct": config.ENTRY_DEPTH_MAX_SPREAD_PCT + 1, "book_value_5": 5e6, "avg_price": 98.5}
        assert code(good(quote=wide)) == "DEPTH_SPREAD"


def test_board_must_say_bear_with_conviction():
    assert code(good(board_row=None)) == "SHORT_NO_BOARD_ROW"
    assert code(good(board_row={"side": "bull", "conviction": 90})) == "SHORT_BOARD_NOT_BEAR"
    assert code(good(board_row={"side": "bear", "conviction": 10})) == "SHORT_BOARD_CONVICTION"
    assert code(good(board_row={"side": "bear", "conviction": None})) == "SHORT_BOARD_CONVICTION"
    if config.BOARD_CHASE_ATR > 0:
        assert code(good(board_row={"side": "bear", "conviction": 80, "chase_atr": config.BOARD_CHASE_ATR + 1})) == "BOARD_CHASE"


def test_structure_is_mirrored_and_required():
    s = lambda **kw: dict(good()["structure"], **kw)
    assert code(good(structure=None)) == "SHORT_STRUCTURE_NODATA"
    assert code(good(structure=s(ms_quality="unavailable"))) == "SHORT_STRUCTURE_NODATA"
    assert code(good(structure=s(ms_trend="bullish"))) == "SHORT_STRUCTURE_BULLISH"
    assert code(good(structure=s(ms_bos_dir="bullish", ms_bos_bars=1))) == "SHORT_STRUCTURE_BULLISH_BOS"
    assert code(good(structure=s(ms_stretch_atr=-9.0))) == "SHORT_STRUCTURE_STRETCHED"
    assert code(good(structure=s(ms_bos_vol_ratio=0.5))) == "SHORT_STRUCTURE_THIN_BREAKDOWN"
    assert code(good(structure=s(ms_stretch_atr=None))) == "SHORT_STRUCTURE_NODATA"


def test_a_bearish_trend_does_not_block_a_short():
    assert code(good(structure=dict(good()["structure"], ms_trend="bearish"))) is None


def test_quality_weak_fundamentals_fine_positive_news_refuses():
    mk = lambda **kw: types.SimpleNamespace(**dict(dict(event_unchecked=False, has_positive_catalyst=False, bulk_deal_flag=False, earnings_date=None), **kw))
    assert code(good(quality=mk(fundamental_score=5.0))) is None
    assert code(good(quality=None)) == "SHORT_NO_QUALITY"
    assert code(good(quality=mk(event_unchecked=True))) == "SHORT_EVENT_UNCHECKED"
    assert code(good(quality=mk(has_positive_catalyst=True))) == "SHORT_POSITIVE_CATALYST"
    assert code(good(quality=mk(bulk_deal_flag=True))) == "SHORT_BULK_DEAL"
    assert code(good(quality=mk(earnings_date="2026-10-23"))) == "SHORT_EARNINGS"
    assert code(good(quality=mk(earnings_date="2026-10-24"))) == "SHORT_EARNINGS"
    assert code(good(quality=mk(earnings_date="2026-11-30"))) is None


def test_a_gate_that_raises_refuses(monkeypatch):
    def boom(f, now):
        raise RuntimeError("x")
    monkeypatch.setattr(g, "GATES", (("boom", boom),))
    assert g.evaluate(good(), NOW) == ("SHORT_GATE_ERROR", "boom: RuntimeError")


def test_evaluate_stops_at_the_first_refusal_in_order():
    assert code(good(live=None, vwap=None, quote=None)) == "SHORT_NO_LIVE_TICK"      # price guard comes before vwap / circuit


def test_every_short_threshold_is_in_the_catalog_and_read_by_the_short_code():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    cat = (root / "control" / "catalog.py").read_text()
    src = (root / "orders" / "short_gates.py").read_text() + (root / "orders" / "short_entry.py").read_text()
    for name in ("SHORT_MAX_POSITIONS", "SHORT_ENTRY_START_IST", "SHORT_ENTRY_CUTOFF_IST", "SHORT_SQUAREOFF_TIME_IST", "SHORT_VWAP_TOLERANCE_PCT",
                 "SHORT_MIN_ENTRY_RANGE_POSITION", "SHORT_MAX_DAY_FALL_PCT", "SHORT_UPPER_CIRCUIT_BUFFER_PCT", "SHORT_MIN_BOARD_CONVICTION",
                 "SHORT_GAP_UP_REFUSE_PCT"):
        assert f'"{name}"' in cat, name
        assert name in src, name


def test_the_short_entry_path_never_places_an_order_or_reserves_capital():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    for rel in ("short_entry.py", "short_gates.py"):
        s = (root / "orders" / rel).read_text()
        for banned in ("place_super_order", "place_order", "reserve_capital(", "try_claim", "shared_order_budget", "dhan_client"):
            assert banned not in s, (rel, banned)


def test_attempt_entry_is_untouched_by_step5():
    root = __import__("pathlib").Path(__file__).resolve().parent.parent
    s = (root / "orders" / "entry.py").read_text()
    assert "short_gates" not in s and "short_entry" not in s and "SHORT_" not in s
