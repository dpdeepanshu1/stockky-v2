# Session 112 (2026-09-25): `notify_sync` no longer blocks order-path threads in position-stocks-service

Picked up the item flagged as "not done" at the end of session111
(`archive/session-notes/SESSION111_NOTIFY_FIRE_AND_FORGET_EXIT_PATH_FIX_2026-09-25.md`):
`position-stocks-service` has the same blocking-`notify_sync` shape as
`real-trade-service` did, called inline from `orders/entry.py`,
`orders/breakeven.py`, `orders/eod_squareoff.py`, `orders/overnight_stop.py`
and `orders/reconcile.py`. Not a blind port — flagged deliberately for its own
pass since this service's lock/loop structure differs from real-trade-service's
exit engine.

Baseline (this zip, before this session): 1337 passed, 80% overall coverage,
`notifier.py` at 53% (83 stmts, 39 missed — no dedicated test file for the
fire-and-forget path existed yet).

## The bug

Same root cause as session111: `notify_sync` is fully synchronous end to end —
`httpx.post` to notification-scheduler-service (12s timeout) → on failure,
direct Telegram (15s timeout) → on a non-200/HTML-parse failure, a second
direct Telegram attempt as plain text (another 15s timeout). Worst case ~42s.

Every order-path caller in this service ran that inline inside either a
to_thread-wrapped stage of `_run_cycle()` (under `_cycle_lock`, shared with
screening/entry for every OTHER candidate that cycle) or
`_fast_reconcile_loop()` (no lock, but a single sequential while-loop, so a
slow call here delays that loop's own next 5-10s tick the same way) — see
main.py. Two of `notify_critical()`'s own callers (main.py's eDIS morning
check) were worse still: called directly on the event loop with no to_thread
wrapper at all, so a blocking `notify_sync` there could stall every request
this service was handling, not just its own background loop.

## The fix

`notifier.py` gains the same `notify_fire_and_forget` / `_deliver_sync` /
`_deliver_background` split as real-trade-service's session111 fix:

* `_deliver_sync(text)` — the actual network attempt (service, then direct
  Telegram), pulled out of `notify_sync`'s body, unchanged logic.
* `notify_sync(text)` — unchanged public behaviour: `_should_send` dedup
  check, then `_deliver_sync`, blocking.
