"""E377: between 15:30 and the bar-close buffer the structure frame still drops today's bar, so results must not get the 6 h closed TTL.
The helper functions of technical/main.py are exec'd without fastapi."""
from __future__ import annotations

import datetime as _dt
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(HERE, "technical", "main.py"), encoding="utf-8").read()


def _ns(monkeypatch, y, m, d, hh, mm):
    real = _dt.datetime(y, m, d, hh, mm, tzinfo=_dt.timezone(_dt.timedelta(hours=5, minutes=30)))

    class FakeDT:
        @staticmethod
        def now(tz=None):
            return real

    a = SRC.index("def is_market_open")
    b = SRC.index("# A \"history too thin\"")
    c = SRC.index("def _bar_close_buffer_min")
    e = SRC.index("def _drop_forming_bar")
    hol = SRC[SRC.index("_NSE_HOLIDAYS_2026"):a]
    ns = {"datetime": FakeDT, "dtime": _dt.time, "timedelta": _dt.timedelta, "ZoneInfo": lambda n: None, "os": os}
    exec(hol, ns)
    exec(SRC[c:e], ns)
    exec(SRC[a:b], ns)
    ns["is_market_open"] = lambda: _dt.time(9, 15) <= real.time() <= _dt.time(15, 30) and real.weekday() < 5 and real.strftime("%Y-%m-%d") not in ns["_NSE_HOLIDAYS_2026"]
    return ns


def test_ttl_short_inside_close_buffer(monkeypatch):
    monkeypatch.delenv("STRUCTURE_BAR_CLOSE_BUFFER_MIN", raising=False)
    ns = _ns(monkeypatch, 2026, 9, 29, 15, 35)        # Tuesday, 15 min before the 15:50 bar-close
    assert ns["get_cache_ttl"]() == 15 * 60 + 5


def test_ttl_long_after_buffer_and_off_days(monkeypatch):
    monkeypatch.delenv("STRUCTURE_BAR_CLOSE_BUFFER_MIN", raising=False)
    assert _ns(monkeypatch, 2026, 9, 29, 15, 50)["get_cache_ttl"]() == 21600
    assert _ns(monkeypatch, 2026, 9, 26, 15, 35)["get_cache_ttl"]() == 21600      # Saturday
    assert _ns(monkeypatch, 2026, 10, 2, 15, 35)["get_cache_ttl"]() == 21600      # NSE holiday
    assert _ns(monkeypatch, 2026, 9, 29, 11, 0)["get_cache_ttl"]() == 300


def test_buffer_zero_keeps_old_behaviour(monkeypatch):
    monkeypatch.setenv("STRUCTURE_BAR_CLOSE_BUFFER_MIN", "0")
    assert _ns(monkeypatch, 2026, 9, 29, 15, 35)["get_cache_ttl"]() == 21600


def test_structure_cache_sig_includes_params_override():
    """E378: the cached structure block is keyed on frame + flags only; a changed STRUCTURE_PARAMS_JSON must invalidate it."""
    assert 'STRUCTURE_PARAMS_JSON") or "").strip()]' in SRC.split("def _structure_cached")[1].split("def _attach_structure")[0]
