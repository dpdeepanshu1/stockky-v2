"""E391: neutral 50.0 seeds (bhavcopy feed, repair) never overwrite a real stored RSI / sentiment score."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GW = os.path.dirname(HERE)
sys.path.insert(0, GW)

import data_feed as df  # noqa: E402


def test_e391_bhavcopy_seed_flags_its_defaults():
    s = df.bhavcopy_seed({"symbol": "A", "price": 5.0})
    assert s["rsi"] == 50.0 and s["rsi_seed"] is True
    assert s["sentiment_score"] == 50.0 and s["sentiment_seed"] is True
    assert s["_seed"] is True and s["source"] == "nse_bhavcopy"
    t = df.bhavcopy_seed({"symbol": "A", "rsi": 61.0, "sentiment_score": 0.4, "source": "x"})
    assert t["rsi"] == 61.0 and "rsi_seed" not in t and t["sentiment_score"] == 0.4 and "sentiment_seed" not in t and t["source"] == "x"
    assert df.bhavcopy_seed({"symbol": "A", "source": ""})["source"] == "nse_bhavcopy"


def test_e391_seed_does_not_overwrite_a_real_value():
    real = {"symbol": "TCS", "rsi": 68.4, "sentiment_score": 0.82, "pe_ratio": 31.0}
    out = df.merge_feed_payload(real, df.bhavcopy_seed({"symbol": "TCS", "price": 1010.0}))
    assert out["rsi"] == 68.4 and out["sentiment_score"] == 0.82 and out["pe_ratio"] == 31.0
    assert "rsi_seed" not in out and "sentiment_seed" not in out          # the rejected seed flags are dropped too


def test_e391_seed_fills_an_empty_row_and_refreshes_a_seed():
    out = df.merge_feed_payload({"symbol": "NEW"}, df.bhavcopy_seed({"symbol": "NEW"}))
    assert out["rsi"] == 50.0 and out["rsi_seed"] is True
    again = df.merge_feed_payload(out, df.bhavcopy_seed({"symbol": "NEW"}))
    assert again["rsi"] == 50.0 and again["rsi_seed"] is True


def test_e391_real_rsi_replaces_a_seed_and_clears_its_flag():
    seeded = {"symbol": "A", "rsi": 50.0, "rsi_seed": True}
    out = df.merge_feed_payload(seeded, {"rsi": 44.0})
    assert out["rsi"] == 44.0 and "rsi_seed" not in out
    # and the next bhavcopy run no longer touches it
    out2 = df.merge_feed_payload(out, df.bhavcopy_seed({"symbol": "A"}))
    assert out2["rsi"] == 44.0


def test_e391_a_stored_zero_rsi_is_seeded_over_on_purpose():
    out = df.merge_feed_payload({"symbol": "A", "rsi": 0.0}, {"rsi": 50.0, "rsi_seed": True})
    assert out["rsi"] == 50.0 and out["rsi_seed"] is True


def test_e391_wiring():
    with open(os.path.join(GW, "data_feed.py"), encoding="utf-8") as f:
        feed = f.read()
    assert "seed_batch[base] = bhavcopy_seed(rec)" in feed
    assert 'seed.setdefault("rsi"' not in feed
    with open(os.path.join(GW, "main.py"), encoding="utf-8") as f:
        main = f.read()
    assert main.count('current.pop("rsi_seed", None)') == 2
