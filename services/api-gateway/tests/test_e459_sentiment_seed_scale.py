"""E459: the sentiment / news placeholder is the neutral 50 on the 0-100 scale every reader uses (it was 0.65 in two places, 50.0 in a third)."""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.dirname(HERE)
sys.path.insert(0, GW)

import data_feed as df  # noqa: E402


def test_one_neutral_constant_on_the_0_100_scale():
    assert df.NEUTRAL_SENTIMENT_SEED == 50.0


def test_bhavcopy_seed_uses_the_constant():
    s = df.bhavcopy_seed({"symbol": "A"})
    assert s["sentiment_score"] == df.NEUTRAL_SENTIMENT_SEED and s["sentiment_seed"] is True


def test_a_real_sentiment_in_the_record_is_kept():
    s = df.bhavcopy_seed({"symbol": "A", "sentiment_score": 0})
    assert s["sentiment_score"] == 0 and "sentiment_seed" not in s


def test_seed_never_overwrites_a_real_stored_score():
    out = df.merge_feed_payload({"sentiment_score": 71.0}, df.bhavcopy_seed({"symbol": "A"}))
    assert out["sentiment_score"] == 71.0 and not out.get("sentiment_seed")


def test_a_new_seed_replaces_an_old_0_65_seed():
    out = df.merge_feed_payload({"sentiment_score": 0.65, "sentiment_seed": True}, df.bhavcopy_seed({"symbol": "A"}))
    assert out["sentiment_score"] == 50.0 and out["sentiment_seed"] is True


def test_missing_symbol_placeholder_uses_the_constant(monkeypatch):
    import types
    class R:
        status_code = 200
        text = ""
        def json(self):
            return {"quotes": {}}
    class C:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, *a, **k): return R()
    monkeypatch.setitem(sys.modules, "httpx", types.SimpleNamespace(Client=C, post=lambda *a, **k: R()))
    out = df.bulk_yahoo_download_prices(["ZZZ"])
    assert out["ZZZ"]["sentiment_score"] == 50.0 and out["ZZZ"]["sentiment_seed"] is True
