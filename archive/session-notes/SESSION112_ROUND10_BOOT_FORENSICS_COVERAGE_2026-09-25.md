# Session 112, round 10 (2026-09-25): `boot_forensics.py` coverage 67% → ~100% (position-stocks-service)

Next item by priority off round 9's list (`boot_forensics.py` 67%, 44
missing lines: 49-50, 62, 69-70, 84-85, 89-105, 131-152, 197-198 —
`_read_int`'s except branch, `memory_snapshot`'s cgroup-v1 fallback and
`/proc/self/status` except branch, `_write`'s except branch, the entire
`_heartbeat_loop`, the entire `_install_signal_logging` (including the
closure handler's chain-to-previous and `SIG_DFL` branches), and
`mark_clean_shutdown`'s except branch).

This service already had a `tests/test_boot_forensics.py` (32 lines), but
it only exercised `record_boot`/`_classify` via the public entry point —
none of the module's other functions. Diffed this service's
`boot_forensics.py` against real-trade-service's copy first: byte-identical
(both services build from their own directory, per the module's own
docstring, but the file itself hasn't drifted). Real-trade-service already
has a comprehensive 751-line `tests/test_boot_forensics.py` covering the
whole module — so this round ported that file wholesale (service-name
strings swapped `real-trade-service` -> `position-stocks-service`,
docstring updated to this round's coverage numbers) rather than writing a
new suite. The existing 32-line file's two scenarios (the `_classify`
cause cycle, and the wrong-shape-JSON self-heal regression) are both
present in the ported file's `TestClassify`/`TestRecordBoot` sections, so
nothing was lost — the ported file replaces it outright rather than living
alongside it.

## What the ported suite covers

`TestReadInt`, `TestMemorySnapshot` (cgroup v2 present, v2 absent -> v1
fallback, v1's huge-sentinel-limit -> `None`, `/proc/self/status` RSS
parse success/absence/malformed), `TestMb`, `TestWrite` (success and the
except-swallow branch), `TestHeartbeatLoop` (one iteration via a fake
`time.sleep` that raises after N calls, memory-pressure warning threshold
and its 60s repeat-suppression, the loop's own except-swallow), `TestClassify`
(all five causes plus edge cases: missing heartbeat falls back to
`started_at`, empty dict, `None`-valued signal), `TestInstallSignalLogging`
(handler chains to a callable previous handler, `SIG_DFL` branch re-raises
the signal to self, install failure on a non-main-thread is swallowed),
`TestRecordBoot` (the full classify-cycle + wrong-shape-JSON self-heal +
daemon thread only spawns once across re-boots in the same process), and
`TestMarkCleanShutdown` (success and except-swallow).

No bug found this round — pure coverage gap, same class as `tz_utils.py`
last round.

## Verification

Unlike `tz_utils.py`, this module isn't pure-stdlib-with-no-side-effects
(threads, signals, file I/O) and the ported suite itself needs pytest
(fixtures, `monkeypatch`, `tmp_path`) — unavailable in this sandbox, no
network to install it. Verified the underlying logic directly instead: a
standalone script hand-driving the real, unmodified `boot_forensics.py`
through 11 checks covering the branches the ported suite's harder-to-trace
sections exercise — `memory_snapshot`'s cgroup-v1 fallback and
huge-limit-sentinel handling, `_write`'s except-swallow on a bad path,
`mark_clean_shutdown`'s except-swallow on a broken lock,
`_install_signal_logging`'s real signal-handler chaining (installed a fake
`signal.signal`/`getsignal`, triggered the real installed handler, confirmed
it recorded state AND called through to the previous handler), and the
full `record_boot` classify-cycle including the wrong-shape-JSON self-heal.
All 11/11 passed against the real module. `py_compile` clean on the ported
test file.

## Next by priority (unchanged from round 9's list, minus this item)

`auth/admin_auth.py` 29%, `auth/dhan_credentials_ro.py` 46%,
`pipeline_status.py` 31%, then `db.py` 16%, `execution/dhan_client.py` 21%,
`feed/*` and `main.py`.
