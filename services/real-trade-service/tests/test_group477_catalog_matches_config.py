"""group477: every Real Money Calibration switch's `shipped` value must equal what config.py really defaults to (a stale one made the page show a wrong
default and a wrong "pending restart"; OVERNIGHT_HOLD_ENABLED drifted after group 470). Runs in a clean interpreter so the suite's own config patches
(conftest) cannot hide a drift."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_CODE = """
import json, sys
sys.path.insert(0, ".")
import config
from control.catalog import CATALOG
from control.kit import canonical, running_value, validate_flag
out = {"drift": [], "invalid": [], "n": len(CATALOG)}
for f in CATALOG:
    p = validate_flag(f)
    if p:
        out["invalid"].append([f.key, p])
    run = running_value(f)
    if run is not None and run != canonical(f, f.shipped):
        out["drift"].append([f.key, run, canonical(f, f.shipped)])
print("RESULT" + json.dumps(out))
"""


def test_catalog_shipped_values_match_config_defaults():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("STRUCTURE_", "KRONOS_", "TIMESFM_", "ENTRY_", "OPENING_", "OVERNIGHT_", "STOPOUT_", "ORB_", "CANDIDATE_", "BROKER_", "ORDER_", "BOARD_", "BAR_", "PS_"))}
    env["CONTROL_DIR"] = tempfile.mkdtemp()
    r = subprocess.run([sys.executable, "-I", "-c", _CODE], cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120)
    line = next((x for x in r.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line, f"could not import config / catalog: {r.stderr[-600:]}"
    out = json.loads(line[6:])
    assert out["n"] > 0 and not out["invalid"], out["invalid"]
    assert not out["drift"], f"catalog `shipped` differs from the config default (key, config, catalog): {out['drift']}"
