"""E273 round 22: Real Trade's open-position cap is 5 and follows the Calibration value for a system-owned risk row.
Plain runner: python3 tests/test_e273_max_positions.py (service folder)."""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from risk_engine import effective_risk as er  # noqa: E402


def row(updated_by, n=3):
    return SimpleNamespace(updated_by=updated_by, max_concurrent_positions=n)


def test_default_is_five_and_the_portfolio_risk_cap_stays():
    assert config.DEFAULT_MAX_CONCURRENT_POSITIONS == 5
    assert config.DEFAULT_MAX_PORTFOLIO_RISK_PCT == 5.0


def test_system_owned_row_follows_config_admin_row_wins():
    assert er.effective_max_positions(row("system-seed", 3)) == 5
    assert er.effective_max_positions(row("system-migration-e46", 3)) == 5
    assert er.effective_max_positions(row("admin@x", 3)) == 3             # saved through PUT /risk-config: the stored number wins
    assert er.effective_max_positions(row("demo-user", 4)) == 4
    assert er.effective_max_positions(row(None, 3)) == 3                  # an old untagged row is stored
    assert er.effective_max_positions(None) is None


def test_bad_config_values_fall_back_to_the_stored_number():
    old = config.DEFAULT_MAX_CONCURRENT_POSITIONS
    try:
        for bad in (0, -2, 11, 99, "x", None):
            config.DEFAULT_MAX_CONCURRENT_POSITIONS = bad
            assert er.effective_max_positions(row("system-seed", 3)) == 3, bad
        config.DEFAULT_MAX_CONCURRENT_POSITIONS = 2
        assert er.effective_max_positions(row("system-seed", 3)) == 2
    finally:
        config.DEFAULT_MAX_CONCURRENT_POSITIONS = old


def test_env_value_is_read_at_import():
    code = "import sys; sys.path.insert(0,'.'); import config; print('N'+str(config.DEFAULT_MAX_CONCURRENT_POSITIONS))"
    for val, want in (("", "5"), ("3", "3"), ("4", "4")):
        env = dict(os.environ)
        env["DEFAULT_MAX_CONCURRENT_POSITIONS"] = val
        out = subprocess.run([sys.executable, "-c", code], cwd=str(HERE), env=env, capture_output=True, text=True).stdout
        assert "N" + want in out, (val, out)


def test_every_account_state_uses_the_effective_cap():
    for f in ("entry_engine/entry.py", "manual_engine.py", "main.py"):
        t = (HERE / f).read_text()
        assert "max_concurrent_positions=effective_max_positions(" in t, f
        assert "max_concurrent_positions=risk.max_concurrent_positions" not in t and "max_concurrent_positions=risk_row.max_concurrent_positions" not in t, f
    assert 'Flag("DEFAULT_MAX_CONCURRENT_POSITIONS"' in (HERE / "control" / "catalog.py").read_text()


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
