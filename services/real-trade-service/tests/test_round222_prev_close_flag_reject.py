"""Round 222: a quote whose previous close market-data corrected / withdrew (split / bonus day) is skipped by both candidate tracks."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from candidate_engine import candidates as cd  # noqa: E402


def test_flagged_quote_gives_a_reject():
    for flag in ("corrected", "suspect"):
        r = cd._prev_close_flag_reject({"price": 608.3, "prev_close_flag": flag, "previous_close_raw": 3143.3})
        assert r and r["reject_kind"] == "prev_close_flag" and "3143.3" in r["reject_reason"]


def test_clean_or_missing_quote_is_not_rejected():
    assert cd._prev_close_flag_reject({"price": 100.0, "previous_close": 99.0}) is None
    assert cd._prev_close_flag_reject(None) is None
    assert cd._prev_close_flag_reject("x") is None
    assert cd._prev_close_flag_reject({"prev_close_flag": "weird"}) is None


def test_switch_off(monkeypatch):
    monkeypatch.setattr(cd, "PREV_CLOSE_FLAG_REJECT", False)
    assert cd._prev_close_flag_reject({"prev_close_flag": "suspect"}) is None


def test_volume_shock_track_rejects_before_any_history_call():
    class Boom:
        async def get(self, *a, **k):
            raise AssertionError("no history call expected")
    q = {"price": 608.3, "previous_close": None, "prev_close_flag": "suspect", "previous_close_raw": 3143.3}
    r = asyncio.run(cd._volume_shock_analysis(Boom(), "KMEW", q))
    assert r["reject_kind"] == "prev_close_flag"


def test_standard_track_rejects_flagged_quote(monkeypatch):
    async def _no_hist(*a, **k):
        return None
    monkeypatch.setattr(cd, "_fetch_history", _no_hist)
    q = {"price": 608.3, "prev_close_flag": "corrected", "previous_close_raw": 3143.3}
    r = asyncio.run(cd._multi_tf_analysis(object(), "KMEW", q))
    assert r.get("reject_kind") == "prev_close_flag" and r["bullish_count"] == 0
