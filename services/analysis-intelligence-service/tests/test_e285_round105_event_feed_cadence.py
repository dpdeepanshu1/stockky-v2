"""tests/test_e285_round105_event_feed_cadence.py - the event feed collector polls on the trading-day clock, and a fast poll is not answered from the 300 s cache.

    python3 -m pytest tests/test_e285_round105_event_feed_cadence.py -q       or       python3 tests/test_e285_round105_event_feed_cadence.py
(plain functions; httpx / feedparser are stubbed for the downloader tests, so it also runs where they are not installed)
"""
import importlib.util
import os
import sys
import types
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
_IST = timezone(timedelta(hours=5, minutes=30))
_ENVS = ("EVENT_FEED_CADENCE_V2", "EVENT_FEED_OPEN_S", "EVENT_FEED_SESSION_S", "EVENT_FEED_RAMP_S", "EVENT_FEED_EVENING_S", "EVENT_FEED_NIGHT_S")


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, "..", rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ef = _load("event_feed_r105", os.path.join("event", "event_feed.py"))


def _clean_env():
    for k in _ENVS:
        os.environ.pop(k, None)


def _ist(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=_IST)


# 2026-11-02 is a Monday, 2026-11-07 a Saturday, 2026-11-08 a Sunday
def test_weekday_phases_and_intervals():
    _clean_env()
    cases = [((2, 3, 0), "night", 7200), ((2, 8, 0), "ramp", 600), ((2, 8, 59), "ramp", 600), ((2, 9, 0), "open", 60), ((2, 9, 19), "open", 60),
             ((2, 9, 20), "session", 180), ((2, 15, 29), "session", 180), ((2, 15, 30), "evening", 1800), ((2, 19, 59), "evening", 1800),
             ((2, 20, 0), "night", 7200), ((2, 23, 59), "night", 7200)]
    for (d, h, m), phase, ivl in cases:
        name, every, _until = ef.poll_plan(_ist(2026, 11, d, h, m))
        assert (name, every) == (phase, ivl), (h, m, name, every)


def test_weekend_uses_night_interval_all_day_and_wakes_at_midnight():
    _clean_env()
    for day in (7, 8):
        name, every, until = ef.poll_plan(_ist(2026, 11, day, 10, 0))
        assert name == "weekend" and every == 7200 and until == 14 * 3600
    assert ef.poll_plan(_ist(2026, 11, 6, 21, 0))[0] == "night"          # Friday evening is still a weekday night


def test_sleep_is_cut_at_the_next_boundary():
    _clean_env()
    assert ef.next_delay_s(_ist(2026, 11, 2, 7, 30)) == 1800              # night interval 2 h, but 08:00 is 30 min away
    assert ef.next_delay_s(_ist(2026, 11, 2, 8, 55)) == 300               # ramp 10 min, 09:00 is 5 min away
    assert ef.next_delay_s(_ist(2026, 11, 2, 9, 19, 30)) == 30            # open phase ends at 09:20
    assert ef.next_delay_s(_ist(2026, 11, 2, 9, 5)) == 60
    assert ef.next_delay_s(_ist(2026, 11, 2, 9, 21)) == 180
    assert ef.next_delay_s(_ist(2026, 11, 2, 15, 29)) == 60               # session ends at 15:30
    assert ef.next_delay_s(_ist(2026, 11, 2, 19, 50)) == 600              # evening 30 min, 20:00 is 10 min away
    assert ef.next_delay_s(_ist(2026, 11, 2, 12, 0, 0)) == 180
    assert ef.next_delay_s(_ist(2026, 11, 2, 9, 19, 59, )) >= 1.0


def test_wake_ups_chain_through_the_morning():
    _clean_env()
    t = _ist(2026, 11, 3, 7, 0)                                            # Tuesday 07:00
    seen = []
    while t < _ist(2026, 11, 3, 9, 25):
        seen.append(t.strftime("%H:%M"))
        t = t + timedelta(seconds=ef.next_delay_s(t))
    for must in ("08:00", "09:00", "09:20"):
        assert must in seen, (must, seen)
    assert seen.count("09:01") + seen.count("09:02") == 2                  # the one-minute loop is on between 09:00 and 09:20


def test_env_overrides_and_the_60_second_floor():
    _clean_env()
    os.environ["EVENT_FEED_OPEN_S"] = "20"
    os.environ["EVENT_FEED_SESSION_S"] = "420"
    os.environ["EVENT_FEED_EVENING_S"] = "junk"
    try:
        assert ef.poll_plan(_ist(2026, 11, 2, 9, 10))[1] == 60            # floor
        assert ef.poll_plan(_ist(2026, 11, 2, 12, 0))[1] == 420
        assert ef.poll_plan(_ist(2026, 11, 2, 16, 0))[1] == 1800          # unreadable value = default
    finally:
        _clean_env()


