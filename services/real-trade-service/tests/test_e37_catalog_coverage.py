"""E37: the Real Money Calibration rule - every on/off or mode switch in config.py (`*_ENABLED`, `*_MODE`) is either a catalog switch
(control/catalog.py) or is listed below with the reason it is not. A new switch that is in neither fails this test, so a feature can no longer ship
with its switch missing from the calibration page. Reads the files as text (ast), so it needs no imports and runs anywhere."""
import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Not on the calibration page on purpose. A secret, a login or an infrastructure link must never be a one-click switch there.
EXEMPT = {
    "ADMIN_PASSWORD_CHANGE_ENABLED": "login security: never a one-click switch",
    "DHAN_TOTP_ENABLED": "broker login credential flow, set in .env only",
    "DAILY_REPORT_ENABLED": "report delivery, never touches a trade",
    "ENTRY_PRODUCT_MODE": "order product type (CNC / MIS) needs a restart and a broker check",
    "EDIS_MORNING_CHECK_ENABLED": "morning broker authorisation check, an alert not a gate",
}


def _config_switches():
    tree = ast.parse((ROOT / "config.py").read_text(encoding="utf-8"))
    return [t.id for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
            if isinstance(t, ast.Name) and re.search(r"(_ENABLED|_MODE)$", t.id)]


def test_every_config_switch_is_on_the_calibration_page_or_exempt_with_a_reason():
    catalog = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    on_page = set(re.findall(r'"([A-Z0-9_]+)"', catalog))
    missing = [n for n in _config_switches() if n not in on_page and n not in EXEMPT]
    assert not missing, (
        f"switch(es) {missing} in config.py are not on the Real Money Calibration page: add a Flag to control/catalog.py "
        "(run scripts/check_control_catalog.py --probe), or add them to EXEMPT here with the reason")


def test_exempt_list_has_no_stale_entries():
    names = set(_config_switches())
    catalog = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    on_page = set(re.findall(r'"([A-Z0-9_]+)"', catalog))
    stale = [k for k in EXEMPT if k not in names or k in on_page]
    assert not stale, f"EXEMPT entries that no longer exist or are now on the page: {stale}"


def test_exempt_reasons_are_written():
    assert all(len(v) >= 15 for v in EXEMPT.values())

