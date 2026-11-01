"""E285 round 87: the plan's no-shadow guard for Position Stocks (Real Trade has test_e273_no_shadow_modes.py). SHORT_MODE, TRADE_ODDS_MODE and the structure
gate (with its thin-volume breakout test) accept no shadow / log / record-only value. A leftover value must read as the safe, live side: SHORT_MODE shadow = off (a
shadow never ordered, so a leftover must not start ordering), the others = live / enforce. Reads config.py in a fresh interpreter; no database, no network."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent          # services/position-stocks-service


def _cfg(var: str, val=None) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STRUCTURE_", "PS_STRUCTURE_", "SHORT_MODE", "TRADE_ODDS_MODE"))}
    if val is not None:
        env[var] = val
    env["CONTROL_DIR"] = os.environ.get("TMPDIR", "/tmp")
    code = f"import sys; sys.path.insert(0, '.'); import config; print('V=' + str(config.{var.removeprefix('PS_')}))"
    r = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(HERE), env=env, capture_output=True, text=True, timeout=120)
    line = next((x for x in r.stdout.splitlines() if x.startswith("V=")), None)
    assert line, r.stderr[-600:]
    return line[2:]


def test_short_mode_ships_off_and_a_leftover_shadow_reads_as_off_never_live():
    assert _cfg("SHORT_MODE") == "off"
    for old in ("shadow", " SHADOW ", "record", "log", "junk"):
        assert _cfg("SHORT_MODE", old) == "off", old
    assert _cfg("SHORT_MODE", "live") == "live"


def test_trade_odds_ships_live_and_a_leftover_shadow_reads_as_live():
    assert _cfg("TRADE_ODDS_MODE") == "live"
    for old in ("shadow", "log", "junk", ""):
        assert _cfg("TRADE_ODDS_MODE", old) == "live", old
    assert _cfg("TRADE_ODDS_MODE", "off") == "off"


def test_structure_gate_and_breakout_volume_read_log_and_shadow_as_enforce():
    for var in ("STRUCTURE_GATE_MODE", "STRUCTURE_BREAKOUT_VOLUME_MODE"):
        assert _cfg(var) == "enforce", var
        for old in ("log", "shadow", "record", "junk"):
            assert _cfg(var, old) == "enforce", (var, old)
            assert _cfg("PS_" + var, old) == "enforce", ("PS_" + var, old)           # not even through the PS_ name
        assert _cfg(var, "off") == "off", var


def test_catalog_choices_have_no_shadow_or_log_for_these_switches():
    cat = (HERE / "control" / "catalog.py").read_text(encoding="utf-8")
    for key in ("PS_STRUCTURE_GATE_MODE", "PS_STRUCTURE_BREAKOUT_VOLUME_MODE", "SHORT_MODE", "TRADE_ODDS_MODE"):
        seg = cat.split(f'Flag("{key}"', 1)[1].split("Flag(", 1)[0]
        ch = seg.split("choices=", 1)[1].split(")", 1)[0]
        assert "shadow" not in ch and '"log"' not in ch, (key, ch)
        assert 'shipped="shadow"' not in seg and 'recommended="shadow"' not in seg, key


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
