"""E191: two defects in the gateway's single-stock decision path (`api-gateway/main.py`, "Decide-level cache").

1. E190 made the decide cache skip a `partial_inputs` (skip_http) entry. This path sends a skip_http request itself whenever the feed row has
   data, and it is also the path that writes those entries, so skipping them made the cache useless for every feed-based call. A partial entry now
   answers a call that is itself skip_http (feed row has data) and is still skipped by a call that would fetch everything live.
2. `feed_row.get("sentiment_score") or feed_row.get("market_score")` read a real sentiment score of 0 as missing. First present value now wins.

Source-level guards (main.py needs the whole gateway to import), plus the sentiment expression run on its own.
"""
import re  # noqa: F401
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")


def _block():
    i = SRC.index("cached_decide = _redis_get(cache_key)")
    return SRC[i:i + 2600]


def test_feed_has_data_is_computed_before_the_cache_read():
    b = _block()
    assert b.index("_feed_has_data = isinstance(feed_row, dict)") < b.index('cached_decide.get("decision")')
    assert "feed_has_data = _feed_has_data" in b          # the later skip_http branch reuses it, no second definition
    import re
    assert re.search(r"(?<!_)feed_has_data = isinstance\(feed_row, dict\)", b) is None


def test_partial_entry_answers_only_a_skip_http_call():
    b = _block()
    assert '(not cached_decide.get("partial_inputs") or _feed_has_data)' in b
    assert 'not cached_decide.get("already_owned")' in b   # E189 still holds


def test_sentiment_score_uses_the_first_present_value():
    assert 'feed_row.get("sentiment_score") or feed_row.get("market_score")' not in SRC
    assert ('feed_row.get("sentiment_score") if feed_row.get("sentiment_score") is not None else feed_row.get("market_score")') in SRC


def test_sentiment_expression_behaviour():
    def pick(row):
        return row.get("sentiment_score") if row.get("sentiment_score") is not None else row.get("market_score")
    assert pick({"sentiment_score": 0, "market_score": 80}) == 0
    assert pick({"market_score": 0}) == 0
    assert pick({"sentiment_score": 64, "market_score": 10}) == 64
    assert pick({}) is None
