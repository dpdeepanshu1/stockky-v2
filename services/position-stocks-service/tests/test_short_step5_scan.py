"""Short selling step 5: screening.engine.scan_shorts() - the mirror of scan(). Feeds the real engine through the ws_client accessors it reads."""
import time

from feed import ws_client
from screening import engine


def _feed(monkeypatch, series, day_range=None, volume=1_000_000):
    now = time.time()
    bufs = {s: [(now - age, px) for age, px in pts] for s, pts in series.items()}
    monkeypatch.setattr(ws_client, "_token_to_symbol", {str(i): s for i, s in enumerate(series)})
    monkeypatch.setattr(ws_client, "get_tick_buffer", lambda s: list(bufs.get(s, [])))
    monkeypatch.setattr(ws_client, "get_last_volume", lambda s: volume)
    monkeypatch.setattr(ws_client, "get_best_bid_ask", lambda s: None)
    monkeypatch.setattr(ws_client, "get_day_range", lambda s: (day_range or {}).get(s))


def _falling(start=100.0, end=98.2):
    pts = [(4200 - i * 60, start) for i in range(0, 64)]                          # flat for the first hour
    pts += [(360 - i * 60, start + (end - start) * i / 6.0) for i in range(0, 7)]  # then a steady 6-minute slide
    return pts


def _rising(start=100.0, end=101.8):
    return _falling(start, end)


def test_a_falling_stock_is_a_short_candidate_with_a_negative_pct_change(monkeypatch):
    _feed(monkeypatch, {"FALL": _falling()})
    out = engine.scan_shorts()
    assert out and all(c.symbol == "FALL" for c in out)
    assert all(c.pct_change < 0 and c.composite_score > 0 for c in out)
    assert out == sorted(out, key=lambda c: c.composite_score, reverse=True)


def test_a_rising_stock_is_never_a_short_candidate(monkeypatch):
    _feed(monkeypatch, {"RISE": _rising()})
    assert engine.scan_shorts() == []


def test_the_long_scan_is_not_fed_a_faller(monkeypatch):
    _feed(monkeypatch, {"FALL": _falling()})
    assert [c for c in engine.scan() if c.symbol == "FALL"] == []


def test_a_fall_below_every_threshold_is_ignored(monkeypatch):
    _feed(monkeypatch, {"FLAT": _falling(100.0, 99.9)})
    assert engine.scan_shorts() == []


def test_thin_volume_is_ignored(monkeypatch):
    _feed(monkeypatch, {"FALL": _falling()}, volume=1)
    assert engine.scan_shorts() == []


def test_held_symbols_are_left_out(monkeypatch):
    _feed(monkeypatch, {"FALL": _falling(), "FALL2": _falling()})
    assert {c.symbol for c in engine.scan_shorts({"FALL.NS"})} == {"FALL2"}


def test_near_the_day_low_scores_lower_than_near_the_high(monkeypatch):
    _feed(monkeypatch, {"LOW": _falling(), "HIGH": _falling()}, day_range={"LOW": (98.1, 103.0), "HIGH": (90.0, 98.3)})
    by = {}
    for c in engine.scan_shorts():
        by[c.symbol] = max(by.get(c.symbol, 0.0), c.composite_score)
    assert by["LOW"] < by["HIGH"]


def test_a_bad_symbol_does_not_stop_the_scan(monkeypatch):
    _feed(monkeypatch, {"FALL": _falling(), "BAD": _falling()})
    real = ws_client.get_tick_buffer

    def _tb(s):
        if s == "BAD":
            raise RuntimeError("x")
        return real(s)
    monkeypatch.setattr(ws_client, "get_tick_buffer", _tb)
    assert {c.symbol for c in engine.scan_shorts()} == {"FALL"}
