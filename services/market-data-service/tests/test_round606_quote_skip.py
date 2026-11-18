"""Round 606: QUOTE_SKIP_SYMBOLS and the once-a-day note (quote_skip.py).

Plain runner: `python tests/test_round606_quote_skip.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

import quote_skip as qs  # noqa: E402


def _env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def test_skip_list_default_is_empty():
    _env("QUOTE_SKIP_SYMBOLS", None)
    assert qs.skip_symbols() == set()
    for v in ("", "  ", "none", "OFF", "0"):
        _env("QUOTE_SKIP_SYMBOLS", v)
        assert qs.skip_symbols() == set(), v
    _env("QUOTE_SKIP_SYMBOLS", None)


def test_skip_list_parses_commas_spaces_suffixes_and_case():
    _env("QUOTE_SKIP_SYMBOLS", "crystalcp, DIGIKORE.NS;greenasia  MVKAGRO.BO,,URBAN")
    assert qs.skip_symbols() == {"CRYSTALCP", "DIGIKORE", "GREENASIA", "MVKAGRO", "URBAN"}
    _env("QUOTE_SKIP_SYMBOLS", None)


def test_daily_note_lets_one_through_per_interval():
    _env("T606_EVERY", "0.05")
    n = qs.DailyNote("T606_EVERY", 86400.0)
    assert n.allow() is True and n.allow() is False and n.allow() is False
    time.sleep(0.06)
    assert n.allow() is True and n.allow() is False
    _env("T606_EVERY", None)


def test_daily_note_default_is_one_day_and_zero_means_every_call():
    _env("T606_EVERY", None)
    n = qs.DailyNote("T606_EVERY", 86400.0)
    assert n.every_s() == 86400.0 and n.allow() is True and n.allow() is False
    _env("T606_EVERY", "0")
    assert n.every_s() == 0.0 and n.allow() is True and n.allow() is True       # the old per-call behaviour
    _env("T606_EVERY", "junk")
    assert n.every_s() == 86400.0
    _env("T606_EVERY", "-5")
    assert n.every_s() == 86400.0
    _env("T606_EVERY", None)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
