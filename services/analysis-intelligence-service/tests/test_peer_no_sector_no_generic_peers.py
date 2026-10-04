"""group101 item 10: a payload with no sector / industry / sectorDisp is not compared with the generic big-cap list."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "fundamental"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import peer_multi_quarter as pmq  # noqa: E402
import peer_ranking as pr  # noqa: E402

URL = "http://mds/"


@pytest.fixture
def calls(monkeypatch):
    seen = []

    def fake_batch(url, syms, timeout=15):
        seen.append(list(syms))
        return {s: {"pe_ratio": 20.0, "roe": 15.0} for s in syms}

    monkeypatch.setattr(pmq, "fetch_fundamentals_batch", fake_batch)
    monkeypatch.setattr(pr, "fetch_fundamentals_batch", fake_batch)
    return seen


@pytest.mark.parametrize("fund", [{}, {"sector": None}, {"sector": "", "industry": "  "}, {"sector": 5, "industry": ["x"]}, None])
def test_has_sector_data_false(fund):
    assert pmq.has_sector_data(fund) is False


@pytest.mark.parametrize("fund", [{"sector": "Agriculture"}, {"industry": "Capital Markets"}, {"sectorDisp": "Tech"}])
def test_has_sector_data_true_even_if_unrecognised(fund):
    assert pmq.has_sector_data(fund) is True


def test_ranking_without_sector_data_compares_nobody(calls):
    out = pr.rank_against_peers("SSRETAIL", {"pe_ratio": 12.0}, URL)
    assert out["sector"] == "UNKNOWN"
    assert out["peers_compared"] == 0 and out["total_compared"] == 1
    assert out["rank_label"] == "No peers compared in UNKNOWN"
    assert all(not c for c in calls) or calls == []      # nothing fetched, in particular no DEFAULT list
    flat = [s for c in calls for s in c]
    assert not set(flat) & set(pmq.DEFAULT_PEERS["DEFAULT"])


def test_peer_relative_without_sector_data_uses_no_peers_and_is_neutral(calls):
    out = pmq.compute_peer_relative("SSRETAIL", {"pe_ratio": 12.0}, URL)
    assert out["peers_used"] == [] and out["peer_score"] == pytest.approx(50.0)
    assert [s for c in calls for s in c] == []


def test_explicit_peers_are_still_honoured_without_sector_data(calls):
    out = pr.rank_against_peers("XYZ", {}, URL, peers=["TCS", "INFY"])
    assert calls[0] == ["TCS.NS", "INFY.NS"]
    assert out["peers_compared"] == 2


def test_present_but_unrecognised_sector_keeps_the_default_list(calls):
    out = pr.rank_against_peers("XYZ", {"sector": "Basic Materials"}, URL)
    assert out["sector"] == "DEFAULT"
    assert calls[0] == pmq.DEFAULT_PEERS["DEFAULT"]


def test_recognised_sector_is_unchanged(calls):
    out = pr.rank_against_peers("XYZ", {"sector": "Information Technology"}, URL)
    assert out["sector"] == "IT" and calls[0] == pmq.DEFAULT_PEERS["IT"]
