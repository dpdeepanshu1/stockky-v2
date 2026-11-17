"""Round 598: blocked_outcome classifies a blocked name from its recorded 1-minute bars."""
import json, os, sys, tempfile
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(__file__))
import blocked_outcome as b

DAY = "2026-11-17"
T0 = datetime(2026, 11, 17, 4, 0).replace(tzinfo=timezone.utc).timestamp()   # 09:30 IST


def _bars(path_dir, sym, closes_highs_lows):
    os.makedirs(os.path.join(path_dir, DAY), exist_ok=True)
    bars = [[T0 + 60 * i, c, h, l, c, 100] for i, (c, h, l) in enumerate(closes_highs_lows)]
    with open(os.path.join(path_dir, DAY, sym.replace("&", "%26") + ".json"), "w") as f:
        json.dump({"v": 1, "bars": bars}, f)


def test_labels_and_percents():
    d = tempfile.mkdtemp()
    _bars(d, "RUN", [(100, 100, 100), (103, 104, 100), (104, 105, 103)])     # best +5, close +4
    _bars(d, "SPK", [(100, 100, 100), (103, 103, 99), (99, 103, 98)])        # best +3, close -1
    _bars(d, "FLT", [(100, 100, 100), (100.5, 101, 99.5), (100.2, 100.8, 100)])
    first = datetime(2026, 11, 17, 4, 0, 20)
    rows, c = b.analyse([("RUN", 5, first, first), ("SPK", 4, first, first), ("FLT", 3, first, first), ("GONE", 2, first, first)], d, DAY, 2.0)
    by = {r["symbol"]: r for r in rows}
    assert by["RUN"]["label"] == "RAN" and round(by["RUN"]["up"], 1) == 5.0 and round(by["RUN"]["close"], 1) == 4.0
    assert by["SPK"]["label"] == "SPIKE" and by["SPK"]["close"] < 0
    assert by["FLT"]["label"] == "NO_MOVE"
    assert by["GONE"]["label"] == "no_bars" and by["RUN"]["first_ist"] == "09:30"
    assert c == {"RAN": 1, "SPIKE": 1, "NO_MOVE": 1, "no_bars": 1}


def test_symbol_with_ampersand_and_bars_before_block_ignored():
    d = tempfile.mkdtemp()
    _bars(d, "M&M", [(90, 99, 80), (100, 100, 100), (110, 112, 100)])        # first bar is BEFORE the block
    first = datetime(2026, 11, 17, 4, 1, 5)                                   # inside bar 2
    rows, _ = b.analyse([("M&M", 1, first, first)], d, DAY, 2.0)
    assert rows[0]["px0"] == 100 and round(rows[0]["up"], 1) == 12.0          # bar 1's 99 / 80 are not counted


def test_block_after_last_bar_and_bad_file_are_no_bars():
    d = tempfile.mkdtemp()
    _bars(d, "LATE", [(100, 100, 100)])
    os.makedirs(os.path.join(d, DAY), exist_ok=True)
    open(os.path.join(d, DAY, "BAD.json"), "w").write("{not json")
    late = datetime(2026, 11, 17, 6, 0, 0)
    _, c = b.analyse([("LATE", 1, late, late), ("BAD", 1, late, late)], d, DAY)
    assert c["no_bars"] == 2


def test_window_and_report_text():
    lo, hi = b.window_utc(DAY)
    assert (lo.hour, lo.minute, hi.hour) == (3, 45, 10)
    rows, c = b.analyse([], tempfile.mkdtemp(), DAY)
    txt = b.format_report(DAY, "no_setup_window", 2.0, rows, c, 40)
    assert "symbols 0" in txt and "one day" in txt
