"""E119: the opening-range recorder mirrored for a SHORT (close below the range low, stop at the range high). Record only. Stdlib only."""
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
DAY = (2026, 10, 23)


def at(h, m, s=0):
    return datetime(*DAY, h, m, s, tzinfo=IST)


def bar(h, m, o, hi, lo, c, v):
    return (at(h, m).timestamp(), float(o), float(hi), float(lo), float(c), float(v))


def q(**kw):
    base = {"price": 97.0, "day_high": 100.0, "day_low": 98.0, "avg_price": 99.0, "spread_pct": 0.1}
    base.update(kw)
    return base


def range_bars(vol=1000):
    return [bar(9, 15 + i, 99, 100, 98, 99.5, vol) for i in range(5)]          # range 98..100


def setup(monkeypatch, bars):
    monkeypatch.setattr(config, "ORB_MODE", "shadow", raising=False)
    orb._reset_for_tests()
    bar_store.reset()
    with bar_store._lock:
        bar_store._bars["ABC"] = collections.deque([list(b) for b in bars])
    orb.record_quote("ABC", q(), at(9, 22))


def test_short_verdict_mirrors_the_long_one(monkeypatch):
    setup(monkeypatch, range_bars())
    v = orb.evaluate("ABC", q(), 97.0, at(9, 40), "SHORT")
    assert v["side"] == "SHORT" and v["would_enter"] is True and v["reasons"] == []
    assert v["stop_price"] == 100.0 and v["limit_price"] == round(98.0 * (1 - config.ORB_LIMIT_BUFFER_PCT / 100.0), 2)
    assert v["stop_pct"] == round((100.0 - 97.0) / 97.0 * 100.0, 3)


def test_short_inside_or_above_the_range_low_and_above_vwap_is_refused_with_mirrored_reasons(monkeypatch):
    setup(monkeypatch, range_bars())
    v = orb.evaluate("ABC", q(avg_price=96.0), 98.5, at(9, 40), "SHORT")
    assert v["would_enter"] is False and v["reasons"][:2] == ["above_range_low", "above_vwap"]


def test_close_below_the_low_is_the_short_trigger_and_volume_is_compared(monkeypatch):
    setup(monkeypatch, range_bars(1000) + [bar(9, 20, 98.5, 98.6, 97.9, 98.2, 500), bar(9, 21, 98.2, 98.2, 97.1, 97.4, 3000)])
    v = orb.evaluate("ABC", q(), 97.0, at(9, 40), "SHORT")
    assert v["close_trigger"] is True and v["trigger_bar_at"] == "09:21" and v["trigger_close"] == 97.4
    assert v["trigger_volume"] == 3000 and v["volume_ratio"] == 3.0


def test_a_wick_below_the_low_that_closes_inside_is_not_a_short_trigger(monkeypatch):
    setup(monkeypatch, range_bars() + [bar(9, 20, 98.5, 98.6, 96.5, 98.4, 9000)])
    v = orb.evaluate("ABC", q(), 97.0, at(9, 40), "SHORT")
    assert v["close_trigger"] is False and "volume_ratio" not in v


def test_a_close_above_the_high_is_not_a_short_trigger_but_is_a_long_one(monkeypatch):
    setup(monkeypatch, range_bars() + [bar(9, 20, 99.5, 101.2, 99.4, 101.0, 2000)])
    assert orb.evaluate("ABC", q(), 97.0, at(9, 40), "SHORT")["close_trigger"] is False
    assert orb.evaluate("ABC", q(price=101.0, day_high=101.5), 101.0, at(9, 40))["close_trigger"] is True


def test_long_record_is_unchanged(monkeypatch):
    setup(monkeypatch, range_bars())
    v = orb.evaluate("ABC", q(price=101.0, avg_price=99.5), 101.0, at(9, 40))
    assert "side" not in v and v["would_enter"] is True and v["stop_price"] == 98.0
    assert v["limit_price"] == round(100.0 * (1 + config.ORB_LIMIT_BUFFER_PCT / 100.0), 2)


def test_attach_writes_the_short_record_and_keeps_other_flow_keys(monkeypatch):
    setup(monkeypatch, range_bars())
    import json
    out = json.loads(orb.attach('{"odds":{"x":1}}', "ABC", q(), 97.0, at(9, 40), side="SHORT"))
    assert out["odds"] == {"x": 1} and out["orb"]["side"] == "SHORT"


def test_the_live_short_row_gets_the_mirrored_record_and_it_cannot_block_the_entry():
    src = (ROOT / "orders" / "short_live.py").read_text()
    i = src.index("_orb.attach(pos.flow_json")
    assert 'side="SHORT"' in src[i:i + 200]
    block = src[src.rfind("try:", 0, i):src.index("tm_gate.stamp(pos.flow_json", i)]
    assert "except Exception" in block                       # record only: a failure is swallowed before the position is saved
    assert "ScalpPosition(" in src[:i] and "db.add(pos)" in src[i:]
    assert "orb_shadow" not in (ROOT / "orders" / "short_entry.py").read_text()   # the shadow-only path is untouched
