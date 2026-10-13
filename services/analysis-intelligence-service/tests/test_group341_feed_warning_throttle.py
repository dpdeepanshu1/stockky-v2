"""group341: a dead news feed is logged at WARNING once per hour, then at DEBUG."""
import logging

from news import feed_fetch as ff


def test_first_warning_then_debug(caplog):
    ff._warned_at.clear()
    with caplog.at_level(logging.DEBUG, logger=ff.logger.name):
        ff._warn_throttled("k1", "feed %s dead", "a")
        ff._warn_throttled("k1", "feed %s dead", "a")
    levels = [r.levelno for r in caplog.records if "dead" in r.getMessage()]
    assert levels == [logging.WARNING, logging.DEBUG]


def test_different_keys_each_warn(caplog):
    ff._warned_at.clear()
    with caplog.at_level(logging.WARNING, logger=ff.logger.name):
        ff._warn_throttled("a", "x %s", 1)
        ff._warn_throttled("b", "x %s", 2)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_warns_again_after_the_interval(monkeypatch, caplog):
    ff._warned_at.clear()
    t = {"now": 1000.0}
    monkeypatch.setattr(ff.time, "time", lambda: t["now"])
    with caplog.at_level(logging.WARNING, logger=ff.logger.name):
        ff._warn_throttled("k", "x")
        t["now"] += ff._WARN_EVERY_S + 1
        ff._warn_throttled("k", "x")
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
