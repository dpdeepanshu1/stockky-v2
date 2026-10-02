"""
tests/test_peers.py — fundamental/peers.py
Pure stdlib, no network.
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fundamental"))

import pytest
import peers as p


class TestSafeFloat:
    """_f(): tolerant float coercion used when averaging peer metrics."""

    @pytest.mark.parametrize("raw,expected", [(None, None), ("12.5", 12.5), (3, 3.0), (0, 0.0)])
    def test_valid_and_none(self, raw, expected):
        assert p._f(raw) == expected

    @pytest.mark.parametrize("bad", ["abc", "", object(), [], {}])
    def test_unconvertible_returns_none(self, bad):
        # ValueError ("abc", "") and TypeError (object/list/dict) -> None (lines 74-75)
        assert p._f(bad) is None


class TestNormalizeSector:
    def test_symbol_lookup_wins(self):
        assert p.normalize_sector(None, "TCS") == "IT"

    def test_raw_software_maps_to_it(self):
        assert p.normalize_sector("software services") == "IT"

    def test_raw_bank_maps_to_banks(self):
        assert p.normalize_sector("banking") == "Banks"

    def test_raw_pharma_maps_to_pharma(self):
        assert p.normalize_sector("drug manufacturers") == "Pharma"

    def test_raw_auto_maps_to_auto(self):
        assert p.normalize_sector("automobile components") == "Auto"

    def test_raw_cement_maps_to_infra(self):
        assert p.normalize_sector("cement and construction") == "Infra"

    def test_raw_telecom_maps_to_telecom(self):
        assert p.normalize_sector("telecom services") == "Telecom"

    def test_unknown_returns_none(self):
        assert p.normalize_sector("completely unknown industry") is None

    def test_normalize_sector_is_idempotent_for_every_canonical_name(self):
        for canonical in p.SECTOR_PEERS:
            assert p.normalize_sector(canonical) == canonical
            assert p.normalize_sector(canonical.upper()) == canonical
            assert p.normalize_sector(f"  {canonical.lower()} ") == canonical

    def test_none_raw_and_no_symbol(self):
        assert p.normalize_sector(None) is None

    def test_energy_mapped(self):
        assert p.normalize_sector("oil and gas refining") == "Energy"

    def test_capital_goods_mapped(self):
        assert p.normalize_sector("electrical equipment") == "Capital Goods"

    def test_consumer_durables_mapped(self):
        assert p.normalize_sector("consumer durables electronics") == "Consumer Durables"

    def test_fmcg_mapped(self):
        assert p.normalize_sector("food products fmcg") == "FMCG"

    def test_metals_mapped(self):
        assert p.normalize_sector("steel and metal mining") == "Metals"


class TestPeersFor:
    def test_known_it_symbol(self):
        peers = p.peers_for("TCS")
        assert "INFY" in peers
        assert "TCS" not in peers   # self excluded

    def test_known_bank_symbol(self):
        peers = p.peers_for("HDFCBANK")
        assert "ICICIBANK" in peers

    def test_with_sector_override(self):
        # pass a raw string that matches "software" mapping → "IT"
        peers = p.peers_for("UNKNOWN", sector="software services")
        assert "TCS" in peers

    def test_unknown_symbol_no_sector(self):
        result = p.peers_for("UNKNOWNSYMBOL")
        assert result == []

    def test_ns_suffix_stripped(self):
        peers = p.peers_for("TCS.NS")
        assert "INFY" in peers


class TestAverageMetrics:
    def test_averages_pe(self):
        rows = [{"pe_ratio": 20.0}, {"pe_ratio": 30.0}]
        result = p.average_metrics(rows)
        assert result["pe_ratio"] == pytest.approx(25.0)

    def test_skips_none_values(self):
        rows = [{"pe_ratio": 20.0}, {"pe_ratio": None}]
        result = p.average_metrics(rows)
        assert result["pe_ratio"] == 20.0

    def test_pe_alias_filled(self):
        rows = [{"pe": 15.0}]
        result = p.average_metrics(rows)
        assert result["pe_ratio"] == 15.0

    def test_empty_rows_returns_nones(self):
        result = p.average_metrics([])
        assert all(v is None for v in result.values())

    def test_non_dict_row_skipped(self):
        result = p.average_metrics(["not_a_dict"])
        assert all(v is None for v in result.values())

    def test_multiple_metrics_averaged(self):
        rows = [
            {"pe_ratio": 10.0, "roe": 20.0},
            {"pe_ratio": 20.0, "roe": 40.0},
        ]
        result = p.average_metrics(rows)
        assert result["pe_ratio"] == pytest.approx(15.0)
        assert result["roe"] == pytest.approx(30.0)


class TestPeerRelativeScore:
    def test_no_peer_avg_returns_50(self):
        result = p.peer_relative_score({}, None)
        assert result["score"] == 50.0
        assert result["note"] == "no_peer_avg"

    def test_empty_peer_avg_returns_50(self):
        result = p.peer_relative_score({}, {})
        assert result["score"] == 50.0

    def test_cheaper_pe_than_peers_raises_score(self):
        stock = {"pe_ratio": 10.0}
        peer_avg = {"pe_ratio": 25.0}
        result = p.peer_relative_score(stock, peer_avg)
        assert result["score"] > 50.0
        assert "pe" in result["components"]

    def test_expensive_pe_lowers_score(self):
        stock = {"pe_ratio": 50.0}
        peer_avg = {"pe_ratio": 20.0}
        result = p.peer_relative_score(stock, peer_avg)
        assert result["score"] < 50.0

    def test_higher_roe_raises_score(self):
        stock = {"roe": 30.0}
        peer_avg = {"roe": 15.0}
        result = p.peer_relative_score(stock, peer_avg)
        assert result["score"] > 50.0
        assert "roe" in result["components"]

    def test_higher_growth_raises_score(self):
        stock = {"revenue_growth": 20.0}
        peer_avg = {"revenue_growth": 5.0}
        result = p.peer_relative_score(stock, peer_avg)
        assert result["score"] > 50.0

    def test_lower_debt_raises_score(self):
        stock = {"debt_to_equity": 0.3}
        peer_avg = {"debt_to_equity": 1.5}
        result = p.peer_relative_score(stock, peer_avg)
        assert result["score"] > 50.0

    def test_score_clamped_0_100(self):
        stock = {"pe_ratio": 1.0, "roe": 99.0, "revenue_growth": 99.0, "debt_to_equity": 0.0}
        peer_avg = {"pe_ratio": 100.0, "roe": 1.0, "revenue_growth": 1.0, "debt_to_equity": 5.0}
        result = p.peer_relative_score(stock, peer_avg)
        assert 0.0 <= result["score"] <= 100.0

    def test_pe_zero_in_stock_skipped(self):
        # pe=0 → skip component (division guard)
        stock = {"pe_ratio": 0.0}
        peer_avg = {"pe_ratio": 20.0}
        result = p.peer_relative_score(stock, peer_avg)
        assert "pe" not in result["components"]
