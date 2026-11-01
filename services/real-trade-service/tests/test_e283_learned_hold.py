"""E283: the swing hold learned from the ledger's closed winners (swing/learned_hold.py). Fixed rows, no database."""
import re
import sys
import types
from pathlib import Path

import config
from swing import learned_hold as lh

ROOT = Path(__file__).resolve().parent.parent


def rows(sessions_net):
    return [{"sessions": s, "net_pct": n, "status": "CLOSED"} for s, n in sessions_net]


def ledger(n_win, win_sessions, n_loss, loss_sessions=5):
    return rows([(win_sessions[i % len(win_sessions)], 1.0) for i in range(n_win)] + [(loss_sessions, -1.0)] * n_loss)


def test_under_thirty_trades_or_ten_winners_keeps_the_constant():
    assert lh.learned_days(ledger(9, [2], 25), 5) is None            # 34 trades, 9 winners
    assert lh.learned_days(ledger(15, [2], 14), 5) is None           # 29 trades
    assert lh.learned_days([], 5) is None


def test_learned_hold_is_shorter_shrunk_and_rounded_up():
    r = lh.learned_days(ledger(20, [2, 3, 3, 2], 20), 5)             # n 40, winners sit at 2..3 sessions: p75 = 3
    assert r["learned"] == 3 and r["n"] == 40 and r["winners"] == 20
    # w = 40 / 70 = 0.571; 0.571 x 3 + 0.429 x 5 = 3.857 -> ceil 4
    assert r["days"] == 4


def test_never_longer_than_the_constant_and_none_when_not_shorter():
    assert lh.learned_days(ledger(20, [5], 20), 5) is None            # winners need all 5 sessions: nothing shorter
    assert lh.learned_days(ledger(20, [9, 9], 20), 5) is None         # never longer


def test_minimum_days_floor():
    r = lh.learned_days(ledger(40, [1], 200), 5, min_days=3)          # n 240: w 0.89, 0.89 x 1 + 0.11 x 5 = 1.44 -> 2 -> floor 3
    assert r["days"] == 3
    assert lh.learned_days(ledger(40, [1], 200), 5, min_days=1)["days"] == 2


def test_unreadable_rows_are_skipped():
    bad = [{"sessions": None, "net_pct": 1.0}, {"sessions": 0, "net_pct": 1.0}, {"sessions": "x", "net_pct": 1.0}, {"sessions": 2, "net_pct": None}, {}]
    assert lh.learned_days(bad + ledger(20, [2], 20), 5)["n"] == 40


def test_hold_days_switch_off_no_db_and_error(monkeypatch):
    lh.reset()
    monkeypatch.setattr(config, "SWING_MAX_HOLD_DAYS", 5, raising=False)
    monkeypatch.setattr(config, "SWING_LEARNED_HOLD_ENABLED", False, raising=False)
    assert lh.hold_days(object()) == 5
    monkeypatch.setattr(config, "SWING_LEARNED_HOLD_ENABLED", True, raising=False)
    assert lh.hold_days(None) == 5
    assert lh.hold_days(object(), now_ts=1.0) == 5                    # SqlRepo(object()) fails or reads nothing: the constant stays
    lh.reset()


def test_hold_days_reads_the_ledger_and_caches(monkeypatch):
    lh.reset()
    monkeypatch.setattr(config, "SWING_MAX_HOLD_DAYS", 5, raising=False)
    monkeypatch.setattr(config, "SWING_LEARNED_HOLD_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "SWING_LEARNED_HOLD_MIN_DAYS", 3, raising=False)
    calls = []

    class Repo:
        def __init__(self, db): pass
        def trades(self, **kw):
            calls.append(kw)
            return ledger(20, [2, 3, 3, 2], 20)

    fake = types.ModuleType("swing.repo")
    fake.SqlRepo = Repo
    import swing
    monkeypatch.setitem(sys.modules, "swing.repo", fake)
    monkeypatch.setattr(swing, "repo", fake, raising=False)
    assert lh.hold_days(object(), now_ts=100.0) == 4
    assert lh.hold_days(object(), now_ts=200.0) == 4 and len(calls) == 1 and calls[0] == {"status": "CLOSED"}      # cached
    assert lh.hold_days(object(), now_ts=100.0 + lh.CACHE_S + 1) == 4 and len(calls) == 2
    monkeypatch.setattr(config, "SWING_MAX_HOLD_DAYS", 3, raising=False)
    assert lh.hold_days(object(), now_ts=100.0 + lh.CACHE_S + 2) == 3  # the constant changed: learned 4 would not be shorter, the constant is used
    lh.reset()


def test_wiring_switches_and_shadow_lane_untouched():
    live = (ROOT / "swing" / "live.py").read_text(encoding="utf-8")
    assert live.count("hold_days(") >= 5 and "SWING_MAX_HOLD_DAYS\", 5)) + have" not in live
    eng = (ROOT / "swing" / "engine.py").read_text(encoding="utf-8")
    assert "learned_hold" not in eng and 'hold = int(_cfg("SWING_MAX_HOLD_DAYS", 5))' in eng        # the shadow lane keeps the constant
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    for k in ("SWING_LEARNED_HOLD_ENABLED", "SWING_LEARNED_HOLD_MIN_DAYS"):
        assert f'Flag("{k}"' in cat and f'"{k}":' in auto and re.search(rf"^{k} = ", cfg, re.M)
