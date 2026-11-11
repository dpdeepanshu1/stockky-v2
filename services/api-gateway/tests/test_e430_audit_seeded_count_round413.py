"""Round 413: E430 - the Health Audit reports how many rows rest on a baseline seed (pe_ratio / roce / sentiment_score / rsi), next to the
health score. A seed still counts as populated (that is what data_feed seeds are for), so health_score, fully_populated and incomplete_count
are unchanged; seeded_count, seeded_by_field, real_data_count and real_data_score are new."""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.dirname(HERE)
sys.path.insert(0, GW)

_IMPORT_TIME_ENV = ("USE_REDIS", "DISABLE_REDIS", "DISABLE_UPSTASH",
                    "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN")
_saved_env = {k: os.environ.pop(k) for k in _IMPORT_TIME_ENV if k in os.environ}
try:
    import main as gw
finally:
    os.environ.update(_saved_env)


def _real():
    return {"price": 100, "rsi": 41, "pe_ratio": 20, "roce": 15, "sentiment_score": 0.3}


class _Store:
    def __init__(self, rows):
        self.rows = rows

    def list_symbols(self):
        return list(self.rows)

    def get_symbol(self, sym):
        return self.rows.get(sym)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(gw, "_AUDIT_MEMO", {})
    monkeypatch.setattr(gw, "AUDIT_TTL_SEC", 20.0)
    monkeypatch.setattr(gw, "MAX_UNIVERSE_PRICE", 0.0)


def test_seeded_fields_lists_only_flagged_present_fields():
    row = dict(_real(), pe_ratio_seed=True, sentiment_seed=True, roce_seed=False)
    assert gw._feed_seeded_fields(row) == ["pe_ratio", "sentiment_score"]
    assert gw._feed_seeded_fields(_real()) == []
    assert gw._feed_seeded_fields(None) == [] and gw._feed_seeded_fields("x") == []


def test_a_flag_on_a_missing_field_is_not_listed_as_seeded():
    row = dict(_real(), roce=0, roce_seed=True)      # zeroed roce is "missing" already
    assert gw._feed_seeded_fields(row) == []


def test_audit_reports_seeds_without_changing_the_health_score(monkeypatch):
    rows = {
        "AAA": _real(),
        "BBB": dict(_real(), pe_ratio_seed=True, roce_seed=True),
        "CCC": dict(_real(), rsi_seed=True),
        "DDD": {"price": 10},                         # incomplete
    }
    monkeypatch.setattr(gw, "_feed_store", lambda: _Store(rows))
    out = gw._compute_feed_audit(500)
    assert out["total_universe"] == 4 and out["fully_populated"] == 3 and out["incomplete_count"] == 1
    assert out["health_score"] == 75.0               # seeds still count as populated
    assert out["seeded_count"] == 2
    assert out["seeded_by_field"] == {"pe_ratio": 1, "roce": 1, "rsi": 1}
    assert out["real_data_count"] == 1 and out["real_data_score"] == 25.0


def test_audit_with_no_seeds_reports_zero(monkeypatch):
    monkeypatch.setattr(gw, "_feed_store", lambda: _Store({"AAA": _real()}))
    out = gw._compute_feed_audit(500)
    assert out["seeded_count"] == 0 and out["seeded_by_field"] == {} and out["real_data_score"] == 100.0
