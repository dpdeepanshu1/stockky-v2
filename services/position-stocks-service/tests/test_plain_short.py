"""E114: orders/plain_short.py. The behaviour script scripts/test_plain_short.py runs in a clean interpreter (it fakes sys.modules, so it must not share this pytest
process); the static rails are read from the source and run anywhere."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT.parent.parent
SRC = (ROOT / "orders" / "plain_short.py").read_text(encoding="utf-8")


def test_behaviour_script_passes_in_a_clean_interpreter():
    r = subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "test_plain_short.py")], cwd=str(ROOT), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, (r.stdout + r.stderr)[-3000:]
    assert re.search(r"ALL \d+ OK", r.stdout), r.stdout[-1500:]
    assert "FAIL" not in r.stdout


def test_the_switch_is_in_the_catalog_and_protected():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    assert 'Flag("SHORT_PLAIN_FALLBACK"' in cat and re.search(r'Flag\("SHORT_PLAIN_FALLBACK".*?shipped="true"', cat, re.S)
    auto = (REPO / "services" / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    assert '"SHORT_PLAIN_FALLBACK":' in auto


def test_the_guard_does_not_depend_on_the_switch_and_has_no_trailing():
    body = SRC[SRC.index("def run_plain_short_guard"):]
    assert "if not enabled()" not in body, "the switch gates NEW plain shorts only"
    assert "modify_super_order" not in SRC and "trailing" not in re.sub(r'''""".*?"""''', "", SRC, flags=re.S).lower().replace("trailing it", "")


def test_a_plain_short_is_intraday_and_never_cnc():
    code = re.sub(r'''""".*?"""''', "", SRC, flags=re.S)
    assert 'product_type="INTRADAY"' in code and '"CNC"' not in code


def test_target_exit_is_a_known_status():
    st = (ROOT / "orders" / "statuses.py").read_text(encoding="utf-8")
    assert '"TARGET_EXIT"' in st
    api = (REPO / "frontend" / "src" / "positionStocksApi.ts").read_text(encoding="utf-8")
    assert '"TARGET_EXIT"' in api
