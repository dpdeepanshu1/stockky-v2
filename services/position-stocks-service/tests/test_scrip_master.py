"""
tests/test_scrip_master.py

Covers feed/scrip_master.py (session112 round 17).

scrip_master is structured around module-level state that must be reset
between tests — every test calls _reset() to clear the map, counters, and
disk-tried flag so tests don't bleed into each other.

Testing strategy:
  * Pure helpers: _clean, _iter_json_array, _rows_to_map
    — no I/O, tested directly.
  * Stateful map helpers: _warm_from_disk (with a real tmp file),
    _save_disk (round-trip), _record_failure (backoff math),
    _is_stale, _refresh_locked (monkeypatched _fetch_map).
  * Public API: get_token, get_tokens_bulk, get_all_nse_eq, status,
    ensure_loaded — each exercised against a pre-seeded module state.

All network calls (_fetch_map, httpx.stream) are fully monkeypatched.

Run from services/position-stocks-service:
    python3 -m pytest tests/test_scrip_master.py -q \\
        --cov=feed.scrip_master --cov-report=term-missing
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import feed.scrip_master as sm


# ─── helpers ────────────────────────────────────────────────────────────────

def _reset(monkeypatch=None):
    """Reset all module-level state between tests."""
    sm._token_map = {}
    sm._loaded_at = 0.0
    sm._fail_count = 0
    sm._next_retry_at = 0.0
    sm._disk_tried = False
    # release _load_lock if it's somehow still held
    if sm._load_lock.locked():
        try:
            sm._load_lock.release()
        except RuntimeError:
            pass


def _seed_map(data: dict):
    sm._token_map = dict(data)
    sm._loaded_at = time.time()


SAMPLE_ROWS = [
    {"exch_seg": "NSE", "symbol": "SBIN-EQ", "token": "3045"},
    {"exch_seg": "NSE", "symbol": "RELIANCE-EQ", "token": "2885"},
    {"exch_seg": "NSE", "symbol": "INFY-EQ", "token": "1594"},
    # BSE row — must be ignored
    {"exch_seg": "BSE", "symbol": "SBIN-EQ", "token": "500112"},
    # Missing token — must be ignored
    {"exch_seg": "NSE", "symbol": "TCS-EQ", "token": ""},
    # Non-dict — must be ignored
    "bad_row",
]

SAMPLE_JSON = json.dumps(SAMPLE_ROWS)


# ══════════════════════════════════════════════════════════════════════════════
# _clean
# ══════════════════════════════════════════════════════════════════════════════

class TestClean:
    def test_uppercases(self):
        assert sm._clean("sbin") == "SBIN"

    def test_strips_ns_suffix(self):
        assert sm._clean("SBIN.NS") == "SBIN"

    def test_strips_bo_suffix(self):
        assert sm._clean("RELIANCE.BO") == "RELIANCE"

    def test_strips_whitespace(self):
        assert sm._clean("  INFY  ") == "INFY"

    def test_empty_string(self):
        assert sm._clean("") == ""

    def test_none_becomes_empty(self):
        assert sm._clean(None) == ""


# ══════════════════════════════════════════════════════════════════════════════
# _iter_json_array
# ══════════════════════════════════════════════════════════════════════════════

class TestIterJsonArray:
    def _chunks(self, text, size=10):
        return [text[i:i + size] for i in range(0, len(text), size)]

    def test_simple_array(self):
        result = list(sm._iter_json_array(["[1, 2, 3]"]))
        assert result == [1, 2, 3]

    def test_array_of_dicts(self):
        payload = '[{"a": 1}, {"b": 2}]'
        result = list(sm._iter_json_array([payload]))
        assert result == [{"a": 1}, {"b": 2}]

    def test_chunked_delivery(self):
        payload = json.dumps([{"x": i} for i in range(20)])
        chunks = self._chunks(payload, 7)
        result = list(sm._iter_json_array(iter(chunks)))
        assert result == [{"x": i} for i in range(20)]

    def test_bom_tolerance(self):
        result = list(sm._iter_json_array(["\ufeff[42]"]))
        assert result == [42]

    def test_empty_array(self):
        result = list(sm._iter_json_array(["[]"]))
        assert result == []

    def test_empty_chunks_are_skipped(self):
        result = list(sm._iter_json_array(["", "[1]", ""]))
        assert result == [1]

    def test_not_an_array_raises(self):
        with pytest.raises(ValueError, match="not a JSON array"):
            list(sm._iter_json_array(['{"x": 1}']))

    def test_malformed_element_raises(self):
        # Force a single oversized pending buffer
        original = sm._MAX_PENDING_CHARS
        sm._MAX_PENDING_CHARS = 5
        try:
            with pytest.raises(ValueError, match="malformed JSON"):
                # Open bracket, then garbage that never closes
                list(sm._iter_json_array(["[" + "x" * 100]))
        finally:
            sm._MAX_PENDING_CHARS = original


# ══════════════════════════════════════════════════════════════════════════════
# _rows_to_map
# ══════════════════════════════════════════════════════════════════════════════

class TestRowsToMap:
    def test_filters_nse_eq_only(self):
        result = sm._rows_to_map(SAMPLE_ROWS)
        assert "SBIN" in result
        assert "RELIANCE" in result
        assert "INFY" in result

    def test_bse_excluded(self):
        result = sm._rows_to_map(SAMPLE_ROWS)
        # BSE SBIN token is "500112", NSE is "3045"
        assert result.get("SBIN") == "3045"

    def test_empty_token_excluded(self):
        result = sm._rows_to_map(SAMPLE_ROWS)
        assert "TCS" not in result

    def test_non_dict_row_skipped(self):
        result = sm._rows_to_map(SAMPLE_ROWS)  # "bad_row" in list
        assert isinstance(result, dict)

    def test_strips_eq_suffix(self):
        rows = [{"exch_seg": "NSE", "symbol": "WIPRO-EQ", "token": "111"}]
        result = sm._rows_to_map(rows)
        assert "WIPRO" in result
        assert "WIPRO-EQ" not in result

    def test_token_coerced_to_str(self):
        rows = [{"exch_seg": "NSE", "symbol": "X-EQ", "token": 9999}]
        result = sm._rows_to_map(rows)
        assert result["X"] == "9999"

    def test_empty_input(self):
        assert sm._rows_to_map([]) == {}


# ══════════════════════════════════════════════════════════════════════════════
# _save_disk / _warm_from_disk
# ══════════════════════════════════════════════════════════════════════════════

class TestDiskSnapshot:
    def test_round_trip(self, tmp_path, monkeypatch):
        cache = str(tmp_path / "cache.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        _reset()

        sample = {"SBIN": "3045", "INFY": "1594"}
        sm._save_disk(sample)

        # _warm_from_disk should restore it
        sm._warm_from_disk()
        assert sm._token_map == sample

    def test_warm_from_disk_uses_cache_once(self, tmp_path, monkeypatch):
        cache = str(tmp_path / "cache.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        _reset()

        sm._save_disk({"RELIANCE": "2885"})
        sm._warm_from_disk()
        # second call is a no-op (disk_tried=True)
        sm._token_map = {}
        sm._warm_from_disk()
        assert sm._token_map == {}  # was cleared, second call didn't restore

    def test_stale_cache_ignored(self, tmp_path, monkeypatch):
        cache = str(tmp_path / "cache.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        monkeypatch.setattr(sm, "_CACHE_MAX_AGE_S", 1.0)
        _reset()

        old = {"OLD": "999"}
        blob = {"saved_at": time.time() - 100, "map": old}
        with open(cache, "w") as f:
            json.dump(blob, f)

        sm._warm_from_disk()
        assert sm._token_map == {}  # ignored because too old

    def test_missing_cache_file_does_not_raise(self, tmp_path, monkeypatch):
        cache = str(tmp_path / "nonexistent.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        _reset()
        sm._warm_from_disk()  # must not raise
        assert sm._token_map == {}

    def test_corrupt_cache_does_not_raise(self, tmp_path, monkeypatch):
        cache = str(tmp_path / "corrupt.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        _reset()
        with open(cache, "w") as f:
            f.write("NOT JSON!!!")
        sm._warm_from_disk()  # must not raise
        assert sm._token_map == {}

    def test_save_disk_bad_path_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(sm, "_CACHE_PATH", "/no/such/directory/x.json")
        _reset()
        sm._save_disk({"A": "1"})  # must log warning, not raise

    def test_warm_skips_if_map_already_populated(self, tmp_path, monkeypatch):
        cache = str(tmp_path / "cache.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        _reset()
        sm._save_disk({"X": "1"})

        sm._token_map = {"EXISTING": "999"}  # already populated
        sm._warm_from_disk()
        # disk should not overwrite the existing map
        assert "EXISTING" in sm._token_map


# ══════════════════════════════════════════════════════════════════════════════
# _record_failure / _is_stale
# ══════════════════════════════════════════════════════════════════════════════

class TestFailureBackoff:
    def test_first_failure_sets_backoff(self):
        _reset()
        sm._record_failure("test error")
        assert sm._fail_count == 1
        assert sm._next_retry_at > time.time()

    def test_backoff_doubles(self):
        _reset()
        monkeypatch_base = sm._FAIL_BACKOFF_BASE_S
        sm._fail_count = 0
        sm._record_failure("err1")
        first_backoff = sm._next_retry_at - time.time()
        sm._record_failure("err2")
        second_backoff = sm._next_retry_at - time.time()
        # second backoff should be roughly double (allow some slop for timing)
        assert second_backoff > first_backoff

    def test_backoff_capped_at_max(self):
        _reset()
        for _ in range(20):
            sm._record_failure("repeated")
        remaining = sm._next_retry_at - time.time()
        assert remaining <= sm._FAIL_BACKOFF_MAX_S + 1.0

    def test_is_stale_true_when_empty(self):
        _reset()
        assert sm._is_stale() is True

    def test_is_stale_false_when_fresh(self):
        _reset()
        _seed_map({"SBIN": "3045"})
        assert sm._is_stale() is False

    def test_is_stale_true_when_old(self, monkeypatch):
        _reset()
        monkeypatch.setattr(sm, "REFRESH_INTERVAL_S", 1.0)
        _seed_map({"SBIN": "3045"})
        sm._loaded_at = time.time() - 10
        assert sm._is_stale() is True


# ══════════════════════════════════════════════════════════════════════════════
# _refresh_locked (monkeypatched _fetch_map)
# ══════════════════════════════════════════════════════════════════════════════

class TestRefreshLocked:
    def _patch_fetch(self, monkeypatch, result=None, raises=None):
        def fake_fetch():
            if raises:
                raise raises
            # NOTE (session112 round18 fix): `result or {...}` was wrong here
            # — an explicitly-passed empty dict `{}` is falsy in Python, so it
            # silently fell through to the non-empty default instead of
            # exercising the empty-result branch. Use an explicit None check.
            return {"SBIN": "3045"} if result is None else result
        monkeypatch.setattr(sm, "_fetch_map", fake_fetch)

    def test_success_updates_map(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "c.json"))
        self._patch_fetch(monkeypatch, result={"SBIN": "3045", "INFY": "1594"})
        _reset()
        with sm._load_lock:
            sm._refresh_locked()
        assert sm._token_map == {"SBIN": "3045", "INFY": "1594"}
        assert sm._fail_count == 0

    def test_fetch_exception_records_failure(self, monkeypatch):
        self._patch_fetch(monkeypatch, raises=RuntimeError("net down"))
        _reset()
        with sm._load_lock:
            sm._refresh_locked()
        assert sm._fail_count == 1
        assert sm._token_map == {}  # still empty

    def test_empty_result_records_failure(self, monkeypatch):
        self._patch_fetch(monkeypatch, result={})
        _reset()
        with sm._load_lock:
            sm._refresh_locked()
        assert sm._fail_count == 1

    def test_skips_when_not_stale(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "c.json"))
        called = []
        monkeypatch.setattr(sm, "_fetch_map", lambda: called.append(1) or {"X": "1"})
        _reset()
        _seed_map({"SBIN": "3045"})
        with sm._load_lock:
            sm._refresh_locked(force=False)
        assert len(called) == 0  # not stale, no fetch

    def test_force_bypasses_staleness_check(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "c.json"))
        self._patch_fetch(monkeypatch, result={"NEW": "1"})
        _reset()
        _seed_map({"OLD": "0"})  # not stale
        with sm._load_lock:
            sm._refresh_locked(force=True)
        assert "NEW" in sm._token_map

    def test_skips_during_backoff(self, monkeypatch):
        called = []
        monkeypatch.setattr(sm, "_fetch_map", lambda: called.append(1) or {"X": "1"})
        _reset()
        sm._next_retry_at = time.time() + 9999  # in backoff
        with sm._load_lock:
            sm._refresh_locked(force=False)
        assert len(called) == 0


# ══════════════════════════════════════════════════════════════════════════════
# ensure_loaded
# ══════════════════════════════════════════════════════════════════════════════

class TestEnsureLoaded:
    def _patch_fetch(self, monkeypatch, result, tmp_path):
        monkeypatch.setattr(sm, "_fetch_map", lambda: result)
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "c.json"))

    def test_cold_start_waits_and_populates(self, monkeypatch, tmp_path):
        self._patch_fetch(monkeypatch, {"SBIN": "3045"}, tmp_path)
        _reset()
        sm.ensure_loaded(wait_s=5.0)
        assert "SBIN" in sm._token_map

    def test_fresh_map_returns_immediately(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(sm, "_fetch_map", lambda: called.append(1) or {"X": "1"})
        _reset()
        _seed_map({"SBIN": "3045"})
        sm.ensure_loaded()
        assert called == []  # not stale, no fetch

    def test_backoff_skips_fetch(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(sm, "_fetch_map", lambda: called.append(1) or {"X": "1"})
        _reset()
        sm._next_retry_at = time.time() + 9999
        sm.ensure_loaded()
        assert called == []

    def test_stale_map_triggers_background_refresh(self, monkeypatch, tmp_path):
        self._patch_fetch(monkeypatch, {"NEW": "2"}, tmp_path)
        _reset()
        _seed_map({"OLD": "0"})
        sm._loaded_at = time.time() - sm.REFRESH_INTERVAL_S - 10
        sm.ensure_loaded()
        # Background thread may not have finished yet; just ensure no exception
        # and that the old map is still being served (stale-while-revalidate)
        assert sm._token_map  # map not cleared

    def test_warm_from_disk_called_on_cold_start(self, monkeypatch, tmp_path):
        cache = str(tmp_path / "warm.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        monkeypatch.setattr(sm, "_fetch_map", lambda: {"FRESH": "1"})
        _reset()
        sm._save_disk({"WARM": "99"})
        # Reset disk_tried so warm_from_disk runs
        sm._disk_tried = False
        sm.ensure_loaded(wait_s=0.0)
        # Even if fetch doesn't run, warm map is seeded
        assert sm._token_map  # something in map


# ══════════════════════════════════════════════════════════════════════════════
# Public API: get_token / get_tokens_bulk / get_all_nse_eq / status
# ══════════════════════════════════════════════════════════════════════════════

class TestPublicApi:
    def setup_method(self):
        _reset()
        _seed_map({"SBIN": "3045", "RELIANCE": "2885", "INFY": "1594"})

    def test_get_token_found(self):
        assert sm.get_token("SBIN") == "3045"

    def test_get_token_lowercased(self):
        assert sm.get_token("sbin") == "3045"

    def test_get_token_ns_suffix(self):
        assert sm.get_token("RELIANCE.NS") == "2885"

    def test_get_token_not_found_returns_none(self):
        assert sm.get_token("NOTEXIST") is None

    def test_get_tokens_bulk_resolves_present(self):
        result = sm.get_tokens_bulk(["SBIN", "RELIANCE", "MISSING"])
        assert result == {"SBIN": "3045", "RELIANCE": "2885"}
        assert "MISSING" not in result

    def test_get_tokens_bulk_empty_input(self):
        assert sm.get_tokens_bulk([]) == {}

    def test_get_tokens_bulk_ns_suffix(self):
        result = sm.get_tokens_bulk(["INFY.NS"])
        assert result == {"INFY": "1594"}

    def test_get_all_nse_eq_returns_copy(self):
        result = sm.get_all_nse_eq()
        assert result == {"SBIN": "3045", "RELIANCE": "2885", "INFY": "1594"}
        # mutating the returned dict must not affect module state
        result["HACK"] = "0"
        assert "HACK" not in sm._token_map

    def test_status_fields(self):
        s = sm.status()
        assert s["loaded_symbols"] == 3
        assert s["loaded_at"] is not None
        assert isinstance(s["age_seconds"], float)
        assert s["source_url"] == sm.SCRIP_MASTER_URL
        assert isinstance(s["loading"], bool)
        assert s["consecutive_failures"] == 0
        assert s["next_retry_in_s"] == 0.0

    def test_status_when_empty(self):
        _reset()
        s = sm.status()
        assert s["loaded_symbols"] == 0
        assert s["loaded_at"] is None
        assert s["age_seconds"] is None

    def test_status_failure_count(self):
        _reset()
        sm._fail_count = 3
        sm._next_retry_at = time.time() + 100
        s = sm.status()
        assert s["consecutive_failures"] == 3
        assert s["next_retry_in_s"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# _load_sync (blocking reload)
# ══════════════════════════════════════════════════════════════════════════════

class TestLoadSync:
    def test_load_sync_forces_refresh(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "c.json"))
        monkeypatch.setattr(sm, "_fetch_map", lambda: {"SYNC": "777"})
        _reset()
        _seed_map({"OLD": "0"})
        sm._load_sync()
        assert sm._token_map.get("SYNC") == "777"


# ══════════════════════════════════════════════════════════════════════════════
# _fetch_map (session112 round 21) — the real network path, never exercised
# before: every other test monkeypatches _fetch_map itself away entirely.
# httpx.stream is faked with a minimal context manager + a response stub
# exposing raise_for_status()/iter_text(), matching how _fetch_map actually
# uses it (`with httpx.stream(...) as resp: resp.raise_for_status(); ...
# resp.iter_text()`) — no real socket involved.
# ══════════════════════════════════════════════════════════════════════════════

class _FakeHttpxResponse:
    def __init__(self, text_chunks, status_error=None):
        self._chunks = text_chunks
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error is not None:
            raise self._status_error

    def iter_text(self):
        return iter(self._chunks)


class _FakeHttpxStream:
    """Stands in for what httpx.stream(...) returns, used only as
    `with httpx.stream(...) as resp:`."""
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self._resp

    def __exit__(self, *exc):
        return False


class TestFetchMap:
    def test_happy_path_parses_and_returns_nse_eq_map(self, monkeypatch):
        chunks = [
            "[",
            '{"symbol":"SBIN-EQ","exch_seg":"NSE","token":"3045"},',
            '{"symbol":"BOGUS","exch_seg":"BSE","token":"1"}',
            "]",
        ]
        resp = _FakeHttpxResponse(chunks)
        monkeypatch.setattr(
            sm.httpx, "stream",
            lambda method, url, timeout=None: _FakeHttpxStream(resp),
        )
        result = sm._fetch_map()
        assert result == {"SBIN": "3045"}  # the BSE row is filtered out by _rows_to_map

    def test_http_status_error_propagates(self, monkeypatch):
        err = sm.httpx.HTTPStatusError("500 error", request=None, response=None)
        resp = _FakeHttpxResponse([], status_error=err)
        monkeypatch.setattr(
            sm.httpx, "stream",
            lambda method, url, timeout=None: _FakeHttpxStream(resp),
        )
        with pytest.raises(sm.httpx.HTTPStatusError):
            sm._fetch_map()

    def test_wall_clock_cap_raises_timeout_error(self, monkeypatch):
        """The per-chunk deadline check inside _fetch_map's own _text_chunks
        closure — deterministic via a scripted time.monotonic() sequence
        rather than an actual sleep: the first call computes `deadline`,
        the second (inside the generator, checking the first chunk) is
        made to read as already past it."""
        # deadline = first_call + _MAX_DOWNLOAD_S (default 180s) — the
        # follow-up calls need to clear THAT, not just the first value.
        calls = iter([100.0] + [10_000.0] * 10)
        monkeypatch.setattr(sm.time, "monotonic", lambda: next(calls))
        resp = _FakeHttpxResponse(["[", "{}"])
        monkeypatch.setattr(
            sm.httpx, "stream",
            lambda method, url, timeout=None: _FakeHttpxStream(resp),
        )
        with pytest.raises(TimeoutError, match="wall-clock cap"):
            sm._fetch_map()


# ══════════════════════════════════════════════════════════════════════════════
# _save_disk — the write-failure cleanup path (session112 round 21)
# ══════════════════════════════════════════════════════════════════════════════

class TestSaveDiskWriteFailure:
    def test_replace_failure_cleans_up_tmp_and_logs(self, tmp_path, monkeypatch):
        """os.replace fails after mkstemp already created the tmp file —
        exercises the inner except's os.unlink(tmp) AND the outer
        except-as-e logger.warning, both otherwise unreached by
        test_save_disk_bad_path_does_not_raise (which fails earlier, at
        makedirs/mkstemp, before any tmp file exists to clean up)."""
        cache = str(tmp_path / "cache.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        _reset()

        def _boom_replace(src, dst):
            raise OSError("simulated replace failure")
        monkeypatch.setattr(sm.os, "replace", _boom_replace)

        sm._save_disk({"A": "1"})  # must not raise
        # the tmp file created by mkstemp should have been cleaned up, and
        # the real cache path was never written to.
        assert not os.path.exists(cache)
        assert not any(p.name.startswith(".scrip_master_") for p in tmp_path.iterdir())

    def test_replace_and_unlink_both_fail_still_logs_and_does_not_raise(self, tmp_path, monkeypatch):
        """Both os.replace AND the cleanup os.unlink fail — the nested
        `except OSError: pass` inside the cleanup try, then the outer
        except-as-e logger.warning."""
        cache = str(tmp_path / "cache.json")
        monkeypatch.setattr(sm, "_CACHE_PATH", cache)
        _reset()

        monkeypatch.setattr(sm.os, "replace", lambda src, dst: (_ for _ in ()).throw(OSError("replace boom")))
        monkeypatch.setattr(sm.os, "unlink", lambda path: (_ for _ in ()).throw(OSError("unlink boom too")))

        sm._save_disk({"A": "1"})  # must not raise despite both failing


# ══════════════════════════════════════════════════════════════════════════════
# ensure_loaded — remaining branches (session112 round 21)
# ══════════════════════════════════════════════════════════════════════════════

class TestEnsureLoadedRemainingBranches:
    def test_cold_start_returns_immediately_during_backoff_window(self, monkeypatch, tmp_path):
        """Empty map, but a recent failure's backoff hasn't elapsed yet —
        returns without ever touching _fetch_map. Points _CACHE_PATH at a
        file that doesn't exist so _warm_from_disk() can't quietly
        populate the map first and take a different branch."""
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "does-not-exist.json"))
        called = []
        monkeypatch.setattr(sm, "_fetch_map", lambda: called.append(1) or {"X": "1"})
        _reset()
        sm._next_retry_at = time.time() + 9999
        sm.ensure_loaded()
        assert called == []
        assert sm._token_map == {}

    def test_stale_background_refresh_thread_start_failure_releases_lock(self, monkeypatch, tmp_path):
        """threading.Thread(...).start() itself raising — the acquired
        _load_lock must still be released rather than left stuck held
        forever (which would wedge every future stale-while-revalidate
        attempt)."""
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "does-not-exist.json"))
        _reset()
        _seed_map({"OLD": "1"})
        sm._loaded_at = time.time() - sm.REFRESH_INTERVAL_S - 10  # stale

        class _BoomThread:
            def __init__(self, *a, **kw):
                pass

            def start(self):
                raise RuntimeError("thread start boom")
        monkeypatch.setattr(sm.threading, "Thread", _BoomThread)

        sm.ensure_loaded()  # must not raise
        assert not sm._load_lock.locked()

    def test_cold_start_gives_up_if_lock_already_held_elsewhere(self, monkeypatch, tmp_path):
        """Simulates another thread already mid-fetch: _load_lock is held
        before ensure_loaded() is even called, so the cold-start
        single-flight acquire (bounded by wait_s) times out and returns
        with the map still empty, exactly as a real caller falling back
        to its non-AngelOne path would see."""
        monkeypatch.setattr(sm, "_CACHE_PATH", str(tmp_path / "does-not-exist.json"))
        called = []
        monkeypatch.setattr(sm, "_fetch_map", lambda: called.append(1) or {"X": "1"})
        _reset()
        sm._load_lock.acquire()
        try:
            sm.ensure_loaded(wait_s=0.05)
            assert called == []
            assert sm._token_map == {}
        finally:
            sm._load_lock.release()


# ── _reset() with lock held (round-30) ───────────────────────────────────────
class TestResetWithLockHeld:
    """Lines 52-55 in _reset() — the `if sm._load_lock.locked(): sm._load_lock.release()`
    branch — are never exercised because every call to _reset() happens when
    the lock is free.  Acquire the lock before calling _reset() to hit the branch."""

    def test_reset_releases_a_held_lock(self):
        sm._load_lock.acquire()
        assert sm._load_lock.locked()
        _reset()                          # must not raise; must release the lock
        assert not sm._load_lock.locked()

    def test_reset_swallows_runtime_error_from_release(self):
        """The `except RuntimeError: pass` guard — reached when release() is
        called on a lock that looks locked() but actually isn't ours to
        release (e.g. released concurrently between the check and the call).
        Simulate with a fake lock object so the branch is exercised
        deterministically."""

        class _FakeLock:
            def locked(self_inner):
                return True

            def release(self_inner):
                raise RuntimeError("release unlocked lock")

        real_lock = sm._load_lock
        sm._load_lock = _FakeLock()
        try:
            _reset()  # must not raise — the RuntimeError is swallowed
        finally:
            sm._load_lock = real_lock
