"""E193: `_derive_fundamental_from_payload` (used when a payload has a PE but no stored fundamental_score) was not monotone above PE 28:
PE 28-40 -> 48, PE 40-50 -> 50 (fell through to the default), PE 50+ -> 35. A PE of 45 scored better than a PE of 35. 28 < PE <= 50 is now 48.
Other buckets unchanged.
"""
import importlib.util
import os
import sys

import pytest

pytest.importorskip("fastapi")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("decision_main_e193", os.path.join(_ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _score(m, pe):
    return m._derive_fundamental_from_payload({"pe_ratio": pe})["fundamental_score"]


@pytest.mark.parametrize("pe, expected", [
    (10, 62), (28, 62), (28.5, 48), (35, 48), (40, 48), (41, 48), (45, 48), (50, 48), (50.5, 35), (80, 35),
    (5, 55), (-3, 40),   # E208: negative PE is 40 now (was 50)
])
def test_pe_buckets(m, pe, expected):
    assert _score(m, pe) == expected


def test_never_better_above_28(m):
    scores = [_score(m, pe) for pe in (28.5, 32, 36, 40, 42, 45, 48, 50, 55, 70)]
    assert scores == sorted(scores, reverse=True)


def test_stored_score_still_wins(m):
    assert m._derive_fundamental_from_payload({"pe_ratio": 45, "fundamental_score": 71})["fundamental_score"] == 71
