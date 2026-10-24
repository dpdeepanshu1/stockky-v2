"""E126: intraday_state_check.py (T5 / T7 / overlay-clear) with fake HTTP answers. Run: python3 -m pytest scripts/test_intraday_state_check.py -q"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import intraday_state_check as C  # noqa: E402

RT, PS, MD = "http://rt", "http://ps", "http://md"
TUE = lambda h, m: datetime(2026, 10, 27, h, m, tzinfo=C.IST)     # a Tuesday
SAT = datetime(2026, 10, 24, 15, 5, tzinfo=C.IST)


def fake(routes):
    def call(url):
        for suffix, ans in routes.items():
            if url.endswith(suffix):
                return ans
        return 0, "refused"
    return call


def go(phase, routes, now, expect="any"):
    return C.run(phase, fake({"/market/session": (200, {"calendar_holiday": False}), **routes}), RT, PS, MD, expect, now)


SHADOW = {"mode": "shadow", "total": 3, "open": 1, "closed": 2, "net_pnl": 5.0, "gate_counters": {"a": 4, "b": 0},
          "s2_progress": {"would_be_shorts": 2, "needed": 100}, "sell_probe": {"verdict": "NOT_RUN"}}
HEALTH = {"orb": {"bars": {"hit": 5, "miss": 1, "stale": 0}}}


def test_t5_pass_and_lines():
    code, out = go("shadow-shorts", {"/short/shadow": (200, SHADOW), "/health": (200, HEALTH)}, TUE(9, 35))
    assert code == 0 and out[-1] == "SHORT SHADOW (T5): PASS"
    assert any("SHORT_MODE = shadow" in x for x in out) and any("hit 5" in x for x in out) and any("probe has not been run" in x for x in out)


def test_t5_fails_when_unreadable_or_wrong_mode_or_no_counters():
    code, out = go("shadow-shorts", {"/health": (200, HEALTH)}, TUE(9, 35))
    assert code == 1 and "FAIL - position-stocks GET /short/shadow" in out[-1]
    code, out = go("shadow-shorts", {"/short/shadow": (200, SHADOW), "/health": (200, HEALTH)}, TUE(9, 35), expect="live")
    assert code == 1 and "expected live" in out[-1]
    code, out = go("shadow-shorts", {"/short/shadow": (200, {"mode": "shadow"}), "/health": (200, HEALTH)}, TUE(9, 35))
    assert code == 1 and "gate_counters" in out[-1]
    code, out = go("shadow-shorts", {"/short/shadow": (200, dict(SHADOW, mode="weird")), "/health": (200, HEALTH)}, TUE(9, 35))
    assert code == 1


def test_t5_real_trade_down_fails_and_missing_bars_only_warns():
    code, out = go("shadow-shorts", {"/short/shadow": (200, SHADOW)}, TUE(9, 35))
    assert code == 1 and "real-trade GET /health" in out[-1]
    code, out = go("shadow-shorts", {"/short/shadow": (200, SHADOW), "/health": (200, {})}, TUE(9, 35))
    assert code == 0 and any(x.startswith("WARN") and "orb.bars" in x for x in out)
    code, out = go("shadow-shorts", {"/short/shadow": (200, SHADOW), "/health": (200, {"orb": {"bars": {"hit": 0, "miss": 12, "stale": 0}}})}, TUE(9, 35))
    assert code == 0 and any("every bar lookup missed" in x for x in out)


def test_t7_flat_passes_and_open_short_fails():
    base = {"/short/plain": (200, {"orphans_watched": 0}), "/short/shadow": (200, dict(SHADOW, open=0))}
    code, out = go("shorts-flat", {"/positions": (200, [{"symbol": "ITC", "side": "LONG", "status": "OPEN"}, {"symbol": "SBIN", "side": "SHORT", "status": "TARGET"}]), **base}, TUE(15, 5))
    assert code == 0 and out[-1] == "SHORTS FLAT (T7): PASS"
    code, out = go("shorts-flat", {"/positions": (200, [{"symbol": "ONGC", "side": "SHORT", "status": "OPEN", "quantity": 1}]), **base}, TUE(15, 5))
    assert code == 1 and "ONGC x1" in out[-1] and "Check Dhan" in out[-1]
    code, out = go("shorts-flat", {"/positions": (200, {"positions": [{"symbol": "NTPC", "side": "short", "status": "EXIT_LEGS_REJECTED"}]}), **base}, TUE(15, 5))
    assert code == 1 and "NTPC" in out[-1]


def test_t7_orphan_unreadable_and_stuck_shadow():
    ok = {"/positions": (200, [])}
    code, out = go("shorts-flat", {**ok, "/short/plain": (200, {"orphans_watched": 2}), "/short/shadow": (200, dict(SHADOW, open=0))}, TUE(15, 5))
    assert code == 1 and "plain-order shorts under watch: 2" in out[-1]
    code, out = go("shorts-flat", {"/short/plain": (200, {"orphans_watched": 0})}, TUE(15, 5))
    assert code == 1 and "cannot prove" in out[-1]
    code, out = go("shorts-flat", {**ok, "/short/plain": (500, "x"), "/short/shadow": (200, dict(SHADOW, open=2))}, TUE(15, 5))
    assert code == 0 and sum(1 for x in out if x.startswith("WARN")) == 2


def test_overlay_clear_after_1530():
    clear = {"/control/overlay": (200, {"active": None, "overlays": []})}
    code, out = go("overlay-clear", clear, TUE(15, 35))
    assert code == 0 and out[-1] == "OVERLAY CLEAR: PASS" and sum(1 for x in out if x.startswith("OK")) == 2
    held = {"/control/overlay": (200, {"active": {"state": "stressed", "until": "15:30"}, "overlays": [{"key": "K"}]})}
    code, out = go("overlay-clear", held, TUE(15, 35))
    assert code == 1 and "STILL active after 15:30" in out[-1]
    code, out = go("overlay-clear", held, TUE(11, 0))
    assert code == 0 and any("has not passed yet" in x for x in out)
    code, out = go("overlay-clear", {"/control/overlay": (401, "no")}, TUE(15, 35))
    assert code == 1 and "answered 401" in out[-1]
    code, out = go("overlay-clear", {"/control/overlay": (200, "text")}, TUE(15, 35))
    assert code == 1


def test_holiday_and_weekend_are_skipped_and_unknown_session_fails_open():
    call = fake({"/market/session": (200, {"calendar_holiday": True})})
    code, out = C.run("shorts-flat", call, RT, PS, MD, "any", TUE(15, 5))
    assert code == 0 and "SKIPPED - exchange holiday" in out[-1]
    code, out = C.run("overlay-clear", fake({}), RT, PS, MD, "any", SAT)
    assert code == 0 and "SKIPPED - weekend" in out[-1]
    code, out = C.run("overlay-clear", fake({"/control/overlay": (200, {"overlays": []})}), RT, PS, MD, "any", TUE(15, 35))     # market-data down: the checks still run
    assert code == 0 and out[-1].endswith("PASS")


def test_main_exit_codes(capsys):
    call = fake({"/market/session": (200, {}), "/control/overlay": (200, {"overlays": []})})
    assert C.main(["--phase", "overlay-clear"], call=call, now=TUE(15, 40)) == 0
    assert "OVERLAY CLEAR: PASS" in capsys.readouterr().out
    assert C.main(["--phase", "nonsense"]) == 2
