"""E263 (2026-10-30): flat trades were closed at their 20 / 30-minute timer almost every time (SATIN, RAMKY, CAMS x2 on 30 Oct).

Two reasons, both in orders/exit_adaptive.py: a candidate refused for capital is on the log most of the day (only ~3 of the 5 slots can ever be filled), and
"lower highs over 10 minutes" is true in roughly half of all windows. Now: a waiting candidate alone closes a flat trade only after EXIT_SLOT_PRESSURE_MIN_AGE_MIN,
and fading (strict) needs the VWAP side missed by a margin, or lower highs on a trade that is not in profit. Only unittest.mock is used.
"""
from __future__ import annotations

import contextlib
import sys
import types
from unittest import mock

import config
from orders import exit_adaptive


@contextlib.contextmanager
def _on(**over):
    with contextlib.ExitStack() as st:
        st.enter_context(mock.patch.object(config, "EXIT_ADAPTIVE_ENABLED", True, create=True))
        for k, v in over.items():
            st.enter_context(mock.patch.object(config, k, v, create=True))
        yield


def _fake_ws(vwap, ticks):
    @contextlib.contextmanager
    def cm():
        fake = types.SimpleNamespace(get_exchange_vwap=lambda _s: vwap, get_tick_buffer=lambda _s: ticks)
        pkg = sys.modules.get("feed")
        with mock.patch.dict(sys.modules, {"feed.ws_client": fake}):
            if pkg is not None:
                with mock.patch.object(pkg, "ws_client", fake, create=True):
                    yield
            else:
                yield
    return cm()


def test_waiting_candidate_alone_needs_the_trade_to_be_old_enough():
    with _on():
        held = exit_adaptive.stagnation_decision(age_min=25, fading=False, pressure="X: INSUFFICIENT_CAPITAL")
        assert held[0] is False and "only 25m old" in held[1]
        assert exit_adaptive.stagnation_decision(age_min=46, fading=False, pressure="X: INSUFFICIENT_CAPITAL")[0] is True
        assert exit_adaptive.stagnation_decision(age_min=25, fading=True, pressure="X: INSUFFICIENT_CAPITAL")[0] is True    # fading closes from the first timer
        assert exit_adaptive.stagnation_decision(age_min=91, fading=False, pressure=None)[0] is True                        # hard maximum unchanged
    with _on(EXIT_SLOT_PRESSURE_MIN_AGE_MIN=0.0):
        assert exit_adaptive.stagnation_decision(age_min=25, fading=False, pressure="X: INSUFFICIENT_CAPITAL")[0] is True   # 0 = the E252 rule


def test_vwap_side_needs_the_margin_in_strict_mode():
    with _on(), _fake_ws(100.0, []):
        assert exit_adaptive.is_fading("X", 99.95) is False          # 0.05 % under VWAP: noise
        assert exit_adaptive.is_fading("X", 99.80) is True           # 0.20 % under
        assert exit_adaptive.is_fading("X", 100.05, short=True) is False
        assert exit_adaptive.is_fading("X", 100.20, short=True) is True
    with _on(EXIT_FADE_STRICT=False), _fake_ws(100.0, []):
        assert exit_adaptive.is_fading("X", 99.95) is True           # E252 rule: any distance


def test_lower_highs_only_count_while_the_trade_is_not_in_profit():
    ticks_down = [(1000.0 + i * 30.0, 100.0 - i * 0.02) for i in range(21)]          # 10 minutes, lower highs, ends 99.6
    with _on(), _fake_ws(None, ticks_down):
        assert exit_adaptive.is_fading("X", 99.6, entry=99.0) is False               # still above its entry: a pause, not a fade
        assert exit_adaptive.is_fading("X", 99.6, entry=100.0) is True               # under its entry with lower highs
        assert exit_adaptive.is_fading("X", 99.6) is True                            # no entry price = the E252 rule
    with _on(EXIT_FADE_STRICT=False), _fake_ws(None, ticks_down):
        assert exit_adaptive.is_fading("X", 99.6, entry=99.0) is True


def test_short_lower_highs_mirror():
    ticks_up = [(1000.0 + i * 30.0, 100.0 + i * 0.02) for i in range(21)]            # higher lows, ends 100.4
    with _on(), _fake_ws(None, ticks_up):
        assert exit_adaptive.is_fading("X", 100.4, short=True, entry=101.0) is False  # short in profit
        assert exit_adaptive.is_fading("X", 100.4, short=True, entry=100.0) is True   # short under water


def test_run_stagnation_exit_passes_the_entry_price_and_the_knobs_exist():
    from pathlib import Path
    svc = Path(__file__).resolve().parent.parent
    eod = (svc / "orders" / "eod_squareoff.py").read_text(encoding="utf-8")
    assert "entry=pos.entry_price" in eod
    cfg = (svc / "config.py").read_text(encoding="utf-8")
    for k in ("EXIT_SLOT_PRESSURE_MIN_AGE_MIN", "EXIT_FADE_STRICT", "EXIT_FADE_VWAP_MARGIN_PCT"):
        assert k in cfg
