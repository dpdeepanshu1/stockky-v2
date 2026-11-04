"""Round 207: the regime score reading goes through write-behind, the history-based threshold is cached, and _rr_floor honours the caller's base."""
import time
import types

import pytest

import config
import regime_adaptive as ra
import write_behind as wb
from entry_engine import entry


class _DB:
    def __init__(self):
        self.inserted, self.commits, self.rollbacks = [], 0, 0

    def bulk_insert_mappings(self, model, rows):
        self.inserted.append((model.__name__, list(rows)))

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_regime_score_kind_is_registered():
    assert "regime_score" in wb._handlers


def test_handler_inserts_readings_and_prunes_at_most_once_an_hour(monkeypatch):
    import adaptive_thresholds as at
    pruned = []
    monkeypatch.setattr(at, "_prune_old_scores", lambda db: pruned.append(1))
    monkeypatch.setattr(wb, "_last_regime_prune", 0.0)
    db = _DB()
    wb._write_regime_score(db, [{"score": 61}, {"score": None}, {"score": 58.5}])
    assert db.inserted == [("MarketRegimeHistory", [{"score": 61.0}, {"score": 58.5}])] and db.commits == 1 and len(pruned) == 1
    wb._write_regime_score(db, [{"score": 40}])
    assert len(pruned) == 1 and len(db.inserted) == 2          # inside the hour: inserted, not pruned again
    monkeypatch.setattr(wb, "_last_regime_prune", time.monotonic() - 3601.0)
    wb._write_regime_score(db, [{"score": 41}])
    assert len(pruned) == 2


def test_handler_with_no_usable_reading_writes_nothing():
    db = _DB()
    wb._write_regime_score(db, [{"score": None}])
    assert db.inserted == [] and db.commits == 0


def test_a_failing_prune_keeps_the_readings_and_leaves_the_session_clean(monkeypatch):
    import adaptive_thresholds as at

    def boom(db):
        raise RuntimeError("delete timed out")
    monkeypatch.setattr(at, "_prune_old_scores", boom)
    monkeypatch.setattr(wb, "_last_regime_prune", 0.0)
    db = _DB()
    wb._write_regime_score(db, [{"score": 50}])
    assert db.commits == 1 and db.rollbacks == 1 and len(db.inserted) == 1


def test_rr_floor_never_goes_below_the_callers_base_and_follows_a_raised_adapted_value(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_MIN_REWARD_RISK", 2.0, raising=False)
    assert entry._rr_floor() == 2.0 and entry._rr_floor(100.0) == 100.0
    monkeypatch.setattr(ra, "effective", lambda name, base: 2.35 if name == "ENTRY_MIN_REWARD_RISK" else base)
    assert entry._rr_floor() == 2.35 and entry._rr_floor(100.0) == 100.0 and entry._rr_floor(2.0) == 2.35
    monkeypatch.setattr(ra, "effective", lambda name, base: 1.5)           # an adapted value below the owner's is ignored
    assert entry._rr_floor() == 2.0
    assert entry._rr_floor("junk") == 2.0                                   # an unreadable base falls back to 2.0


def test_threshold_ttl_env(monkeypatch):
    monkeypatch.delenv("REGIME_THRESHOLD_CACHE_S", raising=False)
    assert entry._regime_threshold_ttl_s() == 600.0
    monkeypatch.setenv("REGIME_THRESHOLD_CACHE_S", "0")
    assert entry._regime_threshold_ttl_s() == 0.0
    monkeypatch.setenv("REGIME_THRESHOLD_CACHE_S", "-5")
    assert entry._regime_threshold_ttl_s() == 0.0
    monkeypatch.setenv("REGIME_THRESHOLD_CACHE_S", "abc")
    assert entry._regime_threshold_ttl_s() == 600.0


class _Http:
    def __init__(self, score):
        self.score = score
        self.calls = 0

    def client(self):
        outer = self

        class C:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

            async def get(s, *a, **k):
                outer.calls += 1
                return types.SimpleNamespace(status_code=200, json=lambda: {"market_score": outer.score})
        return C()


@pytest.fixture()
def regime(monkeypatch):
    import adaptive_thresholds as at
    monkeypatch.setattr(entry, "_regime_cache", {"score": None, "threshold": None, "source": None, "ts": 0.0})
    st = types.SimpleNamespace(http=_Http(60), thr_calls=0, recorded=[], queued=[], wb_on=True)
    monkeypatch.setattr(entry.httpx, "AsyncClient", lambda *a, **k: st.http.client())
    monkeypatch.setattr(at, "record_market_score", lambda db, s: st.recorded.append(s))

    def thr(db):
        st.thr_calls += 1
        return 40, "adaptive_20d_p20"
    monkeypatch.setattr(at, "adaptive_regime_threshold", thr)

    def submit(kind, payload):
        if st.wb_on:
            st.queued.append((kind, payload))
        return st.wb_on
    monkeypatch.setattr(entry._write_behind, "submit", submit)
    monkeypatch.delenv("REGIME_THRESHOLD_CACHE_S", raising=False)
    return st


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_reading_is_queued_not_written_in_the_cycle(regime):
    assert _run(entry._get_market_regime(None)) == (True, 60, 40, "adaptive_20d_p20")
    assert regime.queued == [("regime_score", {"score": 60.0})] and regime.recorded == []


def test_reading_is_written_synchronously_when_write_behind_is_off(regime):
    regime.wb_on = False
    _run(entry._get_market_regime(None))
    assert regime.recorded == [60] and regime.queued == []


def test_threshold_history_read_is_reused_inside_the_threshold_ttl(regime, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    _run(entry._get_market_regime(None))
    now[0] += 130.0                                         # score cache expired, threshold cache (600 s) still valid
    regime.http.score = 70
    assert _run(entry._get_market_regime(None)) == (True, 70, 40, "adaptive_20d_p20")
    assert regime.http.calls == 2 and regime.thr_calls == 1
    now[0] += 500.0                                         # now past 600 s since the threshold was read
    _run(entry._get_market_regime(None))
    assert regime.thr_calls == 2


def test_ttl_zero_reads_the_threshold_every_time(regime, monkeypatch):
    monkeypatch.setenv("REGIME_THRESHOLD_CACHE_S", "0")
    now = [1000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    _run(entry._get_market_regime(None))
    now[0] += 130.0
    _run(entry._get_market_regime(None))
    assert regime.thr_calls == 2
