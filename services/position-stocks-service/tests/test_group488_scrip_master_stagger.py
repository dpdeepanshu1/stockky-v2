"""group488: the first background refresh of a stale scrip-master snapshot waits ANGELONE_SCRIP_MASTER_BG_START_DELAY_S
(+ up to 50 % jitter, counted from process start) so position-stocks and market-data do not download the file at the same
moment after `docker compose up`. A cold start (no map) is not delayed. 0 switches it off.

Run from services/position-stocks-service:  python3 -m pytest tests/test_group488_scrip_master_stagger.py -v
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import feed.scrip_master as sm


def _state(monkeypatch, delay=30.0, started_ago=0.0):
    monkeypatch.setattr(sm, "_BG_START_DELAY_S", delay)
    monkeypatch.setattr(sm, "_PROC_START", time.time() - started_ago)
    monkeypatch.setattr(sm, "_bg_first_done", False)


def test_first_background_refresh_waits_the_rest_of_the_delay(monkeypatch):
    _state(monkeypatch, delay=30.0, started_ago=5.0)
    monkeypatch.setattr(sm.random if hasattr(sm, "random") else __import__("random"), "uniform", lambda a, b: 0.0)
    w = sm._bg_start_wait_s()
    assert 24.0 <= w <= 25.5


def test_only_the_first_background_refresh_waits(monkeypatch):
    _state(monkeypatch, delay=30.0, started_ago=0.0)
    assert sm._bg_start_wait_s() > 0
    assert sm._bg_start_wait_s() == 0.0


def test_no_wait_when_the_process_is_already_old_enough(monkeypatch):
    _state(monkeypatch, delay=30.0, started_ago=100.0)
    assert sm._bg_start_wait_s() == 0.0


def test_zero_delay_switches_it_off(monkeypatch):
    _state(monkeypatch, delay=0.0, started_ago=0.0)
    assert sm._bg_start_wait_s() == 0.0


def test_jitter_stays_within_half_the_delay(monkeypatch):
    for _ in range(20):
        _state(monkeypatch, delay=30.0, started_ago=0.0)
        assert 29.0 <= sm._bg_start_wait_s() <= 45.1


def test_bg_refresh_sleeps_then_refreshes_and_releases_the_lock(monkeypatch):
    _state(monkeypatch, delay=30.0, started_ago=0.0)
    calls = []
    monkeypatch.setattr(sm.time, "sleep", lambda s: calls.append(("sleep", s)))
    monkeypatch.setattr(sm, "_refresh_locked", lambda force=False: calls.append(("refresh", force)))
    assert sm._load_lock.acquire(blocking=False)
    sm._bg_refresh_and_release()
    assert [c[0] for c in calls] == ["sleep", "refresh"] and calls[0][1] > 0
    assert not sm._load_lock.locked()


def test_a_failing_refresh_still_releases_the_lock(monkeypatch):
    _state(monkeypatch, delay=0.0)
    def boom(force=False):
        raise RuntimeError("x")
    monkeypatch.setattr(sm, "_refresh_locked", boom)
    assert sm._load_lock.acquire(blocking=False)
    try:
        sm._bg_refresh_and_release()
    except RuntimeError:
        pass
    assert not sm._load_lock.locked()


def test_cold_start_is_never_delayed(monkeypatch):
    """No map: ensure_loaded fetches inline through _refresh_locked, which does not call the stagger."""
    _state(monkeypatch, delay=30.0, started_ago=0.0)
    sm._token_map, sm._loaded_at, sm._fail_count, sm._next_retry_at, sm._disk_tried = {}, 0.0, 0, 0.0, True
    monkeypatch.setattr(sm, "_fetch_map", lambda: {"SBIN": "3045"})
    monkeypatch.setattr(sm, "_save_disk", lambda m: None)
    t0 = time.time()
    sm.ensure_loaded(wait_s=5.0)
    assert time.time() - t0 < 2.0 and sm._token_map == {"SBIN": "3045"}
    assert sm._bg_first_done is False
    sm._token_map, sm._loaded_at = {}, 0.0
