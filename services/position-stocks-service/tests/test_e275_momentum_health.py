"""E275: pure momentum_health.decide (5 signals, long / short mirror, 3 actions) + wiring of the position-stocks hook."""
import pathlib

from orders import momentum_health as mh

T0 = 1_000_000.0
TAPE_DELTA_AGAINST = {"available": True, "reliable": True, "checks": {"delta": "against"}}
TAPE_OK = {"available": True, "reliable": True, "checks": {"delta": "for"}}


def bar(i, o, h, l, c, v=1000.0):
    return (T0 + 60.0 * i, o, h, l, c, v)


def flip(bars, pivot=100.0):
    """Mirror a long scenario around `pivot`: prices p -> 2*pivot - p, high and low swap."""
    return [(ts, 2 * pivot - o, 2 * pivot - l, 2 * pivot - h, 2 * pivot - c, v) for ts, o, h, l, c, v in bars]


def rising(n=8, start=100.0, step=0.1):
    out = []
    for i in range(n):
        p = start + step * i
        out.append(bar(i, p, p + 0.08, p - 0.02, p + 0.06))
    return out


def forming(bars):
    """The caller's last bar is still forming and ignored: append a throwaway one."""
    last = bars[-1]
    return list(bars) + [bar(len(bars), last[4], last[4] + 0.01, last[4] - 0.01, last[4], 10.0)]


BASE = dict(entry=100.0, stop=99.0, target=102.0, ltp=100.8, qty=10, tick=0.05)


def test_healthy_trade_holds():
    d = mh.decide(side="LONG", bars=forming(rising()), age_min=8, tape=TAPE_OK, fading=False, **BASE)
    assert d["action"] == "HOLD" and d["count"] == 0 and d["reason"] == "healthy"


def test_exhaustion_needs_new_best_and_adverse_delta():
    bars = forming(rising())
    on = mh.decide(side="LONG", bars=bars, tape=TAPE_DELTA_AGAINST, **BASE)
    assert "exhaustion" in on["signals"]
    assert "exhaustion" not in mh.decide(side="LONG", bars=bars, tape=TAPE_OK, **BASE)["signals"]
    assert "exhaustion" not in mh.decide(side="LONG", bars=bars, tape={**TAPE_DELTA_AGAINST, "reliable": False}, **BASE)["signals"]
    assert "exhaustion" not in mh.decide(side="LONG", bars=bars, tape=None, **BASE)["signals"]


def test_no_new_best_after_the_setups_time():
    up = rising(6)
    bars = up
    for i in range(6, 30):                                                          # 24 minutes under the peak
        bars.append(bar(i, 100.5, 100.55, 100.45, 100.5))
    d = mh.decide(side="LONG", bars=forming(bars), age_min=30, no_new_best_min=20, **{**BASE, "ltp": 100.5})
    assert "no_new_best" in d["signals"]
    assert "no_new_best" not in mh.decide(side="LONG", bars=forming(bars), age_min=30, no_new_best_min=40, **{**BASE, "ltp": 100.5})["signals"]
    assert "no_new_best" not in mh.decide(side="LONG", bars=forming(bars), age_min=30, no_new_best_min=None, **{**BASE, "ltp": 100.5})["signals"]


def test_climax_bar_volume_and_upper_wick():
    bars = rising(8)
    bars.append(bar(8, 100.8, 101.4, 100.78, 100.82, 5000.0))                      # 5x volume, wick 0.58 of a 0.62 range
    d = mh.decide(side="LONG", bars=forming(bars), **BASE)
    assert "climax_bar" in d["signals"]
    no_vol = rising(8) + [bar(8, 100.8, 101.4, 100.78, 100.82, 1200.0)]
    assert "climax_bar" not in mh.decide(side="LONG", bars=forming(no_vol), **BASE)["signals"]
    no_wick = rising(8) + [bar(8, 100.8, 101.4, 100.78, 101.38, 5000.0)]
    assert "climax_bar" not in mh.decide(side="LONG", bars=forming(no_wick), **BASE)["signals"]


