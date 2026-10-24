"""E103 (P2 item 13): orders/trade_odds.py - the odds of a trade's own stop / target, record only. Stdlib only (runs without SQLAlchemy)."""
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from orders import trade_odds as to  # noqa: E402
from feed import bar_store  # noqa: E402


def bar(t, o, h, l, c):
    return (float(t * 60), o, h, l, c, 1000.0)


def zigzag(n, start=100.0, step=0.5):
    """n bars alternating up / down by `step` around start: a +/-1% level is never reached."""
    out, px = [], start
    for i in range(n):
        nxt = px + (step if i % 2 == 0 else -step)
        out.append(bar(i, px, max(px, nxt), min(px, nxt), nxt)); px = nxt
    return out


def falling(n, start=100.0, drop=0.5):
    return [bar(i, start - i * drop, start - i * drop, start - (i + 1) * drop, start - (i + 1) * drop) for i in range(n)]


def rising(n, start=100.0, rise=0.5):
    return [bar(i, start + i * rise, start + (i + 1) * rise, start + i * rise, start + (i + 1) * rise) for i in range(n)]


def test_long_in_a_falling_market_loses_and_short_wins():
    bars = falling(60)
    lg, sh = to.simulate(bars, "LONG", 1.0, 2.0), to.simulate(bars, "SHORT", 1.0, 2.0)
    assert lg["target"] == 0 and lg["stop"] > 0
    assert sh["stop"] == 0 and sh["target"] > 0


def test_long_in_a_rising_market_wins_and_short_loses():
    bars = rising(60)
    assert to.simulate(bars, "LONG", 1.0, 2.0)["stop"] == 0
    assert to.simulate(bars, "SHORT", 1.0, 2.0)["target"] == 0


def test_a_bar_touching_both_levels_counts_as_the_stop():
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 103, 98, 100), bar(2, 100, 100, 100, 100)]
    assert to.simulate(bars, "LONG", 1.0, 2.0) == {"target": 0, "stop": 1, "open": 1}
    assert to.simulate(bars, "SHORT", 1.0, 2.0) == {"target": 0, "stop": 1, "open": 1}


def test_the_last_bar_is_never_an_entry_and_unresolved_entries_are_open():
    r = to.simulate(zigzag(10), "LONG", 1.0, 2.0)
    assert r == {"target": 0, "stop": 0, "open": 9}


def test_exact_touch_counts():
    bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 102, 100, 102)]
    assert to.simulate(bars, "LONG", 1.0, 2.0)["target"] == 1


def test_bad_bars_are_dropped_and_nothing_raises():
    bars = [None, (1,), bar(0, 100, 99, 101, 100), bar(1, 100, "x", 100, 100), bar(2, 0, 0, 0, 0)] + falling(5)
    assert sum(to.simulate(bars, "LONG", 1.0, 2.0).values()) == 4


def test_evaluate_needs_levels_and_bars():
    assert to.evaluate([], "LONG", 1.0, 2.0) is None
    assert to.evaluate(falling(60), "LONG", 0, 2.0) is None
    assert to.evaluate(falling(60), "LONG", 1.0, None) is None
    assert to.evaluate(falling(1), "LONG", 1.0, 2.0) is None


def test_too_few_resolved_stores_counts_only():
    v = to.evaluate(falling(10), "SHORT", 1.0, 2.0)
    assert "p_target" not in v and v["n_resolved"] < to.MIN_RESOLVED and "not enough bars" in v["verdict"]


def test_probabilities_break_even_and_average_r():
    v = to.evaluate(falling(80), "SHORT", 1.0, 2.0)
    assert v["p_target"] == 1.0 and v["p_stop"] == 0.0
    assert abs(v["be_win"] - 1 / 3) < 1e-3 and abs(v["rr"] - 2.0) < 1e-9
    assert abs(v["avg_r"] - 2.0) < 1e-9 and "ABOVE break-even" in v["verdict"]
    w = to.evaluate(falling(80), "LONG", 1.0, 2.0)
    assert w["p_target"] == 0.0 and abs(w["avg_r"] + 1.0) < 1e-9 and "BELOW break-even" in w["verdict"]


