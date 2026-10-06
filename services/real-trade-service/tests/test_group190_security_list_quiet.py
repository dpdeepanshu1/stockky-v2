"""
group190 (item 18): dhanhq's fetch_security_list emits pandas "DtypeWarning: Columns (...) have mixed types" on every
security-master load. It is silenced for that one call only; other warnings still come through.

Run from services/real-trade-service:
    python3 -m pytest tests/test_group190_security_list_quiet.py -q
"""
from __future__ import annotations
import os, sys, warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from execution import dhan_client as dc


class _Dtype(Warning):
    pass


class _Sdk:
    def __init__(self, *msgs):
        self.msgs = msgs
        self.modes = []

    def fetch_security_list(self, mode=None):
        self.modes.append(mode)
        for m in self.msgs:
            warnings.warn(m, _Dtype)
        return "DF"


def _record(fn):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        out = fn()
    return out, [str(x.message) for x in w]


def test_mixed_types_warning_is_silenced_and_result_returned():
    sdk = _Sdk("Columns (3,5) have mixed types. Specify dtype option on import or set low_memory=False.")
    out, seen = _record(lambda: dc._fetch_security_list_quiet(sdk))
    assert out == "DF" and seen == [] and sdk.modes == ["compact"]


def test_newer_pandas_wording_is_silenced_too():
    sdk = _Sdk("Columns (0: a, 7: SEM_LOT_UNITS) have mixed types. Specify dtype option on import or set low_memory=False.")
    _, seen = _record(lambda: dc._fetch_security_list_quiet(sdk))
    assert seen == []


def test_other_warnings_are_not_swallowed():
    sdk = _Sdk("something else is deprecated")
    _, seen = _record(lambda: dc._fetch_security_list_quiet(sdk))
    assert seen == ["something else is deprecated"]


def test_filter_does_not_leak_after_the_call():
    sdk = _Sdk()
    before = list(warnings.filters)
    dc._fetch_security_list_quiet(sdk)
    assert list(warnings.filters) == before


def test_sdk_exception_still_propagates():
    class _Boom:
        def fetch_security_list(self, mode=None):
            raise RuntimeError("SDK down")
    with pytest.raises(RuntimeError):
        dc._fetch_security_list_quiet(_Boom())
