"""E273 round 8: the opening-range breakout is a LIVE input in position-stocks (orders/orb_shadow.py).
ORB_MODE is live | off (shadow / junk / blank read as live); the range is the exact 09:15-09:20 range from the recorded 1-minute bars, the quote capture window is the
fallback, so a symbol first seen after 09:26 now has a range; setup_live reads the record. Stdlib only (runs without SQLAlchemy)."""
import collections
import json
import pathlib
import subprocess
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from orders import orb_shadow as orb  # noqa: E402
from orders import setup_live  # noqa: E402
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
    return [bar(9, 15 + i, 99, 100, 98, 99.5, vol) for i in range(5)]          # 09:15..09:19, high 100, low 98


def feed(sym, bars):
    bar_store.reset()
    with bar_store._lock:
        bar_store._bars[sym] = collections.deque([list(b) for b in bars])


def start(monkeypatch, bars=None, sym="ABC"):
    monkeypatch.setattr(config, "ORB_MODE", "live", raising=False)
    monkeypatch.setattr(config, "ORB_BAR_RANGE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ORB_BAR_RANGE_MIN_BARS", 3, raising=False)
    orb._reset_for_tests()
    feed(sym, bars or [])


def _mode_of(value):
    code = "import sys; sys.path.insert(0,'.'); import config; print('M'+config.ORB_MODE)"
    import os
    env = {k: v for k, v in os.environ.items() if not k.startswith("ORB_")}
    if value is not None:
        env["ORB_MODE"] = value
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=60)
    return next(x for x in r.stdout.splitlines() if x.startswith("M"))[1:]


def test_mode_is_live_by_default_and_old_values_read_as_live():
    assert _mode_of(None) == "live"
    assert _mode_of("") == "live"
    assert _mode_of("shadow") == "live"          # there is no record-only mode any more
    assert _mode_of("enforce") == "live"
    assert _mode_of(" OFF ") == "off"


def test_bar_range_is_the_exact_0915_to_0920_range(monkeypatch):
    start(monkeypatch, range_bars() + [bar(9, 20, 99.5, 103.0, 99.5, 102.5, 5000)])   # the 09:20 bar is NOT part of the range
    r = orb.bar_range("ABC", at(9, 31))
    assert r is not None and r["high"] == 100.0 and r["low"] == 98.0 and r["n"] == 5.0


def test_no_bar_range_before_the_range_end_or_with_too_few_bars(monkeypatch):
    start(monkeypatch, range_bars())
    assert orb.bar_range("ABC", at(9, 19, 30)) is None                  # the range is still forming
    start(monkeypatch, range_bars()[:2])                                # 2 bars < ORB_BAR_RANGE_MIN_BARS (3)
    assert orb.bar_range("ABC", at(9, 31)) is None
    start(monkeypatch, [])
    assert orb.bar_range("ABC", at(9, 31)) is None


def test_bar_range_skips_unreadable_rows_and_bars_before_the_open(monkeypatch):
    rows = [("x", 1, 2, 3, 4, 5), (at(9, 10).timestamp(), 150, 160, 140, 150, 9)] + range_bars()
    start(monkeypatch, [])
    r = orb.bar_range("ABC", at(9, 31), bars=rows)
    assert r is not None and r["high"] == 100.0 and r["low"] == 98.0


def test_bar_range_switch_off_falls_back_to_the_quote_window(monkeypatch):
    start(monkeypatch, range_bars())
    monkeypatch.setattr(config, "ORB_BAR_RANGE_ENABLED", False, raising=False)
    assert orb.bar_range("ABC", at(9, 31)) is None
    orb.record_quote("ABC", q(day_high=100.4), at(9, 22))
    v = orb.evaluate("ABC", q(), 101.0, at(9, 40))
    assert v["range_src"] == "quote" and v["range_high"] == 100.4


def test_the_bar_range_beats_a_polluted_quote_capture(monkeypatch):
    start(monkeypatch, range_bars())
    orb.record_quote("ABC", q(day_high=103.0, day_low=97.0), at(9, 25))   # captured late: includes 09:20-09:25 moves
    v = orb.evaluate("ABC", q(day_high=103.0), 101.0, at(9, 40))
    assert v["range_src"] == "bars" and v["range_high"] == 100.0 and v["range_low"] == 98.0
    assert v["mode"] == "live"


def test_a_symbol_first_seen_after_the_capture_window_now_has_a_range(monkeypatch):
    start(monkeypatch, range_bars())
    v = orb.evaluate("ABC", q(), 101.0, at(9, 50))                        # never seen between 09:20 and 09:26
    assert v.get("reason") != "no_range" and v["range_src"] == "bars" and v["range_high"] == 100.0
    start(monkeypatch, [])
    v2 = orb.evaluate("ABC", q(), 101.0, at(9, 50))
    assert v2["reason"] == "no_range" and "range_src" not in v2           # no bars and no capture: still no range


def test_bar_range_is_cached_for_the_day_and_counted_once(monkeypatch):
    start(monkeypatch, range_bars())
    orb.evaluate("ABC", q(), 101.0, at(9, 40))
    feed("ABC", [])                                                       # the bars vanish: the fixed range stays
    v = orb.evaluate("ABC", q(), 101.0, at(9, 45))
    assert v["range_high"] == 100.0 and orb.stats()["bar_ranges"] == 1


def test_off_records_nothing(monkeypatch):
    start(monkeypatch, range_bars())
    monkeypatch.setattr(config, "ORB_MODE", "off", raising=False)
    assert orb.evaluate("ABC", q(), 101.0, at(9, 40)) is None
    assert orb.attach('{"v":1}', "ABC", q(), 101.0, at(9, 40)) == '{"v":1}'


def test_setup_live_reads_the_bar_range_and_the_closed_bar_trigger(monkeypatch):
    start(monkeypatch, range_bars() + [bar(9, 21, 99.8, 100.9, 99.8, 100.6, 3000)])
    fj = orb.attach(None, "ABC", q(), 101.0, at(9, 40))
    rec = json.loads(fj)["orb"]
    assert rec["range_src"] == "bars" and rec["close_trigger"] is True
    f = setup_live.facts_long(symbol="ABC", price=101.0, now=at(9, 40), event_polarity=None, flow_json=fj, quote=q(), gap_pct=None)
    assert f["or_high"] == 100.0 and f["bar_close"] == 100.6 and f["bar_closed_after_range"] is True


def test_no_record_only_wording_left_and_catalog_is_live_off():
    src = (ROOT / "orders" / "orb_shadow.py").read_text()
    assert "RECORD ONLY" not in src and "no live mode" not in src
    cat = (ROOT / "control" / "catalog.py").read_text()
    assert 'choices=("live", "off")' in cat.split('Flag("ORB_MODE"', 1)[1].split("Flag(", 1)[0]
    assert 'shipped="live"' in cat.split('Flag("ORB_MODE"', 1)[1].split("Flag(", 1)[0]
