"""E273: STRUCTURE_EXIT_MODE defaults to tighten. Plain runner: python3 tests/test_e273_structure_exit_tighten_default.py (pytest also collects it)."""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config
from exit_engine import structure_exit as sx

ENTRY = 100.0


def _setup():
    for k, v in (("STRUCTURE_EXIT_TIGHT_ATR", 0.75), ("STRUCTURE_EXIT_CHOCH_BARS", 3), ("STRUCTURE_EXIT_BOS_BARS", 3), ("STRUCTURE_TRAIL_ENABLED", True),
                 ("STRUCTURE_TRAIL_MIN_ATR", 1.0), ("STRUCTURE_MAX_AGE_DAYS", 3650.0), ("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0)):
        setattr(config, k, v)
    sx.reset()


def _sum(**kw):
    base = {"ms_quality": "ok", "ms_ver": "1.0.0", "ms_asof": date.today().isoformat(), "ms_price": 110.0, "ms_atr": 4.0, "ms_choch_dir": "bearish", "ms_choch_bars": 1,
            "ms_bos_dir": None, "ms_bos_bars": None, "ms_bos_failed": None, "ms_stop": 104.0, "ms_trend": "bullish"}
    base.update(kw)
    return base


def _ev(ltp=110.0, stop=96.0):
    return sx.evaluate(_sum(), entry_price=ENTRY, ltp=ltp, current_stop=stop, atr=4.0, opened_at=datetime(2026, 1, 2, tzinfo=timezone.utc))   # exit_mode not passed: the default decides


def test_default_mode_is_tighten_and_junk_falls_back_to_it():
    _setup()
    config.STRUCTURE_EXIT_MODE = "tighten"
    assert sx.mode() == "tighten"
    config.STRUCTURE_EXIT_MODE = "junk"
    assert sx.mode() == "tighten"
    config.STRUCTURE_EXIT_MODE = "tighten"


def test_bearish_break_on_a_profitable_trade_raises_the_stop_without_selling():
    _setup(); config.STRUCTURE_EXIT_MODE = "tighten"
    v = _ev(ltp=110.0, stop=96.0)
    assert v["action"] in ("tighten", "trail") and v["new_stop"] is not None
    assert v["new_stop"] > 96.0 and v["new_stop"] < 110.0 and v["new_stop"] >= ENTRY      # at least breakeven, below the price, never lowered


def test_a_losing_trade_and_a_higher_existing_stop_are_never_touched():
    _setup(); config.STRUCTURE_EXIT_MODE = "tighten"
    assert _ev(ltp=95.0, stop=90.0).get("new_stop") is None and _ev(ltp=95.0, stop=90.0)["action"] != "exit"
    v = _ev(ltp=110.0, stop=108.0)
    assert v.get("new_stop") is None or v["new_stop"] > 108.0


def test_off_changes_nothing_log_is_gone_and_exit_is_opt_in():
    _setup()
    opened = datetime(2026, 1, 2, tzinfo=timezone.utc)
    kw = dict(entry_price=ENTRY, ltp=110.0, current_stop=96.0, atr=4.0, opened_at=opened)
    assert sx.evaluate(_sum(), exit_mode="off", **kw)["opinion"] == "off"
    assert "log" not in sx.MODES      # E273 round 16: the record-only mode is gone; an old "log" falls back to the configured tighten
    assert sx.evaluate(_sum(), exit_mode="log", **kw)["mode"] == "tighten"
    config.STRUCTURE_EXIT_MODE = "tighten"
    assert _ev()["action"] != "exit"


def test_catalog_and_autocalib_agree_with_the_default():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cat = open(os.path.join(root, "control", "catalog.py")).read()
    assert 'shipped="tighten", recommended="tighten", evidence="G2 benefit"' in cat
    ac = open(os.path.join(root, "..", "calibration-runner-service", "autocalib.py")).read()
    assert 'cur == "exit"' in ac and 'Decision("tighten"' in ac


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for f in fns:
        f(); print("ok", f.__name__)
    print(len(fns), "passed")
