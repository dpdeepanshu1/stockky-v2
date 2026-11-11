"""E459: a seeded sentiment (including the old 0.65 rows already stored) reaches the decision engine as the neutral 50, a real one unchanged."""
import pytest

import main


@pytest.mark.parametrize("row,expected", [
    ({"sentiment_score": 0.65, "sentiment_seed": True, "news_score": 0.65}, (50.0, 50.0)),   # old stored seed, repair copy in news_score
    ({"sentiment_score": 50.0, "sentiment_seed": True}, (50.0, None)),
    ({"sentiment_score": 0.65, "sentiment_seed": True, "news_score": 62}, (50.0, 62)),         # a real news score is kept
    ({"sentiment_score": 0, "news_score": 0}, (0, 0)),                                         # a real 0 stays 0
    ({"sentiment_score": 71.0, "news_score": 66}, (71.0, 66)),
    ({"market_score": 44}, (44, None)),
    ({"market_score": 44, "sentiment_score": 0}, (0, None)),
    ({}, (None, None)),
    (None, (None, None)),
])
def test_feed_scoring_sentiment(row, expected):
    assert main._feed_scoring_sentiment(row) == expected


def test_a_flag_that_is_not_true_is_not_a_seed():
    assert main._feed_scoring_sentiment({"sentiment_score": 0.4, "sentiment_seed": "yes"}) == (0.4, None)