def test_at_wall_one_tick_or_typical_gain():
    bars = forming(rising())
    d = mh.decide(side="LONG", bars=bars, tape={**TAPE_OK, "hvn_above": 100.85}, **BASE)
    assert "at_wall" in d["signals"]
    far = mh.decide(side="LONG", bars=bars, tape={**TAPE_OK, "hvn_above": 101.5}, **BASE)
    assert "at_wall" not in far["signals"]
    typ = mh.decide(side="LONG", bars=bars, tape=TAPE_OK, typical_gain_pct=0.85, **BASE)       # 0.8 % gain >= 0.9 x 0.85 %
    assert "at_wall" in typ["signals"]


def test_lost_level_needs_a_trade_that_worked():
    bars = forming(rising())
    assert "lost_level" in mh.decide(side="LONG", bars=bars, fading=True, **BASE)["signals"]
    assert "lost_level" in mh.decide(side="LONG", bars=bars, fading=None, trigger_level=101.0, **BASE)["signals"]
    never_worked = [bar(i, 99.9, 99.95, 99.7, 99.8) for i in range(8)]
    d = mh.decide(side="LONG", bars=forming(never_worked), fading=True, **{**BASE, "ltp": 99.8})
    assert "lost_level" not in d["signals"]


def test_two_signals_tighten_stop_and_cut_target():
    bars = forming(rising())
    d = mh.decide(side="LONG", bars=bars, tape={**TAPE_DELTA_AGAINST, "hvn_above": 101.2}, fading=True, **BASE)
    assert d["action"] == "TIGHTEN" and d["count"] >= 2 and d["book_qty"] == 0
    assert BASE["stop"] < d["new_stop"] < BASE["ltp"]
    assert d["new_target"] == 101.15 and d["new_target"] < BASE["target"]


def test_tighten_never_loosens_or_raises():
    bars = forming(rising())
    d = mh.decide(side="LONG", bars=bars, tape={**TAPE_DELTA_AGAINST, "hvn_above": 101.2}, fading=True, **{**BASE, "stop": 100.75, "target": 101.1})
    assert d["new_stop"] is None and d["new_target"] is None and d["action"] == "HOLD"


def test_three_signals_in_profit_book_full_without_scaleout():
    bars = forming(rising())
    d = mh.decide(side="LONG", bars=bars, tape={**TAPE_DELTA_AGAINST, "hvn_above": 100.85}, fading=True, **BASE)
    assert d["action"] == "BOOK" and d["book_mode"] == "FULL" and d["book_qty"] == 10 and d["count"] >= 3


def test_scaleout_halves_only_four_or_more_shares():
    kw = dict(side="LONG", bars=forming(rising()), tape={**TAPE_DELTA_AGAINST, "hvn_above": 100.85}, fading=True, scaleout=True)
    base = {k: v for k, v in BASE.items() if k != "qty"}
    half = mh.decide(qty=10, **kw, **base)
    assert half["book_mode"] == "HALF" and half["book_qty"] == 5
    odd = mh.decide(qty=5, **kw, **base)
    assert odd["book_mode"] == "HALF" and odd["book_qty"] == 2
    small = mh.decide(qty=3, **kw, **base)
    assert small["book_mode"] == "FULL" and small["book_qty"] == 3


def test_three_signals_but_not_in_profit_enough_only_tightens():
    bars = forming(rising())
    d = mh.decide(side="LONG", bars=bars, tape={**TAPE_DELTA_AGAINST, "hvn_above": 100.15}, fading=True, **{**BASE, "ltp": 100.1, "entry": 100.0})
    assert d["action"] != "BOOK"


def test_short_is_the_exact_mirror():
    bars = forming(rising())
    long_d = mh.decide(side="LONG", bars=bars, tape={**TAPE_DELTA_AGAINST, "hvn_above": 101.2}, fading=True, **BASE)
    short_d = mh.decide(side="SHORT", bars=forming(flip(rising())), tape={**TAPE_DELTA_AGAINST, "hvn_below": 98.8}, fading=True,
                        entry=100.0, stop=101.0, target=98.0, ltp=99.2, qty=10, tick=0.05)
    assert short_d["action"] == long_d["action"] == "TIGHTEN"
    assert sorted(short_d["signals"]) == sorted(long_d["signals"])
    assert 99.2 < short_d["new_stop"] < 101.0 and short_d["new_target"] == 98.85


