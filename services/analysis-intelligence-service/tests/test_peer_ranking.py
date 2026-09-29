"""
tests/test_peer_ranking.py — coverage for fundamental/peer_ranking.py

No network. `fetch_fundamentals_batch` and `compute_peer_relative` (both imported into
peer_ranking's namespace from peer_multi_quarter) are monkeypatched, so these tests only
exercise rank_against_peers' own logic: peer-list building, the combined-score formula,
ranking, and the peer_relative fallback.

The combined-score formula is pinned against hand-computed numbers:
    pe_component     = 100 / (1 + max(pe, 0.1) / 20)
    roe_component    = clamp(roe * 3, 0, 100)  if roe else 30
    growth_component = clamp(50 + (rev_g + profit_g) / 2, 0, 100)
    combined         = round(0.35*pe + 0.35*roe + 0.30*growth, 2)

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_peer_ranking.py -v
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fundamental"))

import pytest

import peer_multi_quarter as pmq
import peer_ranking as pr

URL = "http://market-data.test"


def fund(pe=None, roe=None, rg=None, pg=None, **extra):
    """A fundamentals payload using the primary (snake_case) keys."""
    d = {}
    if pe is not None:
        d["pe_ratio"] = pe
    if roe is not None:
        d["roe"] = roe
    if rg is not None:
        d["revenue_growth_yoy"] = rg
    if pg is not None:
        d["profit_growth_yoy"] = pg
    d.update(extra)
    return d


@pytest.fixture
def env(monkeypatch):
    """Patch the two network-backed helpers; expose what they were called with."""
    state = {
        "fetched": {},                 # what fetch_fundamentals_batch returns
        "batch_calls": [],
        "rel_calls": [],
        "rel_result": {"peer_score": 61.234},
        "rel_raises": None,
    }

    def fake_batch(market_data_url, symbols, *a, **k):
        state["batch_calls"].append((market_data_url, list(symbols)))
        return dict(state["fetched"])

    def fake_rel(symbol, stock_fund, market_data_url, peers=None):
        state["rel_calls"].append((symbol, stock_fund, market_data_url, peers))
        if state["rel_raises"] is not None:
            raise state["rel_raises"]
        return state["rel_result"]

    monkeypatch.setattr(pr, "fetch_fundamentals_batch", fake_batch)
    monkeypatch.setattr(pr, "compute_peer_relative", fake_rel)
    return state


def row(result, sym):
    return next(r for r in result["ranking_table"] if r["symbol"] == sym)


# ── combined-score formula ────────────────────────────────────────────────────

class TestCombinedFormula:
    @pytest.mark.parametrize("data,expected", [
        (fund(pe=20, roe=15, rg=10, pg=10), 51.25),
        (fund(pe=10, roe=20, rg=20, pg=40), 68.33),
        (fund(pe=40, roe=5, rg=-20, pg=-40), 22.92),
        (fund(pe=20, roe=100, rg=10, pg=10), 70.5),        # roe*3 clamped to 100
        (fund(pe=20, roe=-10, rg=10, pg=10), 35.5),        # negative roe -> 0
        (fund(pe=20, roe=15, rg=500, pg=500), 63.25),      # growth clamped to 100
        (fund(pe=20, roe=15, rg=-500, pg=-500), 33.25),    # growth clamped to 0
        (fund(pe=100, roe=30, rg=0, pg=0), 52.33),
    ])
    def test_self_row_combined(self, env, data, expected):
        out = pr.rank_against_peers("RELIANCE", data, URL, peers=["TCS"])
        assert row(out, "RELIANCE.NS")["combined"] == expected

    def test_row_fields_are_rounded_and_shaped(self, env):
        data = fund(pe=12.3456, roe=14.5678, rg=9.8765, pg=-3.2109)
        out = pr.rank_against_peers("RELIANCE", data, URL, peers=["TCS"])
        r = row(out, "RELIANCE.NS")
        assert set(r) == {"symbol", "pe", "roe", "rev_g", "profit_g", "combined", "is_self"}
        assert (r["pe"], r["roe"], r["rev_g"], r["profit_g"]) == (12.35, 14.57, 9.88, -3.21)
        assert r["is_self"] is True

    def test_missing_roe_is_neutral_thirty_not_zero(self, env):
        out = pr.rank_against_peers("A", fund(pe=20, rg=10, pg=10), URL, peers=["B"])
        no_roe = row(out, "A.NS")["combined"]
        out = pr.rank_against_peers("A", fund(pe=20, roe=-1, rg=10, pg=10), URL, peers=["B"])
        neg_roe = row(out, "A.NS")["combined"]
        assert no_roe == 46.0             # .35*50 + .35*30 + .3*60
        assert neg_roe < no_roe           # a real negative ROE scores below "unknown"

    def test_alias_keys_are_used_when_primary_keys_missing(self, env):
        data = {"trailingPE": 20, "returnOnEquity": 15, "revenueGrowth": 10, "earningsGrowth": 10}
        out = pr.rank_against_peers("A", data, URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 51.25

    def test_pe_alias_short_key(self, env):
        out = pr.rank_against_peers("A", {"pe": 20, "roe": 15, "revenue_growth_yoy": 10,
                                          "profit_growth_yoy": 10}, URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 51.25

    def test_primary_key_wins_over_alias(self, env):
        data = {"pe_ratio": 20, "trailingPE": 5, "roe": 15, "returnOnEquity": 99,
                "revenue_growth_yoy": 10, "revenueGrowth": 99,
                "profit_growth_yoy": 10, "earningsGrowth": 99}
        out = pr.rank_against_peers("A", data, URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 51.25

    def test_zero_primary_falls_through_to_alias(self, env):
        # `a or b or c`: a legitimate 0 in the primary key is treated as missing.
        data = {"pe_ratio": 0, "trailingPE": 20, "roe": 15, "revenue_growth_yoy": 10,
                "profit_growth_yoy": 10}
        out = pr.rank_against_peers("A", data, URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 51.25

    def test_non_numeric_values_are_treated_as_zero(self, env):
        data = {"pe_ratio": "N/A", "roe": "n/a", "revenue_growth_yoy": None, "profit_growth_yoy": float("nan")}
        out = pr.rank_against_peers("A", data, URL, peers=["B"])
        r = row(out, "A.NS")
        assert (r["pe"], r["roe"], r["rev_g"], r["profit_g"]) == (0.0, 0.0, 0.0, 0.0)
        assert r["combined"] == 60.33

    def test_empty_fundamentals_score_sixty_point_three(self, env):
        out = pr.rank_against_peers("A", {}, URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 60.33

    def test_negative_pe_is_scored_as_the_best_possible_pe(self, env):
        # Pinned quirk: max(pe, 0.1) means a loss-making company (negative PE) gets the
        # TOP pe_component, higher than a healthy PE of 20.
        healthy = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B"])
        loss = pr.rank_against_peers("A", fund(pe=-5, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert row(healthy, "A.NS")["combined"] == 51.25
        assert row(loss, "A.NS")["combined"] == 68.58
        assert row(loss, "A.NS")["pe"] == -5.0


# ── peer list construction ────────────────────────────────────────────────────

class TestPeerList:
    def test_symbol_is_normalised(self, env):
        out = pr.rank_against_peers("  reliance ", {}, URL, peers=["TCS"])
        assert out["symbol"] == "RELIANCE.NS"
        assert row(out, "RELIANCE.NS")["is_self"] is True

    def test_bo_suffix_is_preserved(self, env):
        out = pr.rank_against_peers("RELIANCE.BO", {}, URL, peers=["TCS.BO"])
        assert {r["symbol"] for r in out["ranking_table"]} == {"RELIANCE.BO", "TCS.BO"}

    def test_explicit_peers_are_normalised_and_self_prepended(self, env):
        env["fetched"] = {"TCS.NS": fund(pe=20), "INFY.NS": fund(pe=20)}
        pr.rank_against_peers("wipro", {}, URL, peers=["tcs", "infy"])
        assert env["batch_calls"] == [(URL, ["TCS.NS", "INFY.NS"])]

    def test_self_already_in_peers_is_not_duplicated_and_not_fetched(self, env):
        env["fetched"] = {"INFY.NS": fund(pe=20)}
        out = pr.rank_against_peers("TCS", {}, URL, peers=["TCS", "INFY"])
        assert env["batch_calls"] == [(URL, ["INFY.NS"])]
        assert out["total_compared"] == 2
        assert [r["symbol"] for r in out["ranking_table"] if r["is_self"]] == ["TCS.NS"]

    def test_default_peers_come_from_detected_sector(self, env):
        out = pr.rank_against_peers("LTIM", {"sector": "Information Technology"}, URL)
        assert out["sector"] == "IT"
        assert env["batch_calls"][0][1] == pmq.DEFAULT_PEERS["IT"]
        assert out["total_compared"] == 1 + len(pmq.DEFAULT_PEERS["IT"])

    def test_unknown_sector_uses_default_list(self, env):
        out = pr.rank_against_peers("XYZ", {"sector": "Basic Materials"}, URL)
        assert out["sector"] == "DEFAULT"
        assert env["batch_calls"][0][1] == pmq.DEFAULT_PEERS["DEFAULT"]

    def test_empty_peers_list_falls_back_to_sector_default(self, env):
        out = pr.rank_against_peers("XYZ", {"sector": "Bank"}, URL, peers=[])
        assert out["sector"] == "BANK"
        assert env["batch_calls"][0][1] == pmq.DEFAULT_PEERS["BANK"]

    def test_default_sector_list_containing_self_is_not_prepended(self, env):
        out = pr.rank_against_peers("TCS", {"sector": "Software"}, URL)
        assert out["total_compared"] == len(pmq.DEFAULT_PEERS["IT"])
        assert "TCS.NS" not in env["batch_calls"][0][1]

    def test_max_peers_truncates_but_keeps_self(self, env):
        out = pr.rank_against_peers("XYZ", {"sector": "Bank"}, URL, max_peers=2)
        assert out["total_compared"] == 3          # self + 2 peers
        assert env["batch_calls"][0][1] == pmq.DEFAULT_PEERS["BANK"][:2]
        assert out["self"]["symbol"] == "XYZ.NS"

    def test_default_max_peers_is_six(self, env):
        many = [f"S{i}" for i in range(20)]
        out = pr.rank_against_peers("XYZ", {}, URL, peers=many)
        assert out["total_compared"] == 7          # self + 6

    def test_self_pushed_past_max_peers_is_dropped_and_ranking_degrades(self, env):
        # Pinned quirk: when a caller-supplied peer list already contains the symbol
        # BEYOND position max_peers, the slice cuts it off. No row is marked is_self, so
        # rank falls back to "last" and `self` becomes the top-ranked PEER.
        env["fetched"] = {
            "A.NS": fund(pe=10, roe=20, rg=20, pg=40),      # 68.33
            "B.NS": fund(pe=20, roe=15, rg=10, pg=10),      # 51.25
        }
        out = pr.rank_against_peers("Z", {}, URL, peers=["A", "B", "C", "D", "Z"], max_peers=2)
        # C has no data (60.33) so it slots between A (68.33) and B (51.25).
        assert [r["symbol"] for r in out["ranking_table"]] == ["A.NS", "C.NS", "B.NS"]
        assert not any(r["is_self"] for r in out["ranking_table"])
        assert out["rank"] == out["total_compared"] == 3
        assert out["self"] is out["ranking_table"][0]
        assert out["self"]["symbol"] != "Z.NS"

    def test_non_self_peers_only_are_fetched(self, env):
        pr.rank_against_peers("A", {}, URL, peers=["B", "C"])
        assert env["batch_calls"] == [(URL, ["B.NS", "C.NS"])]


# ── ranking ───────────────────────────────────────────────────────────────────

class TestRanking:
    @pytest.fixture
    def three(self, env):
        env["fetched"] = {
            "GOOD.NS": fund(pe=10, roe=20, rg=20, pg=40),     # 68.33
            "MEH.NS": fund(pe=20, roe=15, rg=10, pg=10),      # 51.25
        }
        return env

    def test_best_self_is_rank_one(self, three):
        out = pr.rank_against_peers("STAR", fund(pe=8, roe=30, rg=30, pg=50), URL, peers=["GOOD", "MEH"])
        assert out["rank"] == 1
        assert out["rank_label"] == "#1 of 3 in DEFAULT"
        assert out["ranking_table"][0]["symbol"] == "STAR.NS"

    def test_middle_self(self, three):
        out = pr.rank_against_peers("MID", fund(pe=15, roe=15, rg=10, pg=10), URL, peers=["GOOD", "MEH"])
        assert [r["symbol"] for r in out["ranking_table"]] == ["GOOD.NS", "MID.NS", "MEH.NS"]
        assert out["rank"] == 2
        assert out["self"]["symbol"] == "MID.NS"

    def test_worst_self_is_last(self, three):
        out = pr.rank_against_peers("DUD", fund(pe=40, roe=5, rg=-20, pg=-40), URL, peers=["GOOD", "MEH"])
        assert out["rank"] == 3
        assert out["rank_label"] == "#3 of 3 in DEFAULT"
        assert out["ranking_table"][-1]["symbol"] == "DUD.NS"

    def test_table_is_sorted_descending_by_combined(self, three):
        out = pr.rank_against_peers("DUD", fund(pe=40, roe=5, rg=-20, pg=-40), URL, peers=["GOOD", "MEH"])
        scores = [r["combined"] for r in out["ranking_table"]]
        assert scores == sorted(scores, reverse=True)

    def test_ties_favour_self_because_it_is_listed_first(self, env):
        same = fund(pe=20, roe=15, rg=10, pg=10)
        env["fetched"] = {"B.NS": dict(same), "C.NS": dict(same)}
        out = pr.rank_against_peers("A", dict(same), URL, peers=["B", "C"])
        assert out["rank"] == 1
        assert out["ranking_table"][0]["symbol"] == "A.NS"

    def test_peer_whose_fetch_failed_outranks_a_decent_real_peer(self, env):
        # Pinned quirk: a peer that returns no data (fetch failure -> {}) scores 60.33
        # (pe=0 looks like the best PE, roe unknown = 30), which beats a perfectly
        # healthy PE 20 / ROE 15 / growth 10% company (51.25).
        env["fetched"] = {"GONE.NS": {}}          # missing key handled the same way
        out = pr.rank_against_peers("REAL", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["GONE", "ALSOGONE"])
        assert row(out, "GONE.NS")["combined"] == 60.33
        assert row(out, "ALSOGONE.NS")["combined"] == 60.33
        assert row(out, "REAL.NS")["combined"] == 51.25
        assert out["rank"] == 3

    def test_only_self_in_table_is_rank_one_of_one(self, env):
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["A"])
        assert out["rank"] == 1
        assert out["total_compared"] == 1
        assert out["rank_label"] == "#1 of 1 in DEFAULT"

    def test_max_peers_negative_yields_empty_table(self, env):
        # Pinned edge: slicing to [:0] leaves nothing to rank.
        out = pr.rank_against_peers("A", {}, URL, peers=["B"], max_peers=-1)
        assert out["ranking_table"] == []
        assert out["self"] == {}
        assert out["rank"] == 0
        assert out["rank_label"] == "#0 of 0 in DEFAULT"
        assert out["peer_score"] == 61.23         # from peer_relative, not the empty self row


# ── peer_relative / peer_score ────────────────────────────────────────────────

class TestPeerScore:
    def test_peer_score_comes_from_peer_relative_and_is_rounded(self, env):
        env["rel_result"] = {"peer_score": 72.3456, "other": 1}
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B"])
        assert out["peer_score"] == 72.35
        assert out["peer_relative"] == {"peer_score": 72.3456, "other": 1}

    def test_compute_peer_relative_gets_original_arguments(self, env):
        data = fund(pe=20)
        pr.rank_against_peers("a", data, URL, peers=["b", "c"], max_peers=1)
        assert env["rel_calls"] == [("A.NS", data, URL, ["b", "c"])]

    def test_peers_none_is_forwarded_as_none(self, env):
        pr.rank_against_peers("A", {}, URL)
        assert env["rel_calls"][0][3] is None

    def test_missing_peer_score_key_falls_back_to_self_combined(self, env):
        env["rel_result"] = {"something_else": 1}
        out = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert out["peer_score"] == 51.25

    def test_exception_falls_back_to_self_combined_and_logs(self, env, caplog):
        env["rel_raises"] = RuntimeError("market-data down")
        with caplog.at_level(logging.WARNING, logger="peer_ranking"):
            out = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert out["peer_score"] == 51.25
        assert out["peer_relative"] == {}
        assert any("peer_relative in ranking failed" in r.getMessage() for r in caplog.records)

    def test_ranking_still_returned_when_peer_relative_fails(self, env):
        env["rel_raises"] = ValueError("boom")
        env["fetched"] = {"B.NS": fund(pe=20, roe=15, rg=10, pg=10)}
        out = pr.rank_against_peers("A", fund(pe=10, roe=20, rg=20, pg=40), URL, peers=["B"])
        assert out["rank"] == 1
        assert out["total_compared"] == 2


# ── response shape ────────────────────────────────────────────────────────────

class TestResponseShape:
    def test_full_shape_and_static_metrics_block(self, env):
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B"])
        assert set(out) == {
            "symbol", "sector", "peer_score", "rank", "total_compared", "rank_label",
            "self", "ranking_table", "peer_relative", "metrics",
        }
        assert out["metrics"] == {
            "pe_weight": 0.35,
            "roe_weight": 0.35,
            "growth_weight": 0.30,
            "note": "Lower PE and higher ROE/growth rank better",
        }
        assert out["metrics"]["pe_weight"] + out["metrics"]["roe_weight"] + out["metrics"]["growth_weight"] == pytest.approx(1.0)

    def test_self_is_the_row_object_from_the_table(self, env):
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B"])
        assert out["self"] is row(out, "A.NS")

    def test_peer_score_is_a_float(self, env):
        env["rel_result"] = {"peer_score": 70}
        out = pr.rank_against_peers("A", {}, URL, peers=["B"])
        assert isinstance(out["peer_score"], float)
        assert out["peer_score"] == 70.0

    def test_module_logger_name(self):
        assert pr.logger.name == "peer_ranking"
