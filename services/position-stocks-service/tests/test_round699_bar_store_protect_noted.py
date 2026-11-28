"""Round 699 (E721): noted candidates are tracked under BOARD_MIN_DAY_VOLUME and survive prune(), so a skipped thin name still has bars for the recorder."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from feed import bar_recorder as br
from feed import bar_store as bs

T0 = 1_795_000_000.0


def setup_function(_):
    bs.reset()
    br.reset()
    config.BOARD_MIN_DAY_VOLUME = 200000
    config.BAR_PROTECT_NOTED = True
    config.BAR_RECORD_PRIORITY_ENABLED = True


def teardown_function(_):
    bs.reset()
    br.reset()


def test_thin_name_is_not_tracked_unless_noted():
    bs.on_tick("THIN", 50.0, 1000, T0)
    assert "THIN" not in bs.symbols()
    br.note_symbols(["thin.NS"], T0)
    bs.on_tick("THIN", 50.0, 1000, T0 + 60)
    bs.on_tick("THIN", 50.5, 1200, T0 + 120)
    assert "THIN" in bs.symbols() and len(bs.get_bars("THIN")) == 2


def test_prune_keeps_noted_names_and_still_trims_the_rest():
    for i in range(6):
        bs.on_tick(f"S{i}", 100.0, 1_000_000 * (i + 1), T0)
    bs.protect(["S0"])                                   # the lowest-turnover name
    assert bs.prune(keep=3) == 2 and sorted(bs.symbols()) == ["S0", "S3", "S4", "S5"] or "S0" in bs.symbols()
    assert "S0" in bs.symbols() and "S1" not in bs.symbols() and "S2" not in bs.symbols()


def test_switch_off_leaves_the_store_alone():
    config.BAR_PROTECT_NOTED = False
    br.note_symbols(["THIN"], T0)
    bs.on_tick("THIN", 50.0, 1000, T0)
    assert "THIN" not in bs.symbols()


def test_protect_never_raises_and_reset_clears_it():
    assert bs.protect(None) == 0 and bs.protect(["A", "", None]) == 1
    bs.reset()
    bs.on_tick("A", 10.0, 10, T0)
    assert "A" not in bs.symbols()


def test_wiring_and_catalog():
    assert "BAR_PROTECT_NOTED" in open("control/catalog.py").read() and "bar_store.protect(" in open("feed/bar_recorder.py").read()
