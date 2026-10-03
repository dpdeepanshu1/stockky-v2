"""
tests/test_wire_peer_multi_quarter.py — coverage for fundamental/wire_peer_multi_quarter.py

No network. Every test loads a FRESH copy of the module (so the import-time fallbacks and
the MARKET_DATA_URL constant never leak between tests). The two collaborators the wiring
calls — rank_against_peers() and enrich_fundamentals_with_peer_and_consistency() — are
replaced with recording stubs; a few integration tests instead keep the REAL enrich
function and stub only the network-backed helpers underneath it.

Run from services/analysis-intelligence-service:
    python3 -m pytest tests/test_wire_peer_multi_quarter.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import logging
import os
import sys

_SVC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_SVC, "fundamental"))

import pytest

import peer_multi_quarter as pmq
import peer_ranking  # noqa: F401  (must be cached in sys.modules for the fallback tests)

_PATH = os.path.join(_SVC, "fundamental", "wire_peer_multi_quarter.py")
_counter = itertools.count()
_loaded = []
URL = "http://market-data.test"


def _load():
    name = f"_wire_under_test_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, _PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    _loaded.append(name)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("MARKET_DATA_URL", raising=False)
    yield
    while _loaded:
        sys.modules.pop(_loaded.pop(), None)


@pytest.fixture
def w(monkeypatch):
    """Fresh module with recording stubs for the two collaborators."""
    mod = _load()
    rec = {"rank": [], "enrich": [], "rank_result": None, "enrich_result": {},
           "rank_raises": None, "enrich_raises": None}

    def fake_rank(**kw):
        rec["rank"].append(kw)
        if rec["rank_raises"] is not None:
            raise rec["rank_raises"]
        return rec["rank_result"]

    def fake_enrich(**kw):
        rec["enrich"].append(kw)
        if rec["enrich_raises"] is not None:
            raise rec["enrich_raises"]
        return rec["enrich_result"]

    monkeypatch.setattr(mod, "rank_against_peers", fake_rank)
    monkeypatch.setattr(mod, "enrich_fundamentals_with_peer_and_consistency", fake_enrich)
    mod.rec = rec
    return mod


def run(w, payload, symbol="TCS", url=URL):
    return w.apply_to_analyze_response(symbol=symbol, analyze_payload=payload, market_data_url=url)


# ── import-time behaviour ─────────────────────────────────────────────────────

class TestModuleImport:
    def test_real_collaborators_are_imported(self):
        mod = _load()
        assert mod.rank_against_peers is peer_ranking.rank_against_peers
        assert mod.enrich_fundamentals_with_peer_and_consistency is pmq.enrich_fundamentals_with_peer_and_consistency
        assert callable(mod.peers_for) and callable(mod.peer_relative_score)
        assert callable(mod.normalize_sector) and callable(mod.average_metrics)

    def test_missing_peer_multi_quarter_degrades_to_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "peer_multi_quarter", None)
        mod = _load()
        assert mod.enrich_fundamentals_with_peer_and_consistency is None
        assert mod.rank_against_peers is peer_ranking.rank_against_peers

    def test_missing_peer_ranking_degrades_to_none(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "peer_ranking", None)
        mod = _load()
        assert mod.rank_against_peers is None
        assert mod.enrich_fundamentals_with_peer_and_consistency is pmq.enrich_fundamentals_with_peer_and_consistency

    def test_missing_peers_module_nones_all_four_helpers(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "peers", None)
        mod = _load()
        assert mod.peers_for is None
        assert mod.normalize_sector is None
        assert mod.peer_relative_score is None
        assert mod.average_metrics is None

    def test_market_data_url_default_is_empty(self):
        assert _load().MARKET_DATA_URL == ""

    def test_market_data_url_is_read_from_env_and_stripped(self, monkeypatch):
        monkeypatch.setenv("MARKET_DATA_URL", "http://md.test//")
        assert _load().MARKET_DATA_URL == "http://md.test"

    def test_logger_name(self):
        assert _load().logger.name == "wire_peer_mq"


# ── defaults, url resolution, gating ──────────────────────────────────────────

class TestGating:
    def test_none_payload_returns_neutral_defaults(self, w):
        out = run(w, None, url="")
        assert out == {"consistency_score": 50.0, "consistent_growth": False, "peer_score": 50.0}

    def test_empty_payload_same_as_none(self, w):
        assert run(w, {}, url="") == run(w, None, url="")

    def test_no_url_means_neither_collaborator_is_called(self, w):
        run(w, {}, url="")
        run(w, {}, url=None)
        assert w.rec["rank"] == [] and w.rec["enrich"] == []

    def test_module_level_url_is_used_when_argument_missing(self, w, monkeypatch):
        monkeypatch.setattr(w, "MARKET_DATA_URL", "http://module.test")
        run(w, {}, url=None)
        assert w.rec["rank"][0]["market_data_url"] == "http://module.test"
        assert w.rec["enrich"][0]["market_data_url"] == "http://module.test"

    def test_argument_wins_over_module_url_and_trailing_slash_is_stripped(self, w, monkeypatch):
        monkeypatch.setattr(w, "MARKET_DATA_URL", "http://module.test")
        run(w, {}, url="http://arg.test///")
        assert w.rec["rank"][0]["market_data_url"] == "http://arg.test"
        assert w.rec["enrich"][0]["market_data_url"] == "http://arg.test"

    def test_rank_skipped_when_unavailable_enrich_still_runs(self, w, monkeypatch):
        monkeypatch.setattr(w, "rank_against_peers", None)
        run(w, {})
        assert len(w.rec["enrich"]) == 1

    def test_enrich_skipped_when_unavailable_rank_still_runs(self, w, monkeypatch):
        monkeypatch.setattr(w, "enrich_fundamentals_with_peer_and_consistency", None)
        run(w, {})
        assert len(w.rec["rank"]) == 1

    def test_input_payload_is_not_mutated(self, w):
        payload = {"fundamental_score": 80, "metrics": {"pe": 10}}
        snapshot = {"fundamental_score": 80, "metrics": {"pe": 10}}
        out = run(w, payload)
        assert payload == snapshot
        assert out is not payload


# ── how collaborators are called ──────────────────────────────────────────────

class TestCollaboratorArguments:
    def test_rank_arguments(self, w):
        run(w, {"raw": {"a": 1}, "metrics": {"b": 2}, "peer_list": ["INFY.NS"]}, symbol="tcs")
        assert w.rec["rank"] == [{
            "symbol": "tcs",                         # passed through un-normalised
            "stock_fund": {"a": 1, "b": 2},
            "market_data_url": URL,
            "peers": ["INFY.NS"],
        }]

    def test_enrich_arguments(self, w):
        run(w, {"raw": {"a": 1}, "metrics": {"b": 2}, "peer_list": ["INFY.NS"]})
        assert w.rec["enrich"] == [{
            "symbol": "TCS",
            "fundamentals": {"a": 1, "b": 2},
            "market_data_url": URL,
            "quarterly": None,
            "peers": ["INFY.NS"],
        }]

    def test_metrics_override_raw_in_merged_fundamentals(self, w):
        run(w, {"raw": {"pe": 30, "only_raw": 1}, "metrics": {"pe": 12}})
        assert w.rec["rank"][0]["stock_fund"] == {"pe": 12, "only_raw": 1}

    def test_missing_or_null_raw_and_metrics_give_empty_fundamentals(self, w):
        run(w, {"raw": None, "metrics": None})
        run(w, {})
        assert w.rec["rank"][0]["stock_fund"] == {}
        assert w.rec["rank"][1]["stock_fund"] == {}

    def test_peers_is_none_without_a_peer_list(self, w):
        run(w, {})
        assert w.rec["rank"][0]["peers"] is None
        assert w.rec["enrich"][0]["peers"] is None


# ── consistency defaults taken from the incoming payload ──────────────────────

class TestConsistencyFromPayload:
    def test_top_level_score_preferred(self, w):
        out = run(w, {"multi_quarter_score": 72, "multi_quarter_detail": {"score": 10}}, url="")
        assert out["consistency_score"] == 72.0

    def test_falls_back_to_detail_score(self, w):
        assert run(w, {"multi_quarter_detail": {"score": 64}}, url="")["consistency_score"] == 64.0

    def test_neutral_fifty_when_absent(self, w):
        assert run(w, {"multi_quarter_detail": {}}, url="")["consistency_score"] == 50.0

    def test_numeric_string_is_accepted(self, w):
        assert run(w, {"multi_quarter_score": "72.5"}, url="")["consistency_score"] == 72.5

    def test_a_real_zero_score_is_kept(self, w):
        """Fixed: `x or y or 50.0` used to treat a genuine 0 (the worst score) as
        missing and replace it with the neutral 50."""
        assert run(w, {"multi_quarter_score": 0}, url="")["consistency_score"] == 0.0

    def test_a_real_zero_beats_a_nonzero_detail_score(self, w):
        out = run(w, {"multi_quarter_score": 0, "multi_quarter_detail": {"score": 64}}, url="")
        assert out["consistency_score"] == 0.0

    def test_a_real_zero_detail_score_is_kept(self, w):
        assert run(w, {"multi_quarter_detail": {"score": 0}}, url="")["consistency_score"] == 0.0

    @pytest.mark.parametrize("missing", [None, "", float("nan")])
    def test_missing_top_level_values_fall_through_to_the_detail_score(self, w, missing):
        out = run(w, {"multi_quarter_score": missing, "multi_quarter_detail": {"score": 64}}, url="")
        assert out["consistency_score"] == 64.0

    @pytest.mark.parametrize("missing", [None, "", float("nan")])
    def test_missing_everywhere_is_still_neutral_fifty(self, w, missing):
        out = run(w, {"multi_quarter_score": missing, "multi_quarter_detail": {"score": missing}}, url="")
        assert out["consistency_score"] == 50.0

    @pytest.mark.parametrize("payload,expected", [
        ({"multi_quarter_ok": True}, True),
        ({"multi_quarter_detail": {"ok": True}}, True),
        ({"multi_quarter_ok": False, "multi_quarter_detail": {"ok": True}}, True),
        ({"multi_quarter_ok": False, "multi_quarter_detail": {"ok": False}}, False),
        ({}, False),
    ])
    def test_consistent_growth(self, w, payload, expected):
        assert run(w, payload, url="")["consistent_growth"] is expected


# ── peer score seeding ────────────────────────────────────────────────────────

class TestPeerScoreSeed:
    def test_peer_relative_score_used_and_rounded(self, w):
        assert run(w, {"peer_relative_score": 61.2345}, url="")["peer_score"] == 61.23

    def test_zero_is_kept_not_replaced(self, w):
        assert run(w, {"peer_relative_score": 0}, url="")["peer_score"] == 0.0

    def test_zero_top_level_score_does_not_fall_through_to_the_dict(self, w):
        out = run(w, {"peer_relative_score": 0, "peer_relative": {"score": 58.0}}, url="")
        assert out["peer_score"] == 0.0

    def test_falls_back_to_peer_relative_dict_score(self, w):
        assert run(w, {"peer_relative": {"score": 58.0}}, url="")["peer_score"] == 58.0

    def test_top_level_score_wins_over_dict(self, w):
        out = run(w, {"peer_relative_score": 70, "peer_relative": {"score": 58.0}}, url="")
        assert out["peer_score"] == 70.0

    def test_non_dict_peer_relative_is_ignored(self, w):
        assert run(w, {"peer_relative": "n/a"}, url="")["peer_score"] == 50.0

    def test_dict_without_score_gives_neutral(self, w):
        assert run(w, {"peer_relative": {"peer_score": 90}}, url="")["peer_score"] == 50.0


# ── rank_against_peers integration ────────────────────────────────────────────

class TestRankingBlock:
    RANKING = {"peer_score": 74.567, "rank": 2, "rank_label": "#2 of 6 in IT", "total_compared": 6}

    def test_ranking_fields_are_attached(self, w):
        w.rec["rank_result"] = dict(self.RANKING)
        out = run(w, {})
        assert out["peer_ranking"] == self.RANKING
        assert out["peer_rank"] == 2
        assert out["peer_rank_label"] == "#2 of 6 in IT"
        assert out["peer_count"] == 6

    def test_ranking_peer_score_overrides_seed(self, w):
        w.rec["rank_result"] = dict(self.RANKING)
        assert run(w, {"peer_relative_score": 40})["peer_score"] == 74.57

    def test_ranking_without_peer_score_keeps_seed(self, w):
        w.rec["rank_result"] = {"rank": 1, "rank_label": "#1 of 3 in IT", "total_compared": 3}
        out = run(w, {"peer_relative_score": 40})
        assert out["peer_score"] == 40.0
        assert out["peer_rank"] == 1

    def test_ranking_with_null_peer_score_keeps_seed(self, w):
        w.rec["rank_result"] = {"peer_score": None, "rank": 1}
        assert run(w, {"peer_relative_score": 40})["peer_score"] == 40.0

    def test_empty_ranking_attaches_nothing(self, w):
        w.rec["rank_result"] = {}
        out = run(w, {})
        for k in ("peer_ranking", "peer_rank", "peer_rank_label", "peer_count"):
            assert k not in out

    def test_none_ranking_attaches_nothing(self, w):
        w.rec["rank_result"] = None
        # a None return makes `ranking.get` raise inside the inner try -> logged, skipped
        out = run(w, {})
        assert "peer_ranking" not in out
        assert out["peer_score"] == 50.0

    def test_ranking_failure_is_logged_and_enrich_still_runs(self, w, caplog):
        w.rec["rank_raises"] = RuntimeError("md down")
        w.rec["enrich_result"] = {"consistency_score": 66}
        with caplog.at_level(logging.WARNING, logger="wire_peer_mq"):
            out = run(w, {})
        assert any("rank_against_peers failed" in r.getMessage() for r in caplog.records)
        assert "peer_ranking" not in out
        assert out["consistency_score"] == 66.0


# ── enrichment integration (stubbed enrich) ───────────────────────────────────

class TestEnrichBlock:
    def test_enriched_scores_override_everything_before(self, w):
        w.rec["rank_result"] = {"peer_score": 70, "rank": 1}
        w.rec["enrich_result"] = {"peer_score": 55.555, "consistency_score": 81}
        out = run(w, {"multi_quarter_score": 20})
        assert out["peer_score"] == 55.55             # enrich beats ranking (float 55.555 rounds down)
        assert out["consistency_score"] == 81.0       # enrich beats payload

    def test_consistent_growth_key_present_overrides_even_when_false(self, w):
        w.rec["enrich_result"] = {"consistent_growth": False}
        assert run(w, {"multi_quarter_ok": True})["consistent_growth"] is False

    def test_consistent_growth_truthy_is_coerced_to_bool(self, w):
        w.rec["enrich_result"] = {"consistent_growth": 1}
        assert run(w, {})["consistent_growth"] is True

    def test_absent_keys_leave_payload_values_alone(self, w):
        w.rec["enrich_result"] = {}
        out = run(w, {"multi_quarter_score": 33, "multi_quarter_ok": True, "peer_relative_score": 44})
        assert out["consistency_score"] == 33.0
        assert out["consistent_growth"] is True
        assert out["peer_score"] == 44.0

    def test_null_scores_are_ignored(self, w):
        w.rec["enrich_result"] = {"peer_score": None, "consistency_score": None}
        out = run(w, {"multi_quarter_score": 33, "peer_relative_score": 44})
        assert out["consistency_score"] == 33.0
        assert out["peer_score"] == 44.0

    def test_peer_relative_and_multi_quarter_replace_when_truthy(self, w):
        w.rec["enrich_result"] = {"peer_relative": {"peer_score": 66}, "multi_quarter": {"consistency_score": 80}}
        out = run(w, {"peer_relative": {"score": 50, "note": "old"}})
        assert out["peer_relative"] == {"peer_score": 66}
        assert out["multi_quarter"] == {"consistency_score": 80}

    def test_empty_peer_relative_and_multi_quarter_are_not_copied(self, w):
        w.rec["enrich_result"] = {"peer_relative": {}, "multi_quarter": None}
        out = run(w, {"peer_relative": {"score": 50}})
        assert out["peer_relative"] == {"score": 50}
        assert "multi_quarter" not in out

    def test_enrich_failure_is_logged_and_earlier_results_survive(self, w, caplog):
        w.rec["rank_result"] = {"peer_score": 70, "rank": 1, "rank_label": "#1 of 2 in IT", "total_compared": 2}
        w.rec["enrich_raises"] = RuntimeError("boom")
        with caplog.at_level(logging.WARNING, logger="wire_peer_mq"):
            out = run(w, {})
        assert any("enrich_fundamentals failed" in r.getMessage() for r in caplog.records)
        assert out["peer_score"] == 70.0
        assert out["peer_rank"] == 1


class TestWithRealEnrich:
    """Keep the real enrich function; stub only what it calls over the network."""

    @pytest.fixture
    def real(self, monkeypatch):
        mod = _load()
        monkeypatch.setattr(mod, "rank_against_peers", None)
        state = {"rel": {"peer_score": 66.0}, "mq": {"consistency_score": 80.0, "consistent_both": True},
                 "rel_raises": None}

        def fake_rel(symbol, fundamentals, url, peers=None):
            if state["rel_raises"]:
                raise state["rel_raises"]
            return state["rel"]

        monkeypatch.setattr(pmq, "compute_peer_relative", fake_rel)
        monkeypatch.setattr(pmq, "compute_multi_quarter_consistency", lambda **kw: state["mq"])
        mod.state = state
        return mod

    def test_enrich_output_replaces_the_analyze_payload_values(self, real):
        out = run(real, {"multi_quarter_score": 90, "multi_quarter_ok": True, "peer_relative_score": 30})
        assert out["peer_score"] == 66.0
        assert out["consistency_score"] == 80.0
        assert out["consistent_growth"] is True
        assert out["peer_relative"] == {"peer_score": 66.0}
        assert out["multi_quarter"] == {"consistency_score": 80.0, "consistent_both": True}

    def test_payload_multi_quarter_ok_is_overridden_by_enrich(self, real):
        # Pinned: enrich always returns the flat convenience fields, so with a market-data
        # URL configured the analyze() values are always replaced by the enrich ones.
        real.state["mq"] = {"consistency_score": 40.0, "consistent_both": False}
        out = run(real, {"multi_quarter_score": 90, "multi_quarter_ok": True})
        assert out["consistent_growth"] is False
        assert out["consistency_score"] == 40.0

    def test_failed_peer_relative_forces_neutral_fifty_over_a_real_score(self, real):
        # enrich swallows the failure and reports peer_score=50.0 (never None), which then
        # overwrites the score already seeded from the payload.
        real.state["rel_raises"] = RuntimeError("md down")
        out = run(real, {"peer_relative_score": 77})
        assert out["peer_score"] == 50.0
        assert out["peer_relative"]["error"] == "md down"


# ── fundamental_score blend ───────────────────────────────────────────────────

class TestScoreBlend:
    def test_blend_formula_and_flags(self, w):
        out = run(w, {"fundamental_score": 80, "peer_relative_score": 60, "multi_quarter_score": 40}, url="")
        assert out["fundamental_score_raw"] == 80.0
        assert out["fundamental_score"] == 72.0          # .7*80 + .2*60 + .1*40
        assert out["fundamental_score_adjusted"] is True

    def test_uses_enriched_scores_in_the_blend(self, w):
        w.rec["enrich_result"] = {"peer_score": 100, "consistency_score": 100}
        out = run(w, {"fundamental_score": 50})
        assert out["fundamental_score"] == 65.0          # .7*50 + .2*100 + .1*100

    def test_result_is_rounded_to_two_decimals(self, w):
        out = run(w, {"fundamental_score": 33.333, "peer_relative_score": 44.444, "multi_quarter_score": 55.555}, url="")
        assert out["fundamental_score"] == 37.78
        assert out["fundamental_score_raw"] == 33.333

    def test_clamped_to_zero_and_hundred(self, w):
        hi = run(w, {"fundamental_score": 500, "peer_relative_score": 100, "multi_quarter_score": 100}, url="")
        lo = run(w, {"fundamental_score": -100, "peer_relative_score": 0, "multi_quarter_score": 10}, url="")
        assert hi["fundamental_score"] == 100.0
        assert lo["fundamental_score"] == 0.0
        assert hi["fundamental_score_raw"] == 500.0

    def test_int_score_is_stored_raw_as_float(self, w):
        out = run(w, {"fundamental_score": 80}, url="")
        assert isinstance(out["fundamental_score_raw"], float)

    @pytest.mark.parametrize("base", [None, "80", [80]])
    def test_non_numeric_base_is_left_untouched(self, w, base):
        out = run(w, {"fundamental_score": base}, url="")
        assert out["fundamental_score"] == base
        assert "fundamental_score_raw" not in out
        assert "fundamental_score_adjusted" not in out

    def test_absent_base_adds_no_score_fields(self, w):
        out = run(w, {}, url="")
        assert "fundamental_score" not in out
        assert "fundamental_score_adjusted" not in out

    def test_zero_consistency_counts_as_zero_in_the_blend(self, w):
        """Fixed: the blend used `float(x or 50.0)`, so the payload kept
        consistency_score == 0.0 while the blend quietly used 50."""
        w.rec["enrich_result"] = {"consistency_score": 0.0}
        out = run(w, {"fundamental_score": 100})
        assert out["consistency_score"] == 0.0
        assert out["fundamental_score"] == 80.0          # .7*100 + .2*50 + .1*0

    def test_missing_consistency_in_the_blend_is_still_neutral(self, w):
        out = run(w, {"fundamental_score": 100}, url="")
        assert out["consistency_score"] == 50.0
        assert out["fundamental_score"] == 85.0          # .7*100 + .2*50 + .1*50

    def test_applying_twice_is_idempotent(self, w):
        # Fixed: a second pass used to blend the already-adjusted score again (80 -> 71 -> 64.7) and
        # overwrite fundamental_score_raw with the adjusted value. It now blends from the stored raw score.
        first = run(w, {"fundamental_score": 80}, url="")
        assert first["fundamental_score"] == 71.0
        second = run(w, first, url="")
        assert second["fundamental_score_raw"] == 80.0
        assert second["fundamental_score"] == 71.0
        assert second == first

    def test_applying_three_times_is_still_stable(self, w):
        out = run(w, {"fundamental_score": 80, "peer_relative_score": 60, "multi_quarter_score": 40}, url="")
        for _ in range(3):
            out = run(w, out, url="")
        assert out["fundamental_score_raw"] == 80.0 and out["fundamental_score"] == 72.0

    def test_second_pass_with_enrichment_blends_from_the_raw_score(self, w):
        w.rec["enrich_result"] = {"peer_score": 100, "consistency_score": 100}
        first = run(w, {"fundamental_score": 50})
        second = run(w, first)
        assert first["fundamental_score"] == second["fundamental_score"] == 65.0
        assert second["fundamental_score_raw"] == 50.0

    def test_adjusted_flag_without_a_numeric_raw_blends_the_current_score(self, w):
        out = run(w, {"fundamental_score": 80, "fundamental_score_adjusted": True}, url="")
        assert out["fundamental_score_raw"] == 80.0 and out["fundamental_score"] == 71.0

    @pytest.mark.parametrize("raw", ["80", None, True, float("nan")])
    def test_unusable_stored_raw_is_ignored(self, w, raw):
        out = run(w, {"fundamental_score": 80, "fundamental_score_adjusted": True,
                      "fundamental_score_raw": raw}, url="")
        assert out["fundamental_score_raw"] == 80.0 and out["fundamental_score"] == 71.0

    def test_raw_without_the_adjusted_flag_is_overwritten(self, w):
        # A stray raw field alone does not mean the payload was blended; only the flag does.
        out = run(w, {"fundamental_score": 80, "fundamental_score_raw": 10}, url="")
        assert out["fundamental_score_raw"] == 80.0 and out["fundamental_score"] == 71.0


# ── never raises ──────────────────────────────────────────────────────────────

class TestNeverRaises:
    @pytest.mark.parametrize("payload", [
        {"multi_quarter_score": "not-a-number"},
        {"multi_quarter_detail": ["not", "a", "dict"]},
        {"peer_relative_score": "abc"},
        {"raw": ["not", "a", "dict"]},
    ])
    def test_bad_payload_returns_the_original_object_unchanged(self, w, payload, caplog):
        before = dict(payload)
        with caplog.at_level(logging.ERROR, logger="wire_peer_mq"):
            out = run(w, payload)
        assert out is payload
        assert payload == before                          # the working copy took the writes
        assert any("apply_to_analyze_response failed" in r.getMessage() for r in caplog.records)
        assert w.rec["rank"] == [] and w.rec["enrich"] == []

    def test_non_dict_payload_is_returned_as_is(self, w):
        payload = [1, 2, 3]
        assert run(w, payload) is payload
