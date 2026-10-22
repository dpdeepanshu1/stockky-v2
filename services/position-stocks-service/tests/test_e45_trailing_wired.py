"""E45: pin that the Position Stocks trailing stop is actually wired into the live loop and on the calibration page.

The trail itself is tested in test_trailing.py; these tests only fail if someone unhooks it (the loop call, the switch, the tuning knobs).
"""
from pathlib import Path

import config
from control.catalog import CATALOG

SVC = Path(__file__).resolve().parent.parent


def test_fast_reconcile_loop_calls_the_trailing_pass():
    src = (SVC / "main.py").read_text()
    assert "trailing.run_trailing_stop" in src
    assert "from orders import" in src and "trailing" in src.split("from orders import", 1)[1].split("\n", 1)[0] or "import trailing" in src


def test_trailing_is_on_by_default_and_needs_super_order():
    assert config.TRAILING_STOP_ENABLED is True
    assert config.USE_SUPER_ORDER is True


def test_trail_knobs_are_on_the_calibration_page_with_shipped_equal_to_config():
    flags = {f.key: f for f in CATALOG}
    for key in ("TRAILING_STOP_ENABLED", "TRAIL_ACTIVATE_PCT", "TRAIL_DISTANCE_STOP_FRACTION", "TRAIL_MIN_DISTANCE_PCT", "TRAIL_MIN_STEP_PCT"):
        assert key in flags, key
        assert float(flags[key].shipped.replace("true", "1")) == float(getattr(config, key))
        assert flags[key].live is True


def test_a_trail_can_never_end_below_entry_once_active():
    from orders.trailing import compute_trailing_stop
    new = compute_trailing_stop(entry=100.0, peak=102.0, ltp=101.5, current_stop=98.0, target=106.0, stop_pct=1.5)
    assert new is not None and new > 100.0