def test_short_books_at_three_signals():
    d = mh.decide(side="SHORT", bars=forming(flip(rising())), tape={**TAPE_DELTA_AGAINST, "hvn_below": 99.15}, fading=True,
                  entry=100.0, stop=101.0, target=98.0, ltp=99.2, qty=10, tick=0.05)
    assert d["action"] == "BOOK" and d["book_qty"] == 10


def test_no_data_or_garbage_is_a_hold_never_an_exception():
    assert mh.decide(side="LONG", bars=None, **BASE)["action"] == "HOLD"
    assert mh.decide(side="LONG", bars=[], **BASE)["action"] == "HOLD"
    assert mh.decide(side="LONG", bars=[("x", 1, 2)], **BASE)["action"] == "HOLD"
    assert mh.decide(side="LONG", bars=forming(rising()), **{**BASE, "ltp": "bad"})["reason"] == "bad_number"
    assert mh.decide(side="LONG", bars=forming(rising()), **{**BASE, "qty": 0})["reason"] == "no_quantity"
    assert mh.decide(side=None, bars=forming(rising()), tape="junk", fading="x", **BASE)["action"] in ("HOLD", "TIGHTEN", "BOOK")


def test_one_signal_returns_a_recomputed_trail_gap_only():
    bars = forming(rising())
    d = mh.decide(side="LONG", bars=bars, fading=True, **BASE)
    assert d["action"] == "HOLD" and d["count"] == 1 and d["trail_gap_pct"] is not None and d["trail_gap_pct"] >= 0.15


def test_forming_bar_is_ignored():
    bars = rising(8)
    spike = bars + [bar(8, 100.8, 105.0, 100.0, 100.8, 90000.0)]                  # last bar still forming: must not read as a climax
    assert "climax_bar" not in mh.decide(side="LONG", bars=spike, **BASE)["signals"]


# ── wiring ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_status_config_and_loop_are_wired():
    from orders import statuses
    assert "MOMENTUM_EXIT" in statuses.FLAT_SELL_STATUSES and "MOMENTUM_EXIT" in statuses.TERMINAL_STATUSES
    import config
    assert config.MOMENTUM_EXIT_MODE in ("live", "off") and config.MOMENTUM_BOOK_N == 3
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "momentum_hook.run_momentum_exit" in main_src
    assert main_src.index("tape_hook.run_tape_exit") < main_src.index("momentum_hook.run_momentum_exit") < main_src.index("stop_watchdog.run_stop_watchdog")
    hook_src = (ROOT / "orders" / "momentum_hook.py").read_text(encoding="utf-8")
    assert 'exit_reason="MOMENTUM_EXIT"' in hook_src and "scaleout=_scaleout_ok(pos)" in hook_src       # round 164: the half-sell is built, gated by MOMENTUM_SCALEOUT (default off)


def test_default_mode_is_live_and_old_log_reads_live(monkeypatch):
    import importlib

    import config
    monkeypatch.delenv("MOMENTUM_EXIT_MODE", raising=False)
    assert importlib.reload(config).MOMENTUM_EXIT_MODE == "live"
    for old in ("log", "shadow", "lvie"):                      # E276 round 66: no log-only mode; only an explicit off switches it off
        monkeypatch.setenv("MOMENTUM_EXIT_MODE", old)
        assert importlib.reload(config).MOMENTUM_EXIT_MODE == "live", old
    monkeypatch.setenv("MOMENTUM_EXIT_MODE", "off")
    assert importlib.reload(config).MOMENTUM_EXIT_MODE == "off"
    monkeypatch.delenv("MOMENTUM_EXIT_MODE", raising=False)
    importlib.reload(config)
