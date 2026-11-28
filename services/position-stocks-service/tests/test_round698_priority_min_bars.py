"""Round 698 (E720): a noted candidate gets a bar file with as few as BAR_RECORD_PRIORITY_MIN_BARS bars (the 20-bar floor left thinly traded skipped names with no file)."""
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from feed import bar_recorder as br
from orders import candidate_outcomes as co

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = datetime(2026, 11, 26, 9, 15, tzinfo=IST).timestamp()
DAYNUM = int((OPEN + 19800) // 86400)
KEYS = ("BAR_RECORD_ENABLED", "BAR_RECORD_DIR", "BAR_RECORD_MIN_BARS", "BAR_RECORD_PRIORITY_MIN_BARS", "BAR_RECORD_PRIORITY_ENABLED", "BAR_WANTED_ENABLED", "BAR_RECORD_MAX_SYMBOLS")
_orig = {}
_tmp = []


def setup_function(_):
    for k in KEYS:
        _orig.setdefault(k, getattr(config, k, None))
    br.reset()
    d = tempfile.mkdtemp(prefix="barrec697_")
    _tmp.append(d)
    config.BAR_RECORD_ENABLED, config.BAR_RECORD_DIR, config.BAR_RECORD_MIN_BARS = True, d, 20
    config.BAR_RECORD_PRIORITY_MIN_BARS, config.BAR_RECORD_PRIORITY_ENABLED, config.BAR_WANTED_ENABLED, config.BAR_RECORD_MAX_SYMBOLS = 2, True, False, 250


def teardown_function(_):
    for k, v in _orig.items():
        setattr(config, k, v)
    for d in _tmp:
        shutil.rmtree(d, ignore_errors=True)
    _tmp.clear()
    br.reset()


def _bars(n, gap=7):
    """n minute bars, one every `gap` minutes (a thinly traded name)."""
    return [(OPEN + k * gap * 60.0, 100.0, 100.2, 99.8, 100.0 + k * 0.1, 10.0) for k in range(n)]


def _run(store, noted):
    now = OPEN + 6 * 3600 + 20 * 60                                 # 15:35 IST: the final flush
    br.note_symbols(noted, now)
    return br.run_once(now, False, bars_fn=lambda s: store[s], symbols_fn=lambda: list(store), day_fn=lambda: DAYNUM, backfilled_fn=lambda s: False, subscribed_fn=lambda: set(store))


def _files():
    d = os.path.join(config.BAR_RECORD_DIR, "2026-11-26")
    return sorted(os.listdir(d)) if os.path.isdir(d) else []


def test_a_noted_thin_name_gets_a_file_and_an_unnoted_one_does_not():
    _run({"THIN": _bars(12), "OTHER": _bars(12), "FULL": _bars(25, 1)}, ["THIN"])
    assert _files() == ["FULL.json", "THIN.json"]                  # THIN noted: 12 bars is enough; OTHER not noted: still needs 20


def test_the_floor_is_still_two_bars_and_names_under_it_are_listed():
    _run({"ONE": _bars(1), "TWO": _bars(2)}, ["ONE", "TWO"])
    assert _files() == ["TWO.json"]
    s = br.stats()
    assert s["priority_too_few_bars"] == 1 and s["priority_too_few_bars_names"] == ["ONE"]


def test_priority_off_keeps_the_old_floor():
    config.BAR_RECORD_PRIORITY_ENABLED = False
    _run({"THIN": _bars(12)}, ["THIN"])
    assert _files() == []


def test_the_labeller_measures_a_thin_recorded_name():
    bars = _bars(12)                                                # a bar every 7 min: t0 at the 2nd bar, +60 min is covered by later bars
    lab = co.label([list(b) for b in bars], bars[1][0] + 30, "LONG")
    assert lab["status"] == "ok" and lab["r60"] is not None


def test_config_catalog_and_scorecard_wording():
    assert config.BAR_RECORD_PRIORITY_MIN_BARS == 2 or _orig["BAR_RECORD_PRIORITY_MIN_BARS"] == 2
    assert "BAR_RECORD_PRIORITY_MIN_BARS" in open("control/catalog.py").read()
    assert "under 20 minutes" in open("orders/scorecard.py").read()
