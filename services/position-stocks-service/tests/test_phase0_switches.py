"""E106 (P2 item 16, the page part): the VIX / pre-open gap / opening-gate ATR constants are on the Switches page, live-read, shipped equal to the
real config default, and protected from the calibrator. Stdlib only (runs without SQLAlchemy). Behaviour at the defaults is unchanged."""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import config  # noqa: E402

KEYS = {"VIX_CAUTION_ABOVE": "18.0", "VIX_SPIKE_PCT": "10.0", "VIX_CAUTION_SIZE_MULT": "0.7", "PREOPEN_GAP_UP_BLOCK_PCT": "6.0",
        "PREOPEN_GAP_DOWN_BLOCK_PCT": "3.0", "PREOPEN_GAP_GATE_UNTIL_IST": "10:00", "OPENING_GATE_MIN_STOP_ATR_FRAC": "0.3",
        "SCALP_TARGET_CAP_MIN_STOP_ATR_MULT": "1.0"}
CAT = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
AUTO = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")


def _flag(key):
    i = CAT.index(f'Flag("{key}"')
    return CAT[i:CAT.index("\n    Flag(", i + 1)]


def test_every_key_is_a_live_catalog_switch_with_its_config_target():
    for k in KEYS:
        f = _flag(k)
        assert "live=True" in f, k
        assert f'(("config", "{k}"),)' in f.replace("targets=", ""), k


def test_shipped_value_is_the_real_config_default():
    for k, shipped in KEYS.items():
        assert f'shipped="{shipped}"' in _flag(k), k
        real = getattr(config, k)
        assert (real == shipped) if isinstance(real, str) else abs(float(real) - float(shipped)) < 1e-9, (k, real)


def test_directions_are_the_loosening_directions():
    for k in ("VIX_CAUTION_ABOVE", "VIX_SPIKE_PCT", "VIX_CAUTION_SIZE_MULT", "PREOPEN_GAP_UP_BLOCK_PCT", "PREOPEN_GAP_DOWN_BLOCK_PCT"):
        assert 'looser="higher"' in _flag(k), k
    for k in ("OPENING_GATE_MIN_STOP_ATR_FRAC", "SCALP_TARGET_CAP_MIN_STOP_ATR_MULT"):
        assert 'looser="lower"' in _flag(k), k
    assert 'looser="earlier"' in _flag("PREOPEN_GAP_GATE_UNTIL_IST")
    for k in ("VIX_CAUTION_ABOVE", "VIX_SPIKE_PCT", "PREOPEN_GAP_UP_BLOCK_PCT", "PREOPEN_GAP_DOWN_BLOCK_PCT"):
        assert "zero_off=True" in _flag(k), k            # 0 = test disabled = the loosest value, as the gate code reads it


def test_every_key_is_protected_from_the_calibrator():
    for k in KEYS:
        assert re.search(r'"%s":\s*"' % k, AUTO), k


def test_keys_are_unique_in_the_catalog():
    for k in KEYS:
        assert CAT.count(f'Flag("{k}"') == 1, k
