"""E273 round 16: the plan's no-shadow guard. The mode switches the plan turns live accept no shadow / log / record-only value in either service, and the
52-week candidate rule with its log-only flag is gone. Plain runner: python3 tests/test_e273_no_shadow_modes.py (service folder). Reads the source files, no imports of the services."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent
PS = ROOT / "services" / "position-stocks-service"
RT = HERE


def _run(svc: Path, var: str, val: str) -> str:
    code = f"import sys; sys.path.insert(0,'.'); import config; print('M'+str(config.{var}))"
    env = dict(os.environ)
    env[var] = val
    return subprocess.run([sys.executable, "-c", code], cwd=str(svc), env=env, capture_output=True, text=True).stdout


def test_listed_switches_read_shadow_and_log_as_live():
    # service, variable, value that must come out for the old shadow / log values
    cases = [(PS, "MARKET_STATE_MODE", "live"), (RT, "MARKET_STATE_MODE", "live"),
             (PS, "ORB_MODE", "live"), (RT, "ORB_MODE", "live"), (PS, "MOMENTUM_EXIT_MODE", "live"),
             (RT, "REGIME_RULES_MODE", "live"), (RT, "STRUCTURE_EXIT_MODE", "tighten")]
    for svc, var, want in cases:
        for old in ("shadow", "log", "record", "junk"):
            got = _run(svc, var, old)
            assert "M" + want in got, (svc.name, var, old, got)


def test_structure_gate_and_breakout_volume_read_log_as_enforce():
    """E285 round 88: the A9 log mode and the B15 evidence downgrade are gone from Real Trade's structure gate."""
    for var in ("STRUCTURE_GATE_MODE", "STRUCTURE_GATE_DEMO_MODE", "STRUCTURE_BREAKOUT_VOLUME_MODE"):
        for old in ("shadow", "log", "record", "junk"):
            assert "Menforce" in _run(RT, var, old), (var, old)
        assert "Moff" in _run(RT, var, "off"), var
    cat = (RT / "control" / "catalog.py").read_text()
    for var in ("STRUCTURE_GATE_MODE", "STRUCTURE_GATE_DEMO_MODE", "STRUCTURE_BREAKOUT_VOLUME_MODE"):
        seg = cat.split(f'Flag("{var}"', 1)[1].split("Flag(", 1)[0]
        assert '"log"' not in seg.split("choices=", 1)[1].split(")", 1)[0], var


def test_swing_mode_shadow_reads_as_off_not_live():
    assert "Moff" in _run(RT, "SWING_MODE", "shadow")      # shadow never bought, so a leftover value must not start buying
    assert "Mlive" in _run(RT, "SWING_MODE", "live")


def test_swing_shadow_picks_and_52w_rule_are_gone():
    cfg = (RT / "config.py").read_text()
    assert "SWING_SHADOW_PICKS_PER_DAY" not in cfg.replace("replaces SWING_SHADOW_PICKS_PER_DAY", "")
    for f in (RT / "control" / "catalog.py", RT / "candidate_engine" / "candidates.py", RT / "main.py", RT / "config.py"):
        t = f.read_text()
        assert "CANDIDATE_52W_RULE_ENABLED" not in t and "CANDIDATE_52W_LOG_ONLY" not in t, f.name
    assert "CANDIDATE_52W_LOG_ONLY" not in (ROOT / "docker-compose.yml").read_text()


def test_catalog_choices_have_no_shadow_or_log_for_the_live_switches():
    for svc in (PS, RT):
        cat = (svc / "control" / "catalog.py").read_text()
        for var in ("MARKET_STATE_MODE", "ORB_MODE", "REGIME_RULES_MODE", "STRUCTURE_EXIT_MODE", "SWING_MODE", "MOMENTUM_EXIT_MODE"):
            if f'Flag("{var}"' not in cat:
                continue
            seg = cat.split(f'Flag("{var}"', 1)[1].split("Flag(", 1)[0]
            ch = seg.split("choices=", 1)[1].split(")", 1)[0] if "choices=" in seg else ""
            assert "shadow" not in ch and '"log"' not in ch, (svc.name, var, ch)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