def test_cadence_switch():
    _clean_env()
    assert ef.cadence_v2_enabled() is True
    for off in ("0", "false", "off", "no"):
        os.environ["EVENT_FEED_CADENCE_V2"] = off
        try:
            assert ef.cadence_v2_enabled() is False
            assert ef._cache_max_age_s() is None
        finally:
            _clean_env()


def test_cache_max_age_follows_the_interval():
    _clean_env()
    age = ef._cache_max_age_s()
    assert age is not None and 30.0 <= age <= 0.8 * 7200 + 1


def test_start_collector_with_explicit_interval_keeps_the_flat_sleep():
    src = open(os.path.join(_HERE, "..", "event", "event_feed.py"), encoding="utf-8").read()
    assert "scheduled = interval_s is None and cadence_v2_enabled()" in src and "time.sleep(every)" in src and "next_delay_s(now)" in src


# ── downloader: max_age_s ────────────────────────────────────────────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, status=200, body=b"<rss/>"):
        self.status_code, self.content, self.headers = status, body, {"content-type": "application/rss+xml"}


def _feed_fetch(responses):
    """feed_fetch loaded with stub httpx / feedparser; `responses` is a list consumed one per download."""
    calls = []

    class _Client:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url):
            calls.append(url)
            return responses.pop(0) if len(responses) > 1 else responses[0]

    fake_httpx = types.SimpleNamespace(Client=_Client)
    fake_fp = types.SimpleNamespace(parse=lambda raw: types.SimpleNamespace(entries=[{"title": "ABC wins order"}] if raw and b"rss" in raw else [], bozo=False))
    saved = {k: sys.modules.get(k) for k in ("httpx", "feedparser")}
    sys.modules["httpx"], sys.modules["feedparser"] = fake_httpx, fake_fp
    try:
        mod = _load("feed_fetch_r105", os.path.join("news", "feed_fetch.py"))
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return mod, calls


def test_default_call_is_still_served_from_cache():
    ff, calls = _feed_fetch([_Resp()])
    url = "https://x.test/feed"
    ff.fetch_feed_ex(url, "t")
    p, info = ff.fetch_feed_ex(url, "t")
    assert info["cached"] is True and len(calls) == 1 and len(p.entries) == 1


def test_max_age_downloads_again_when_the_cached_copy_is_older():
    ff, calls = _feed_fetch([_Resp()])
    url = "https://x.test/feed"
    ff.fetch_feed_ex(url, "t")
    ff._stored_at[url] -= 100.0                                            # the cached copy is now 100 s old
    _p, info = ff.fetch_feed_ex(url, "t", max_age_s=48.0)
    assert info["cached"] is False and len(calls) == 2
    _p, info = ff.fetch_feed_ex(url, "t", max_age_s=48.0)                  # the new copy is fresh again
    assert info["cached"] is True and len(calls) == 2


def test_max_age_does_not_override_a_young_copy():
    ff, calls = _feed_fetch([_Resp()])
    url = "https://x.test/feed"
    ff.fetch_feed_ex(url, "t")
    _p, info = ff.fetch_feed_ex(url, "t", max_age_s=48.0)
    assert info["cached"] is True and len(calls) == 1


def test_max_age_never_bypasses_a_cached_failure():
    ff, calls = _feed_fetch([_Resp(503, b"x")])
    url = "https://x.test/down"
    ff.fetch_feed_ex(url, "t")
    n = len(calls)
    if url in ff._stored_at:
        ff._stored_at[url] -= 500.0
    _p, info = ff.fetch_feed_ex(url, "t", max_age_s=10.0)
    assert len(calls) == n and info["cached"] is True                      # a dead source is not hit every minute


def test_clear_cache_also_clears_stored_times():
    ff, _calls = _feed_fetch([_Resp()])
    ff.fetch_feed_ex("https://x.test/feed", "t")
    assert ff._stored_at
    ff.clear_cache()
    assert not ff._stored_at and not ff._cache


def test_default_fetch_passes_max_age_only_with_the_cadence_on():
    src = open(os.path.join(_HERE, "..", "event", "event_feed.py"), encoding="utf-8").read()
    assert "max_age = _cache_max_age_s()" in src and "max_age_s=max_age" in src


if __name__ == "__main__":
    n = 0
    for k, f in sorted(globals().items()):
        if k.startswith("test_") and callable(f):
            f(); n += 1; print("ok", k)
    print(n, "passed")
