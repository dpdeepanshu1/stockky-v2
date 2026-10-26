"""E208: a loss-making company (negative PE) must not score better than a richly valued profitable one. It was neutral 50 (above PE 28-50 -> 48);
it is now 40: below PE 28-50, above the PE 50+ bucket (35). A stored fundamental_score is still used as given."""
import importlib.util
import os
import sys

import pytest

pytest.importorskip("fastapi")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)


@pytest.fixture(scope="module")
def m():
    spec = importlib.util.spec_from_file_location("decision_main_e208", os.path.join(_ROOT, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _score(m, payload):
    return m._derive_fundamental_from_payload(payload)["fundamental_score"]


@pytest.mark.parametrize("pe", [-0.5, -3, -40, -500])
def test_negative_pe_is_40(m, pe):
    assert _score(m, {"pe_ratio": pe}) == 40


def test_ordering_loss_making_between_rich_and_very_rich(m):
    assert _score(m, {"pe_ratio": 50}) > _score(m, {"pe_ratio": -5}) > _score(m, {"pe_ratio": 60})


def test_stored_score_still_wins(m):
    assert _score(m, {"pe_ratio": -5, "fundamental_score": 71}) == 71


def test_other_buckets_unchanged(m):
    assert [_score(m, {"pe_ratio": pe}) for pe in (5, 10, 35, 60)] == [55, 62, 48, 35]