* `notify_fire_and_forget(text)` — new. Same `_should_send` dedup check on
  the calling thread (keeps the "identical message within 5 minutes is
  suppressed" guarantee exact, no race between near-simultaneous callers),
  then hands `_deliver_sync` to a daemon background thread
  (`_deliver_background`, its own try/except so a delivery bug can't crash
  the unjoined thread loudly) and returns immediately. Thread-start failure
  itself is also swallowed.
* `notify_critical(text)` — now calls `notify_fire_and_forget` instead of
  `notify_sync`. No change needed at either of its two main.py call sites.

Call-site changes, two shapes (this service didn't have a single local-import
convention like real-trade-service's `exit_engine/exit.py`):

* Module-attribute call sites (`notifier.notify_sync(...)`) in
  `orders/entry.py` (x2), `orders/breakeven.py`, `orders/overnight_stop.py`
  (x2), `orders/reconcile.py` (x2) — changed to
  `notifier.notify_fire_and_forget(...)` directly, since an import-alias
  trick doesn't apply to a module-attribute reference.
* Local-import call sites (`from notifier import notify_sync`) in
  `orders/eod_squareoff.py` (x2) — changed to
  `from notifier import notify_fire_and_forget as notify_sync`, same alias
  pattern as real-trade-service's `exit_engine/exit.py`, so the call
  expression at each site (`notify_sync(...)`) needed no further edit.

None of the nine call sites use `notify_sync`'s return value — every one was
already a fire-and-forget statement, so this is purely a blocking → non-
blocking swap with no behavioural change to what gets sent or when a caller
"sees" delivery finish (never did).

## Tests

Every existing test file that monkeypatches `notifier.notify_sync` to
intercept an order-path alert (`test_entry.py`, `test_breakeven.py`,
`test_eod_squareoff.py`, `test_overnight_stop.py`, `test_reconcile.py`) now
also patches `notifier.notify_fire_and_forget` to the same recording
callable — needed because those call sites no longer resolve through the
`notify_sync` name at all, so a test that only patched the old name would
silently stop intercepting and start real (network) calls.
`notifier.notify_critical` patches were untouched — call sites still go
through that name unchanged, and its own new internal behaviour is covered
separately.

New `tests/test_notifier_fire_and_forget.py`: `TestNotifyFireAndForget` (8
tests, same shape as real-trade-service's session111 suite) — returns `None`
and does not block while delivery is deliberately held open on a scripted
slow `_deliver_sync` (proven with `threading.Event`, not a timing guess);
delivers on a background thread, not the caller's; that thread is a daemon;
duplicate-within-window is suppressed without ever starting a thread (spied
via a `threading.Thread` subclass); falls back to direct Telegram in the
background same as the blocking variant (real httpx.Client over a
MockTransport); thread-start failure and a delivery exception inside the
background thread are both swallowed and logged, exercised both via the
public `notify_fire_and_forget` entry point and by calling
`_deliver_background` directly. `TestNotifyCriticalUsesFireAndForget` (3
tests): routes through `notify_fire_and_forget` with the CRITICAL prefix,
swallows an exception from it, and an end-to-end non-blocking proof using a
held-open `_deliver_sync`. `TestOrderPathCallSitesUseFireAndForget` (5
tests, `inspect.getsource`-based): pins each of the five order modules'
source text against a future regression back to `notifier.notify_sync(`,
and pins `eod_squareoff.py`'s two local imports to the aliased form — added
specifically because every order-file test mocks the notifier attribute
wholesale and none of them would otherwise notice a regression to the
blocking call.

## Verification notes

This sandbox has no outbound network access, so `pytest`/`httpx` were not
installable here and the full `pytest -q --cov=.` run from the earlier
session could not be re-executed in this environment. In their place:
every touched file was `ast.parse`-checked clean, and the new
`notify_fire_and_forget` / `_deliver_background` / `notify_critical` logic
was exercised directly with plain `python3` (a stubbed `httpx` module,
manual `threading.Event`/spy-`Thread` harnesses matching the new test
file's approach) — all six manual checks passed: non-blocking return,
correct dedup-vs-thread-start behaviour, daemon flag, thread-start-failure
swallow, `notify_critical` routing, and background-exception swallow. The
committed test file itself was not run by pytest in this session — please
run `python3 -m pytest -q --cov=. --cov-report=term-missing` from
`services/position-stocks-service` after unzipping to get a real pass/fail
and coverage number before treating this as verified in your own
environment.

## Not done / still open

* Full coverage re-run to confirm `notifier.py` reaches 100% and the
  suite-wide 1337+ count still passes, given this sandbox's network
  restriction — see "Verification notes" above.
* Nothing else from either session's open-issues list was picked up this
  round — this was a single focused port.

## Round 2 (same day): closing the coverage gap after a real pytest run

The user ran the round-1 zip for real: **1353 passed, 0 failed** — the fix and
all nine call-site migrations were correct on the first try. Coverage,
however, showed `notifier.py` at only 77% (99 stmts, 23 missed:
85-86, 118, 121, 140-143, 211-216, 225-226, 241-247), because:

* Every order-file test fixture monkeypatches `notify_sync` /
  `notify_fire_and_forget` wholesale, so the real `notify_sync` body (140-143)
  never runs in the suite at all.
* `_deliver_sync`'s success / not-delivered / service-unreachable branches
  (211-216) were never exercised with a real (mocked-transport) httpx call —
  only `_direct_telegram` had that kind of test, and only for one shape.
* `_direct_telegram`'s no-token-configured branch (225-226) and its non-200
  HTML-reject → plain-text-retry branch (241-247) were untested.
* `_should_send`'s refresh-on-resend-after-expiry branch (118) and its LRU
  eviction branch (121) were untested — the existing dedup tests only ever
  sent up to a couple of distinct messages within the window.
* `_TelegramTokenRedactingFilter.filter`'s malformed-record guard (85-86) was
  untested.
* Also found: `test_notifier_fire_and_forget.py`'s own `net` fixture (lines
  76-77) was dead code — the one test that took it as a parameter shadowed
  it with a second, local `httpx.post` monkeypatch, so the fixture's handler
  body never actually ran. Removed the unused fixture rather than wiring it
  up, since the test already had its own working mock.

New `tests/test_notifier_core.py` (12 tests) closes the remaining gaps by
calling the real functions directly rather than through a fixture's
monkeypatch: `_should_send` refresh + eviction, `notify_sync` (duplicate
short-circuit, real-delivery True, real-delivery False), `_deliver_sync`
(service delivers / service 200-but-not-delivered-falls-back / service
unreachable-falls-back, each via a real `httpx.Client` over a
`httpx.MockTransport`), `_direct_telegram` (no token/chat-id configured;
HTML-mode-rejected retries as plain text and succeeds; the plain-text retry
itself raising returns False), and the token filter's malformed-record path.

Every new assertion in both this file and the resulting diff was verified
by hand first, the same way as round 1: real notifier.py functions called
directly against a stubbed `httpx` module and a controllable clock, no
pytest available in this sandbox (still no outbound network here — see
round 1's "Verification notes"). All checks passed. As before, please run

    python3 -m pytest -q --cov=. --cov-report=term-missing

from `services/position-stocks-service` to get the real numbers — expected
result is `notifier.py` at or very close to 100%, with the previous 1353
passed count now a little higher (23 new test functions across the two
files) and no failures.
