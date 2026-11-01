"""E273 round 5: the tape verdict also carries absorption and exhaustion (S3 in position-stocks reads them). Plain runner: python3 tests/test_e273_tape_verdict_keys.py"""
import os
import sys
import types

if "pytest" not in sys.modules:                                   # the E268 helpers import pytest at the top; only their feed builders are used here
    sys.modules["pytest"] = types.SimpleNamespace(mark=types.SimpleNamespace(parametrize=lambda *a, **k: (lambda f: f)), fixture=lambda *a, **k: (lambda f: f), raises=None, skip=lambda *a, **k: None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import test_e268_order_flow_tape as base  # noqa: E402
import order_flow_tape as t  # noqa: E402


def test_verdict_carries_absorption_and_exhaustion_keys():
    base._feed(60, buy=True)
    v = t.verdict("ABC", "LONG", now=base.T0 + base.timedelta(seconds=130))
    assert v["available"] is True
    assert "absorption" in v and "exhaustion" in v and "hvn_below" in v and "checks" in v, sorted(v)


def test_an_unavailable_symbol_still_has_the_old_shape():
    t._reset_for_tests()
    v = t.verdict("NOPE", "LONG")
    assert v["available"] is False and v["block"] is False


if __name__ == "__main__":
    ok = bad = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); ok += 1
            except Exception as e:  # noqa: BLE001
                bad += 1; print("FAIL", name, type(e).__name__, e)
    print(f"{ok} passed, {bad} failed")
    sys.exit(1 if bad else 0)
