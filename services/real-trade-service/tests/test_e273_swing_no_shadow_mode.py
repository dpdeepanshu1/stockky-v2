"""E273 round 13: the swing lane has no shadow mode (live or off), the evening plan follows SWING_PICKS_PER_DAY + SWING_BACKUP_PICKS, and entries start no earlier
than SWING_ENTRY_FLOOR_IST. Plain runner: python3 tests/test_e273_swing_no_shadow_mode.py (service folder). No database, no network."""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, SVC)
import test_swing_lane as base  # noqa: E402  (its helpers: row, _scan, MemRepo)

import config  # noqa: E402
from swing import lane  # noqa: E402


def _cfg_in_clean_interpreter(value):
    env = dict(os.environ)
    env.pop("SWING_MODE", None)
    if value is not None:
        env["SWING_MODE"] = value
    out = subprocess.run([sys.executable, "-c", "import config;print(config.SWING_MODE)"], cwd=SVC, env=env, capture_output=True, text=True, timeout=60)
    return out.stdout.strip().splitlines()[-1] if out.stdout.strip() else out.stderr[-200:]


def test_modes_are_live_or_off_only():
    assert lane.MODES == ("off", "live")
    assert lane.norm_mode("live") == "live" and lane.norm_mode(" LIVE ") == "live" and lane.norm_mode("off") == "off"
    assert lane.norm_mode("shadow") == "off" and lane.norm_mode("") == "off" and lane.norm_mode(None) == "off" and lane.norm_mode("x") == "off"
    assert lane.live_allowed("shadow", "DEMO", True) is False and lane.live_allowed("live", "REAL", False) is False and lane.live_allowed("live", "REAL", True) is True


def test_config_reads_an_old_shadow_value_as_off_and_junk_as_live():
    assert _cfg_in_clean_interpreter(None) == "live"
    assert _cfg_in_clean_interpreter("shadow") == "off"          # it never bought: it must not start
    assert _cfg_in_clean_interpreter("off") == "off"
    assert _cfg_in_clean_interpreter("LIVE") == "live"
    assert _cfg_in_clean_interpreter("junk") == "live"           # the shipped default, as before


def test_shadow_picks_setting_is_gone_and_backup_default_keeps_three_planned():
    assert not hasattr(config, "SWING_SHADOW_PICKS_PER_DAY")
    assert config.SWING_BACKUP_PICKS == 1 and config.SWING_PICKS_PER_DAY == 3
    assert config.SWING_ENTRY_FLOOR_IST == "09:21"


def test_evening_plan_is_picks_plus_backups():
    old = (config.SWING_PICKS_PER_DAY, config.SWING_BACKUP_PICKS)
    try:
        rows = [base.row(f"S{i}", "2026-10-26", 100, 99, 100, rank=i + 1) for i in range(8)]
        config.SWING_PICKS_PER_DAY, config.SWING_BACKUP_PICKS = 2, 1
        repo = base.MemRepo()
        assert base._scan(repo, "2026-10-26", rows)["planned"]["planned"] == 3
        config.SWING_PICKS_PER_DAY, config.SWING_BACKUP_PICKS = 2, 3
        repo = base.MemRepo()
        assert base._scan(repo, "2026-10-26", rows)["planned"]["planned"] == 5
        config.SWING_PICKS_PER_DAY, config.SWING_BACKUP_PICKS = 1, 0
        repo = base.MemRepo()
        assert base._scan(repo, "2026-10-26", rows)["planned"]["planned"] == 1
    finally:
        config.SWING_PICKS_PER_DAY, config.SWING_BACKUP_PICKS = old


def test_entry_floor_wiring_and_no_shadow_defaults_by_source():
    live = open(os.path.join(SVC, "swing", "live.py"), encoding="utf-8").read()
    assert "SWING_ENTRY_FLOOR_IST" in live and "max(_t_cfg, _t_floor)" in live
    for p in ("swing/live.py", "swing/engine.py", "execution/auto_pilot.py"):
        src = open(os.path.join(SVC, p), encoding="utf-8").read()
        assert '"SWING_MODE", "shadow"' not in src, p
    cat = open(os.path.join(SVC, "control", "catalog.py"), encoding="utf-8").read()
    assert 'choices=("off", "live")' in cat[cat.index('Flag("SWING_MODE"'):cat.index('Flag("SWING_LIVE_REAL_ENABLED"')]
    assert 'Flag("SWING_BACKUP_PICKS"' in cat and 'Flag("SWING_ENTRY_FLOOR_IST"' in cat


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v) and v.__module__ == "__main__":
            v(); n += 1; print("ok", k)
    print(n, "passed")
