"""Round 607: the per-symbol yfinance gate for /quotes/bulk (yf_bulk_gate.py) and its wiring in main.py.

Plain runner: `python tests/test_round607_yf_bulk_gate.py` (also collected by pytest).
main.py cannot be imported without fastapi etc., so the wiring is checked on its source text."""
from __future__ import annotations

import os
import re
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import yf_bulk_gate as g  # noqa: E402

_ENV = "QUOTE_BULK_YF_MIN_INTERVAL_S"


def _env(v):
    if v is None:
        os.environ.pop(_ENV, None)
    else:
        os.environ[_ENV] = v


def _fresh(v=None):
    g.reset()
    _env(v)


def _never(_s):
    return False


def test_default_interval_is_30_and_bad_values_fall_back():
    _fresh()
    assert g.min_interval_s() == 30.0
    for v in ("", "  ", "abc", "-1", "nan", "99999"):
        _env(v)
        assert g.min_interval_s() == 30.0, v
    _env("0")
    assert g.min_interval_s() == 0.0
    _env("12.5")
    assert g.min_interval_s() == 12.5
    _env(None)


def test_first_ask_goes_second_within_interval_is_held_back():
    _fresh()
    send, held = g.split(["AAA.NS", "BBB.NS"], _never, now=1000.0)
    assert send == ["AAA.NS", "BBB.NS"] and held == []
    send, held = g.split(["AAA.NS", "BBB.NS", "CCC.NS"], _never, now=1010.0)
    assert send == ["CCC.NS"] and held == ["AAA.NS", "BBB.NS"]


def test_symbol_goes_again_after_the_interval():
    _fresh()
    g.split(["AAA.NS"], _never, now=1000.0)
    assert g.split(["AAA.NS"], _never, now=1029.9)[1] == ["AAA.NS"]
    send, held = g.split(["AAA.NS"], _never, now=1030.0)
    assert send == ["AAA.NS"] and held == []


def test_a_concurrent_call_sees_the_stamp_taken_before_the_download():
    _fresh()
    out = []
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        out.append(g.split(["AAA.NS", "BBB.NS", "CCC.NS"], _never))

    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    sent = [s for (s, _h) in out]
    assert sum(len(s) for s in sent) == 3, sent      # each name asked exactly once across 8 simultaneous calls


def test_held_and_index_bypass_and_an_error_in_bypass_counts_as_held():
    _fresh()
    byp = lambda s: s.startswith("^") or s.startswith("HELD")
    g.split(["^NSEI", "HELD1.NS", "OTHER.NS"], byp, now=1000.0)
    send, held = g.split(["^NSEI", "HELD1.NS", "OTHER.NS"], byp, now=1001.0)
    assert send == ["^NSEI", "HELD1.NS"] and held == ["OTHER.NS"]

    def boom(_s):
        raise RuntimeError("x")

    send, held = g.split(["ZZZ.NS"], boom, now=1002.0)
    assert send == ["ZZZ.NS"] and held == []
    send, held = g.split(["ZZZ.NS"], boom, now=1003.0)
    assert send == ["ZZZ.NS"] and held == []


def test_gate_off_sends_everything_and_stamps_nothing():
    _fresh("0")
    for _ in range(3):
        send, held = g.split(["AAA.NS"], _never, now=1000.0)
        assert send == ["AAA.NS"] and held == []
    _env("30")
    assert g.split(["AAA.NS"], _never, now=1000.0)[1] == []   # nothing was stamped while it was off
    _env(None)


def test_suffix_and_case_share_one_stamp_and_counters_move():
    _fresh()
    g.split(["abc.NS"], _never, now=1000.0)
    send, held = g.split(["ABC.BO"], _never, now=1001.0)
    assert send == [] and held == ["ABC.BO"]
    assert g.STATS["sent"] == 1 and g.STATS["throttled"] == 1


def test_empty_input_and_old_keys_are_pruned():
    _fresh()
    assert g.split([], _never) == ([], [])
    g._LAST.update({f"S{i}": 0.0 for i in range(g._MAX_KEYS + 5)})
    g.split(["NEW.NS"], _never, now=500.0)
    assert len(g._LAST) <= 2 and "NEW" in g._LAST


def test_wiring_in_main_py_is_present_and_in_the_right_place():
    src = open(os.path.join(os.path.dirname(_HERE), "main.py"), encoding="utf-8").read()
    assert "import yf_bulk_gate as _yf_gate" in src
    i_gate = src.index("_yf_gate.split(")
    i_small = src.index("small-leftover gate failed")
    i_empty = src.index("# Everything was already fresh (cache, live feed, or AngelOne REST)")
    i_dl = src.index("data = yf.download(\n            tickers=\" \".join(yf_tickers)")
    assert i_small < i_gate < i_empty < i_dl            # after the other leftover gates, before the empty check and the download
    block = src[i_gate - 400:i_gate + 1500]
    assert "startswith(\"^\")" in block and "_cooldown_symbol_held" in block   # indices and held symbols bypass
    assert "yf_tickers = _send_yf" in block
    assert re.search(r"_yf_gate\.min_interval_s\(\)", src)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