def test_near_break_even_wording(monkeypatch):
    bars = falling(5)
    monkeypatch.setattr(to, "simulate", lambda *a, **k: {"target": 34, "stop": 66, "open": 5})
    v = to.evaluate(bars, "LONG", 1.0, 2.0)
    assert "near break-even" in v["verdict"] and v["p_open"] == round(5 / 105, 4)
    monkeypatch.setattr(to, "simulate", lambda *a, **k: {"target": 50, "stop": 50, "open": 0})
    assert "ABOVE break-even" in to.evaluate(bars, "LONG", 1.0, 2.0)["verdict"]
    monkeypatch.setattr(to, "simulate", lambda *a, **k: {"target": 20, "stop": 80, "open": 0})
    assert "BELOW break-even" in to.evaluate(bars, "LONG", 1.0, 2.0)["verdict"]


def test_side_is_recorded_and_unknown_side_is_a_long():
    assert to.evaluate(falling(60), "short", 1.0, 2.0)["side"] == "SHORT"
    assert to.evaluate(falling(60), "weird", 1.0, 2.0)["side"] == "LONG"


def _feed(sym, bars):
    bar_store.reset()
    with bar_store._lock:
        bar_store._bars[sym] = __import__("collections").deque([list(b) for b in bars])


def test_attach_merges_under_odds_and_keeps_other_keys(monkeypatch):
    monkeypatch.setattr(config, "TRADE_ODDS_MODE", "shadow", raising=False)
    to._reset_for_tests(); _feed("ABC", falling(60))
    out = to.attach(json.dumps({"orb": {"x": 1}, "spread": 0.1}), "abc", "SHORT", 1.0, 2.0)
    d = json.loads(out)
    assert d["orb"] == {"x": 1} and d["spread"] == 0.1 and d["odds"]["side"] == "SHORT" and d["odds"]["p_target"] == 1.0
    assert to.stats()["recorded"] == 1


def test_attach_with_no_flow_creates_it(monkeypatch):
    monkeypatch.setattr(config, "TRADE_ODDS_MODE", "shadow", raising=False)
    _feed("ABC", falling(60))
    assert "odds" in json.loads(to.attach(None, "ABC", "LONG", 1.0, 2.0))


def test_attach_leaves_input_alone_when_off_no_bars_or_bad_levels(monkeypatch):
    _feed("ABC", falling(60))
    monkeypatch.setattr(config, "TRADE_ODDS_MODE", "off", raising=False)
    assert to.attach("keep", "ABC", "LONG", 1.0, 2.0) == "keep"
    monkeypatch.setattr(config, "TRADE_ODDS_MODE", "shadow", raising=False)
    assert to.attach("keep", "NOPE", "LONG", 1.0, 2.0) == "keep"
    assert to.attach(None, "ABC", "LONG", 0.0, 2.0) is None
    assert to.attach("{bad json", "ABC", "LONG", 1.0, 2.0) is not None      # unreadable flow text is replaced by a fresh record


def test_attach_never_raises(monkeypatch):
    monkeypatch.setattr(config, "TRADE_ODDS_MODE", "shadow", raising=False)
    monkeypatch.setattr(bar_store, "get_bars", lambda s: (_ for _ in ()).throw(RuntimeError("boom")))
    assert to.attach("keep", "ABC", "LONG", 1.0, 2.0) == "keep"
    assert to.stats()["errors"] >= 1


def test_wiring_rails():
    ent = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    assert 'trade_odds.attach(_flow_snap, candidate.symbol, "LONG"' in ent
    assert ent.index("trade_odds.attach") < ent.index("flow_json=_flow_snap")
    sl = (ROOT / "orders" / "short_live.py").read_text(encoding="utf-8")
    assert "flow_json=trade_odds.attach(None, sym, SIDE," in sl
    src = (ROOT / "orders" / "trade_odds.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(sqlalchemy|requests|httpx)", src, re.M)      # recorder: no database, no network
    for banned in ("place_order", "dhan_client", "reserve_capital", "return False"):
        assert banned not in src
