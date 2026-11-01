"""E273 round 10: the pre-open gap block (PREOPEN_GAP_UP / PREOPEN_GAP_DOWN) is a SIZE PENALTY in the Position Stocks entry, like the round 9 soft gates.
penalty = gap / the limit that fired (the stock's own ATR limit or the fixed %), capped at SOFT_GATE_MAX_PENALTY; gap_reason() is unchanged (the veto path)."""
import pathlib
import sys
from types import SimpleNamespace as NS

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402
from screening import preopen, prev_day  # noqa: E402


def _env(mp, gap, atr=None, up=6.0, dn=3.0, mult=1.5, floor=1.5, until_passed=False):
    mp.setattr(config, "PREOPEN_SNAPSHOT_ENABLED", True, raising=False)
    mp.setattr(config, "PREOPEN_GAP_UP_BLOCK_PCT", up, raising=False)
    mp.setattr(config, "PREOPEN_GAP_DOWN_BLOCK_PCT", dn, raising=False)
    mp.setattr(config, "PREOPEN_GAP_ATR_MULT", mult, raising=False)
    mp.setattr(config, "PREOPEN_GAP_ATR_FLOOR_PCT", floor, raising=False)
    mp.setattr(config, "PREOPEN_GAP_GATE_UNTIL_IST", "10:00", raising=False)
    mp.setattr(config, "SOFT_GATE_MAX_PENALTY", 3.0, raising=False)
    mp.setattr(preopen, "ist_today_str", lambda now=None: "2026-10-26")
    mp.setattr(preopen, "ist_time_at_or_after", lambda t, now=None: until_passed)
    mp.setitem(preopen._snap, "day", "2026-10-26")
    mp.setitem(preopen._snap, "gaps", {"AAA": gap})
    mp.setattr(prev_day, "get", lambda sym: (NS(atr_pct=atr) if atr is not None else None))


def test_penalty_is_the_gap_over_the_atr_limit(monkeypatch):
    _env(monkeypatch, gap=3.6, atr=1.2)                 # limit 1.5 x 1.2 = 1.8 (floor 1.5): 3.6 / 1.8 = 2.0
    got = preopen.gap_penalty("AAA")
    assert got and got["why"].startswith("PREOPEN_GAP_UP") and got["penalty"] == 2.0
    assert preopen.gap_reason("AAA") == got["why"]      # the veto path reads the same decision


def test_fixed_percent_limit_and_cap(monkeypatch):
    _env(monkeypatch, gap=9.0, atr=9.0)                 # ATR limit 13.5 > fixed 6: the fixed 6 % fires, 9 / 6 = 1.5
    got = preopen.gap_penalty("AAA")
    assert got["penalty"] == 1.5 and "ATR" not in got["why"]
    _env(monkeypatch, gap=30.0, atr=9.0)
    assert preopen.gap_penalty("AAA")["penalty"] == 3.0     # capped


def test_gap_down_is_scored_on_its_size(monkeypatch):
    _env(monkeypatch, gap=-4.5, atr=None)               # fixed down limit 3.0: 4.5 / 3.0 = 1.5
    got = preopen.gap_penalty("AAA")
    assert got["why"].startswith("PREOPEN_GAP_DOWN") and got["penalty"] == 1.5


def test_no_penalty_inside_the_limit_after_the_window_without_data_or_switch(monkeypatch):
    _env(monkeypatch, gap=1.0, atr=3.0)
    assert preopen.gap_penalty("AAA") is None
    _env(monkeypatch, gap=9.0, atr=None, until_passed=True)
    assert preopen.gap_penalty("AAA") is None and preopen.gap_scored("AAA") == (None, None)      # gate window over
    _env(monkeypatch, gap=9.0)
    assert preopen.gap_penalty("BBB") is None                                                     # no gap recorded for the symbol
    monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_ENABLED", False, raising=False)
    assert preopen.gap_penalty("AAA") is None


def test_entry_wires_the_gap_penalty_before_the_total_check():
    src = (ROOT / "orders" / "entry.py").read_text(encoding="utf-8")
    assert '_penalties["preopen_gap"]' in src and "preopen.gap_penalty(candidate.symbol)" in src
    _gp = src.index('_penalties["preopen_gap"]')           # E273 round 12 added an earlier total check for veto mode; this one is the total AFTER the gap penalty
    assert _gp < src.index("soft_gates.total_skip_reason(_penalties", _gp) < src.index("setup_live.classify_long(")
    assert "_gap_reject = None if _soft else preopen.gap_reason(" in src                          # veto mode keeps the old refusal
