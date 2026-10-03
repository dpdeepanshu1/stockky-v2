"""
tests/test_peer_ranking.py — coverage for fundamental/peer_ranking.py

No network. `fetch_fundamentals_batch` and `compute_peer_relative` (both imported into
peer_ranking's namespace from peer_multi_quarter) are monkeypatched, so these tests only
exercise rank_against_peers' own logic: peer-list building, the combined-score formula,
ranking, and the peer_relative fallback.

The combined-score formula is pinned against hand-computed numbers:
    pe_component     = 100 / (1 + pe / 20)   if pe > 0
                       0                      if pe < 0   (loss-making)
                       50                     if pe == 0  (missing / unknown)
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
        "rel_max_peers": [],
        "rel_result": {"peer_score": 61.234},
        "rel_raises": None,
    }

    def fake_batch(market_data_url, symbols, *a, **k):
        state["batch_calls"].append((market_data_url, list(symbols)))
        # A peer not listed in state["fetched"] answers with a non-empty payload that has no
        # usable metrics (market-data knew the symbol); a failed fetch is modelled explicitly
        # as `state["fetched"][sym] = {}`, which rank_against_peers now skips.
        return {s: state["fetched"].get(s, {"name": s}) for s in symbols}

    def fake_rel(symbol, stock_fund, market_data_url, peers=None, max_peers=None):
        state["rel_calls"].append((symbol, stock_fund, market_data_url, peers))
        state["rel_max_peers"].append(max_peers)
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
        assert r["combined"] == 43.0     # all-unknown = neutral PE 50, unknown ROE 30, growth 50

    def test_empty_fundamentals_score_neutral_forty_three(self, env):
        # .35*50 (unknown PE = neutral) + .35*30 (unknown ROE) + .30*50 (no growth) = 43.0
        out = pr.rank_against_peers("A", {}, URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 43.0

    def test_negative_pe_is_scored_as_the_worst_pe(self, env):
        # A loss-making company (negative PE) gets pe_component 0, not the top score.
        healthy = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B"])
        loss = pr.rank_against_peers("A", fund(pe=-5, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert row(healthy, "A.NS")["combined"] == 51.25
        assert row(loss, "A.NS")["combined"] == 33.75   # .35*0 + .35*45 + .3*60
        assert row(loss, "A.NS")["combined"] < row(healthy, "A.NS")["combined"]
        assert row(loss, "A.NS")["pe"] == -5.0

    @pytest.mark.parametrize("pe", [-0.01, -5, -500])
    def test_any_negative_pe_gets_zero_pe_component(self, env, pe):
        out = pr.rank_against_peers("A", fund(pe=pe, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 33.75

    def test_missing_pe_is_neutral_not_best(self, env):
        # No PE at all -> same pe_component as a PE of 20 (50), not the top score.
        missing = pr.rank_against_peers("A", fund(roe=15, rg=10, pg=10), URL, peers=["B"])
        assert row(missing, "A.NS")["combined"] == 51.25

    def test_zero_pe_is_treated_as_missing_neutral(self, env):
        out = pr.rank_against_peers("A", fund(pe=0, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 51.25

    def test_very_low_positive_pe_still_scores_near_the_top(self, env):
        # pe 0.5 -> 100 / 1.025 = 97.56; .35*97.56 + .35*45 + .3*60 = 67.9
        out = pr.rank_against_peers("A", fund(pe=0.5, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert row(out, "A.NS")["combined"] == 67.9

    def test_ordering_by_pe_is_loss_lt_expensive_lt_cheap(self, env):
        def c(pe):
            out = pr.rank_against_peers("A", fund(pe=pe, roe=15, rg=10, pg=10), URL, peers=["B"])
            return row(out, "A.NS")["combined"]
        assert c(-5) < c(80) < c(20) < c(5)


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

    def test_default_max_peers_is_five_same_as_compute_peer_relative(self, env):
        many = [f"S{i}" for i in range(20)]
        out = pr.rank_against_peers("XYZ", {}, URL, peers=many)
        assert out["total_compared"] == 6          # self + 5 (was self + 6)
        assert pr.DEFAULT_MAX_PEERS == pmq.DEFAULT_MAX_PEERS == 5
        # ...and the same limit is what the peer-relative score is computed with
        assert env["rel_max_peers"] == [5]

    def test_self_listed_past_max_peers_is_still_included(self, env):
        # The symbol is always kept (and put first) even when the caller's list had it
        # beyond position max_peers; it is no longer sliced off and replaced by a peer.
        env["fetched"] = {
            "A.NS": fund(pe=10, roe=20, rg=20, pg=40),      # 68.33
            "B.NS": fund(pe=20, roe=15, rg=10, pg=10),      # 51.25
        }
        out = pr.rank_against_peers("Z", {}, URL, peers=["A", "B", "C", "D", "Z"], max_peers=2)
        # Z has no data (43.0) so it ranks last behind A (68.33) and B (51.25).
        assert [r["symbol"] for r in out["ranking_table"]] == ["A.NS", "B.NS", "Z.NS"]
        assert [r["symbol"] for r in out["ranking_table"] if r["is_self"]] == ["Z.NS"]
        assert out["rank"] == out["total_compared"] == 3
        assert out["self"]["symbol"] == "Z.NS"
        assert env["batch_calls"] == [(URL, ["A.NS", "B.NS"])]

    def test_max_peers_counts_peers_only_when_self_is_in_the_list(self, env):
        out = pr.rank_against_peers("TCS", {}, URL, peers=["INFY", "TCS", "WIPRO", "HCLTECH"], max_peers=2)
        assert env["batch_calls"] == [(URL, ["INFY.NS", "WIPRO.NS"])]
        assert out["total_compared"] == 3

    def test_duplicate_peers_are_removed(self, env):
        out = pr.rank_against_peers("A", {}, URL, peers=["B", "C", "B"])
        assert env["batch_calls"] == [(URL, ["B.NS", "C.NS"])]
        assert [r["symbol"] for r in out["ranking_table"]].count("B.NS") == 1
        assert out["total_compared"] == 3

    def test_duplicates_differing_only_by_case_or_suffix_are_removed(self, env):
        out = pr.rank_against_peers("A", {}, URL, peers=["b", "B", "B.NS", " b.ns "])
        assert env["batch_calls"] == [(URL, ["B.NS"])]
        assert out["total_compared"] == 2

    def test_bo_and_ns_listings_are_distinct_peers(self, env):
        out = pr.rank_against_peers("A", {}, URL, peers=["B.NS", "B.BO"])
        assert env["batch_calls"] == [(URL, ["B.NS", "B.BO"])]
        assert out["total_compared"] == 3

    def test_self_listed_twice_in_different_spellings_appears_once(self, env):
        out = pr.rank_against_peers("tcs", {}, URL, peers=["TCS", "tcs.ns", "INFY"])
        assert env["batch_calls"] == [(URL, ["INFY.NS"])]
        assert out["total_compared"] == 2
        assert [r["symbol"] for r in out["ranking_table"] if r["is_self"]] == ["TCS.NS"]

    def test_duplicates_do_not_consume_max_peers_slots(self, env):
        out = pr.rank_against_peers("A", {}, URL, peers=["B", "B", "B", "C", "D"], max_peers=2)
        assert env["batch_calls"] == [(URL, ["B.NS", "C.NS"])]
        assert out["total_compared"] == 3

    def test_duplicate_peers_are_not_double_counted_in_the_ranking(self, env):
        env["fetched"] = {"B.NS": fund(pe=10, roe=20, rg=20, pg=40)}     # 68.33
        out = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B", "B", "B"])
        assert [r["symbol"] for r in out["ranking_table"]] == ["B.NS", "A.NS"]
        assert out["rank"] == 2 and out["rank_label"] == "#2 of 2 in DEFAULT"

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

    def test_peer_whose_fetch_failed_is_left_out_not_ranked(self, env):
        # A failed fetch (fetch_fundamentals -> {}) once scored 60.33 and beat a healthy
        # company; then it was listed with a made-up neutral 43.0. It is now skipped, the
        # same way compute_peer_relative skips it.
        env["fetched"] = {"GONE.NS": {}, "ALSOGONE.NS": {}}
        out = pr.rank_against_peers("REAL", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["GONE", "ALSOGONE"])
        assert [r["symbol"] for r in out["ranking_table"]] == ["REAL.NS"]
        assert out["peers_skipped"] == ["GONE.NS", "ALSOGONE.NS"]
        assert out["total_compared"] == 1 and out["rank"] == 1
        assert out["rank_label"] == "No peers compared in DEFAULT" and out["peers_compared"] == 0

    def test_failed_peer_does_not_sit_above_a_genuinely_weak_peer(self, env):
        # Previously pinned as a known limit: the unknown peer (43.0) ranked above a real but
        # weak company (22.92). The unknown peer is gone now, so the weak one is simply last.
        env["fetched"] = {"WEAK.NS": fund(pe=40, roe=5, rg=-20, pg=-40), "GONE.NS": {}}
        out = pr.rank_against_peers("REAL", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["WEAK", "GONE"])
        assert [r["symbol"] for r in out["ranking_table"]] == ["REAL.NS", "WEAK.NS"]
        assert row(out, "WEAK.NS")["combined"] == 22.92
        assert out["peers_skipped"] == ["GONE.NS"]
        assert out["total_compared"] == 2

    def test_peer_missing_from_the_batch_result_is_skipped_too(self, env, monkeypatch):
        monkeypatch.setattr(pr, "fetch_fundamentals_batch", lambda url, syms, *a, **k: {})
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B", "C"])
        assert out["peers_skipped"] == ["B.NS", "C.NS"] and out["total_compared"] == 1

    def test_peer_with_a_payload_but_no_usable_metrics_is_still_ranked_neutral(self, env):
        # Same rule as compute_peer_relative (`if not f: continue`): only an EMPTY payload is
        # a failed fetch. A non-empty payload with no P/E / ROE / growth scores neutral 43.0.
        env["fetched"] = {"B.NS": {"name": "B Ltd"}}
        out = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B"])
        assert row(out, "B.NS")["combined"] == 43.0
        assert out["peers_skipped"] == [] and out["total_compared"] == 2

    def test_stock_with_empty_fundamentals_is_never_skipped(self, env):
        env["fetched"] = {"B.NS": fund(pe=20, roe=15, rg=10, pg=10)}
        out = pr.rank_against_peers("A", {}, URL, peers=["B"])
        assert row(out, "A.NS")["is_self"] is True and row(out, "A.NS")["combined"] == 43.0
        assert out["peers_skipped"] == []
        assert out["self"]["symbol"] == "A.NS"

    def test_failed_peers_still_use_up_max_peers_slots(self, env):
        # Same as compute_peer_relative: the cap is applied before fetching.
        env["fetched"] = {"B.NS": {}, "C.NS": fund(pe=20), "D.NS": fund(pe=20)}
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B", "C", "D"], max_peers=2)
        assert out["peers_skipped"] == ["B.NS"]
        assert [r["symbol"] for r in out["ranking_table"] if not r["is_self"]] == ["C.NS"]

    def test_skipped_peers_keep_their_order_and_are_normalised(self, env):
        env["fetched"] = {"X.NS": {}, "Y.NS": {}}
        out = pr.rank_against_peers("A", {}, URL, peers=["y", "x", "Y.NS", "Z"])
        assert out["peers_skipped"] == ["Y.NS", "X.NS"]

    def test_all_peers_failed_ranks_the_stock_alone(self, env):
        env["fetched"] = {"B.NS": {}, "C.NS": {}}
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B", "C"])
        assert (out["rank"], out["total_compared"]) == (1, 1)
        assert out["self"]["symbol"] == "A.NS" and out["peers_skipped"] == ["B.NS", "C.NS"]

    def test_peers_compared_counts_ranked_peers_and_drives_the_label(self, env):
        env["fetched"] = {"B.NS": fund(pe=20), "C.NS": {}, "D.NS": fund(pe=25)}
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B", "C", "D"])
        assert out["peers_compared"] == 2 and out["total_compared"] == 3
        assert out["rank_label"].endswith("of 3 in DEFAULT") and "No peers" not in out["rank_label"]

    def test_all_peers_failed_label_says_no_peers_compared(self, env):
        env["fetched"] = {"B.NS": {}, "C.NS": {}}
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B", "C"])
        assert out["peers_compared"] == 0
        assert out["rank_label"] == "No peers compared in DEFAULT"
        assert (out["rank"], out["total_compared"]) == (1, 1)    # numeric fields unchanged

    def test_peers_skipped_is_empty_when_every_peer_returned_data(self, env):
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["B", "C"])
        assert out["peers_skipped"] == []

    def test_ranking_peers_match_compute_peer_relative_peers_used(self, monkeypatch):
        """Real compute_peer_relative, one shared fetch result: both see the same peers."""
        data = {"B.NS": fund(pe=20, roe=15, rg=10, pg=10), "C.NS": {}, "D.NS": fund(pe=25, roe=10, rg=5, pg=5)}

        def fake_batch(url, syms, timeout=15.0):
            return {s: data.get(s, {}) for s in syms}

        monkeypatch.setattr(pmq, "fetch_fundamentals_batch", fake_batch)
        monkeypatch.setattr(pr, "fetch_fundamentals_batch", fake_batch)
        out = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B", "C", "D"])
        ranked_peers = sorted(r["symbol"] for r in out["ranking_table"] if not r["is_self"])
        assert ranked_peers == sorted(out["peer_relative"]["peers_used"]) == ["B.NS", "D.NS"]
        assert out["peers_skipped"] == ["C.NS"]

    def test_loss_making_peer_ranks_below_a_healthy_peer(self, env):
        env["fetched"] = {"LOSS.NS": fund(pe=-8, roe=15, rg=10, pg=10)}
        out = pr.rank_against_peers("REAL", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["LOSS"])
        assert [r["symbol"] for r in out["ranking_table"]] == ["REAL.NS", "LOSS.NS"]
        assert out["rank"] == 1

    def test_only_self_in_table_is_rank_one_of_one(self, env):
        out = pr.rank_against_peers("A", fund(pe=20), URL, peers=["A"])
        assert out["rank"] == 1
        assert out["total_compared"] == 1
        assert out["rank_label"] == "No peers compared in DEFAULT" and out["peers_compared"] == 0

    @pytest.mark.parametrize("n", [0, -1, -10])
    def test_max_peers_zero_or_negative_ranks_self_alone(self, env, n):
        # No peers requested -> the table is just the symbol (it used to be empty, with
        # rank 0, "#0 of 0" and an empty `self`).
        out = pr.rank_against_peers("A", {}, URL, peers=["B"], max_peers=n)
        assert [r["symbol"] for r in out["ranking_table"]] == ["A.NS"]
        assert out["self"]["symbol"] == "A.NS"
        assert out["rank"] == 1
        assert out["total_compared"] == 1
        assert out["rank_label"] == "No peers compared in DEFAULT" and out["peers_compared"] == 0
        assert out["peer_score"] == 61.23         # from peer_relative
        assert env["batch_calls"] == [(URL, [])]


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
            "symbol", "sector", "peer_score", "rank", "total_compared", "peers_compared", "peers_skipped",
            "rank_label", "self", "ranking_table", "peer_relative", "metrics",
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


class TestZeroPrimaryKeyDoesNotFallThrough:
    """`_safe(a or b)` used to read the alias key whenever the primary key held a
    real 0.0 (0% ROE, 0% growth). rank_against_peers now uses _pick: only an
    absent / None / NaN / non-numeric primary falls through. P/E 0 is the one
    exception - it still means "not reported" (see test_zero_primary_falls_through_to_alias).
    """

    def test_zero_roe_primary_is_kept_not_replaced_by_alias(self, env):
        data = {"pe_ratio": 20, "roe": 0, "returnOnEquity": 15,
                "revenue_growth_yoy": 10, "profit_growth_yoy": 10}
        out = pr.rank_against_peers("A", data, URL, peers=["B"])
        r = row(out, "A.NS")
        assert r["roe"] == 0.0                       # was 15.0 via the alias
        assert r["combined"] == 46.0                 # .35*50 + .35*30 (roe 0 -> neutral) + .3*60

    def test_zero_revenue_growth_primary_is_kept(self, env):
        data = {"pe_ratio": 20, "roe": 15, "revenue_growth_yoy": 0, "revenueGrowth": 99,
                "profit_growth_yoy": 10}
        r = row(pr.rank_against_peers("A", data, URL, peers=["B"]), "A.NS")
        assert r["rev_g"] == 0.0                     # was 99.0 via the alias
        assert r["combined"] == 49.75                # growth = 50 + (0+10)/2 = 55 -> 17.5 + 15.75 + 16.5

    def test_zero_profit_growth_primary_is_kept(self, env):
        data = {"pe_ratio": 20, "roe": 15, "revenue_growth_yoy": 10,
                "profit_growth_yoy": 0, "earningsGrowth": 99}
        r = row(pr.rank_against_peers("A", data, URL, peers=["B"]), "A.NS")
        assert r["profit_g"] == 0.0
        assert r["combined"] == 49.75

    def test_zero_primary_is_kept_for_peers_too(self, env):
        env["fetched"] = {"B.NS": {"pe_ratio": 20, "roe": 15, "revenue_growth_yoy": 0,
                                   "revenueGrowth": 99, "profit_growth_yoy": 0,
                                   "earningsGrowth": 99}}
        out = pr.rank_against_peers("A", fund(pe=20, roe=15, rg=10, pg=10), URL, peers=["B"])
        b = row(out, "B.NS")
        assert (b["rev_g"], b["profit_g"]) == (0.0, 0.0)
        assert b["combined"] == 48.25                # growth = 50 -> 17.5 + 15.75 + 15
        assert out["rank"] == 1                      # A (51.25) still ahead of B

    def test_nan_and_non_numeric_primary_still_fall_through_to_alias(self, env):
        data = {"pe_ratio": 20, "roe": float("nan"), "returnOnEquity": 15,
                "revenue_growth_yoy": "N/A", "revenueGrowth": 10,
                "profit_growth_yoy": None, "earningsGrowth": 10}
        r = row(pr.rank_against_peers("A", data, URL, peers=["B"]), "A.NS")
        assert r["combined"] == 51.25                # same as the all-alias payload

    def test_zero_pe_primary_still_falls_through_but_zero_alone_is_neutral(self, env):
        r = row(pr.rank_against_peers("A", fund(pe=0, roe=15, rg=10, pg=10), URL, peers=["B"]), "A.NS")
        assert r["pe"] == 0.0
        assert r["combined"] == 51.25               # P/E 0 -> neutral 50, same as P/E 20


class TestSamePeerSetAsPeerRelative:
    """rank_against_peers() and compute_peer_relative() must compare the same peers."""

    def test_max_peers_is_forwarded_to_compute_peer_relative(self, env):
        pr.rank_against_peers("A", {}, URL, peers=["B", "C", "D"], max_peers=2)
        assert env["rel_max_peers"] == [2]

    def test_zero_max_peers_is_forwarded_not_replaced_by_default(self, env):
        pr.rank_against_peers("A", {}, URL, peers=["B", "C"], max_peers=0)
        assert env["rel_max_peers"] == [0]

    def test_real_compute_peer_relative_fetches_exactly_the_ranking_peers(self, monkeypatch):
        """No fake compute_peer_relative: run both for real, record every symbol fetched."""
        seen = []

        def fake_batch(url, syms, timeout=15.0):
            seen.append(list(syms))
            return {}

        monkeypatch.setattr(pmq, "fetch_fundamentals_batch", fake_batch)
        monkeypatch.setattr(pr, "fetch_fundamentals_batch", fake_batch)
        many = ["s1", "S2", "s3.ns", "S4", "S5", "S6", "S7", "S2"]
        pr.rank_against_peers("A", {}, URL, peers=many)
        assert len(seen) == 2 and seen[0] == seen[1]
        assert seen[0] == ["S1.NS", "S2.NS", "S3.NS", "S4.NS", "S5.NS"]

    def test_real_compute_peer_relative_agrees_for_explicit_max_peers(self, monkeypatch):
        seen = []

        def fake_batch(url, syms, timeout=15.0):
            seen.append(list(syms))
            return {}

        monkeypatch.setattr(pmq, "fetch_fundamentals_batch", fake_batch)
        monkeypatch.setattr(pr, "fetch_fundamentals_batch", fake_batch)
        pr.rank_against_peers("Z", {}, URL, peers=["A", "B", "Z", "C"], max_peers=2)
        assert seen[0] == seen[1] == ["A.NS", "B.NS"]
