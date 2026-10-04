"""group148: "scrip master not loaded yet" is a normal boot condition for the first few attempts, so it is
logged at WARNING, and only escalates to ERROR if the wait drags on."""
import inspect
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import angelone_ws_feed as f


def test_first_attempts_are_warnings():
    for attempt in (1, 2, 3):
        assert f._scrip_wait_log_level(attempt) == logging.WARNING


def test_long_waits_escalate_to_error():
    for attempt in (4, 5, 20):
        assert f._scrip_wait_log_level(attempt) == logging.ERROR


def test_feed_loop_uses_the_helper_not_a_fixed_error():
    src = inspect.getsource(f)
    assert "_scrip_wait_log_level(attempt)" in src
    assert 'logger.error(\n                    "AngelOne feed: scrip master not loaded yet' not in src
