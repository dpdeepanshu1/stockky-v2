"""E104 (P2 item 14): ORB recorder extra fields - close-based trigger and trigger-bar volume vs the opening-range average. Record only.
Stdlib only (runs without SQLAlchemy)."""
import collections
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from orders import orb_shadow as orb  # noqa: E402
from feed import bar_store  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))
DAY = (2026, 10, 23)                       # a Friday


def at(h, m, s=0):
    return datetime(*DAY, h, m, s, tzinfo=IST)


def bar(h, m, o, hi, lo, c, v):
    return (at(h, m).timestamp(), float(o), float(hi), float(lo), float(c), float(v))


def q(**kw):
    base = {"price": 101.0, "day_high": 100.0, "day_low": 98.0, "avg_price": 99.5, "spread_pct": 0.1}
    base.update(kw)
    return base


def range_bars(vol=1000):
    return [bar(9, 15 + i, 99, 100, 98, 99.5, vol) for i in range(5)]          # 09:15..09:19, high 100


def feed(sym, bars):
    bar_store.reset()
    with bar_store._lock:
        bar_store._bars[sym] = collections.deque([list(b) for b in bars])


def setup(monkeypatch, bars):
    monkeypatch.setattr(config, "ORB_MODE", "shadow", raising=False)
    orb._reset_for_tests()
    feed("ABC", bars)
    orb.record_quote("ABC", q(), at(9, 22))                                    # range 98..100


def test_close_above_high_is_the_trigger_and_volume_is_compared(monkeypatch):
    setup(monkeypatch, range_bars(1000) + [bar(9, 20, 99.5, 100.4, 99.5, 99.8, 500), bar(9, 21, 99.8, 100.9, 99.8, 100.6, 3000)])
    v = orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))
    assert v["close_trigger"] is True and v["trigger_bar_at"] == "09:21" and v["trigger_close"] == 100.6
    assert v["trigger_volume"] == 3000 and v["range_avg_volume"] == 1000 and v["volume_ratio"] == 3.0 and v["n_range_bars"] == 5


def test_a_wick_through_the_high_that_closes_inside_is_not_a_trigger(monkeypatch):
    setup(monkeypatch, range_bars() + [bar(9, 20, 99.5, 101.5, 99.5, 99.9, 9000)])
    v = orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))
    assert v["close_trigger"] is False and "volume_ratio" not in v and "trigger_volume" not in v
    assert v["would_enter"] is True                                            # the tick-based verdict is untouched


def test_close_exactly_on_the_high_is_not_above_it(monkeypatch):
    setup(monkeypatch, range_bars() + [bar(9, 20, 99.5, 100.2, 99.5, 100.0, 5000)])
    assert orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))["close_trigger"] is False


def test_a_bar_still_forming_is_not_counted(monkeypatch):
    setup(monkeypatch, range_bars() + [bar(9, 21, 99.8, 100.9, 99.8, 100.6, 3000)])
    assert orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 21, 30))["close_trigger"] is False
    assert orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 22, 0))["close_trigger"] is True


def test_the_first_closing_bar_is_the_trigger_not_a_later_one(monkeypatch):
    setup(monkeypatch, range_bars() + [bar(9, 21, 99.8, 100.9, 99.8, 100.5, 2000), bar(9, 22, 100.5, 101.5, 100.4, 101.2, 8000)])
    v = orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))
    assert v["trigger_bar_at"] == "09:21" and v["trigger_volume"] == 2000


def test_bars_before_the_open_are_ignored_and_range_bars_are_only_the_opening_range(monkeypatch):
    pre = [bar(9, 10, 99, 120, 90, 110, 900000)]
    setup(monkeypatch, pre + range_bars(1000) + [bar(9, 21, 99.8, 100.9, 99.8, 100.6, 2000)])
    v = orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))
    assert v["n_range_bars"] == 5 and v["range_avg_volume"] == 1000 and v["volume_ratio"] == 2.0


def test_no_range_bars_gives_the_trigger_but_no_ratio(monkeypatch):
    setup(monkeypatch, [bar(9, 21, 99.8, 100.9, 99.8, 100.6, 2000)])
    v = orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))
    assert v["close_trigger"] is True and v["n_range_bars"] == 0 and "volume_ratio" not in v


def test_no_bars_adds_no_fields_and_the_verdict_is_the_same(monkeypatch):
    setup(monkeypatch, [])
    v = orb.evaluate("ABC", q(day_high=101.5), 101.0, at(9, 40))
    assert "close_trigger" not in v and v["would_enter"] is True and v["range_high"] == 100.0


def test_junk_bars_never_raise():
    assert orb.bar_fields("ABC", 100.0, at(9, 40), bars=[None, (1,), ("x",) * 6, (at(9, 21).timestamp(), 1, 2, 3, "bad", 5)]) == {"close_trigger": False, "n_range_bars": 0}
    assert orb.bar_fields("ABC", None, at(9, 40), bars=range_bars()) == {}
    assert orb.bar_fields("ABC", 100.0, at(9, 40), bars=[]) == {}


def test_fields_never_change_would_enter_or_reasons(monkeypatch):
    setup(monkeypatch, range_bars() + [bar(9, 21, 99.8, 100.9, 99.8, 100.6, 3000)])
    with_bars = orb.evaluate("ABC", q(day_high=101.5), 99.0, at(9, 40))
    setup(monkeypatch, [])
    without = orb.evaluate("ABC", q(day_high=101.5), 99.0, at(9, 40))
    for k in ("would_enter", "reasons", "limit_price", "stop_price", "stop_pct", "range_high", "range_low"):
        assert with_bars[k] == without[k]
