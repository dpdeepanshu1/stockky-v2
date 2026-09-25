# Session 112, round 21 (2026-09-25): `feed/scrip_master.py` — closing the last coverage gaps to 100% (position-stocks-service)

Confirmed against a real run (the person's own instance) that after round 20
`main.py` reached 100% and the largest remaining production-code gap in the
service was `feed/scrip_master.py`: 200 stmts / 20 missed / 90%, missing
lines `133-144, 157-162, 277, 285-286, 291` (the sandbox here showed 22
missed with a slightly wider `157-164` range — same branches, one extra
statement per line-numbering quirk between environments; irrelevant to what
needed covering). Every one of those is closed this round.

Round 17's `test_scrip_master.py` already covered every pure-logic helper
(`_clean`, `_iter_json_array`, `_rows_to_map`), the disk-snapshot round-trip,
failure backoff, and `_refresh_locked`/`ensure_loaded`'s main paths — but by
its own docstring, `_fetch_map` (the real `httpx.stream` network call) was
"fully monkeypatched" away in *every* test, never exercised directly, and a
few defensive/edge branches in `_save_disk` and `ensure_loaded` were never
reached either.

## What was added (all in `tests/test_scrip_master.py`)

### `TestFetchMap` (new class) — `_fetch_map()`, lines 133-144

`httpx.stream` faked with a minimal context manager (`_FakeHttpxStream`) and
response stub (`_FakeHttpxResponse` — `raise_for_status()` +
`iter_text()`), matching exactly how `_fetch_map` uses it. No real socket.

  * Happy path: a scripted multi-chunk JSON array streams through
    `_iter_json_array`/`_rows_to_map` end to end, and a non-NSE row is
    correctly filtered out
  * `resp.raise_for_status()` raising `httpx.HTTPStatusError` propagates
    out of `_fetch_map` uncaught (the caller, `_refresh_locked`, is what
    turns it into a backoff — already covered)
  * The wall-clock cap: `_text_chunks`'s own per-chunk `time.monotonic()`
    check raising `TimeoutError`. Driven deterministically with a scripted
    `time.monotonic()` sequence (first call computes `deadline` as
    `t0 + _MAX_DOWNLOAD_S`, later calls read as already past it) rather
    than an actual multi-minute sleep.

### `TestSaveDiskWriteFailure` (new class) — `_save_disk`, lines 157-164

`test_save_disk_bad_path_does_not_raise` (round 17) fails at
`os.makedirs`/`tempfile.mkstemp` — before any tmp file exists — so it never
reaches the write-failure cleanup path at all. Added:
  * `os.replace` raising after `mkstemp` already created the tmp file —
    exercises the inner `try: os.unlink(tmp)` cleanup succeeding, then the
    outer `except Exception as e: logger.warning(...)`; asserts the tmp
    file is actually gone afterward
  * Both `os.replace` AND the cleanup `os.unlink` raising — the nested
    `except OSError: pass` that swallows a failed cleanup too, so a
    disk-snapshot write failure can never raise into the caller no matter
    how badly it fails

### `TestEnsureLoadedRemainingBranches` (new class) — `ensure_loaded()`, lines 277, 285-286, 291

Round 17's `test_backoff_skips_fetch` looked like it should already cover
line 277 (empty map + backoff window not yet elapsed → early return) but
never reliably did: it didn't pin `_CACHE_PATH`, so on a machine with a
leftover snapshot at the real default path (`/tmp/position_stocks_scrip_
master_cache.json`), `_warm_from_disk()` could populate a fresh-enough map
first and the test would take the "not stale" branch instead — explaining
why this line still showed as missing despite looking covered. Every new
test in this class pins `_CACHE_PATH` to a nonexistent file under `tmp_path`
so `_warm_from_disk()` can't quietly change which branch runs:
  * Cold, empty map, backoff window not yet elapsed → returns without
    calling `_fetch_map` at all (deterministic version of the flaky case
    above)
  * Stale-while-revalidate: `threading.Thread(...).start()` itself raising
    — the `_load_lock` acquired just before must still be released rather
    than left stuck held forever (which would wedge every future refresh
    attempt); asserts `_load_lock.locked()` is `False` afterward
  * Cold start where `_load_lock` is already held by "another thread"
    (pre-acquired by the test itself) — the bounded `acquire(timeout=...)`
    times out and `ensure_loaded` returns with the map still empty, exactly
    as a real caller falling back to its non-AngelOne path would see

## Verification

  * `tests/test_scrip_master.py` alone: **65 passed**,
    `feed/scrip_master.py` 90% → **100%** (0 missing statements).
  * Full service suite (`tests/`, `--cov=.`): **2246 passed, 2 skipped**,
    no regressions.

## Next by priority

Only single-digit-miss-count gaps remain anywhere in the service:
  * `execution/dhan_client.py` (97%, 11 lines: 158-163, 244, 246,
    252-253, 960-961) — the largest of what's left.
  * `config.py` (92%, 9 lines: 31-32, 38-39, 471-475).
  * `db.py` (98%, 3 lines: 296-298), and one-line gaps in
    `orders/adaptive.py`, `orders/entry.py`, `orders/reconcile.py`,
    `screening/engine.py`.

None of these are likely to justify a full session on their own scale —
worth batching into one round if pursued further, rather than one round
each.
