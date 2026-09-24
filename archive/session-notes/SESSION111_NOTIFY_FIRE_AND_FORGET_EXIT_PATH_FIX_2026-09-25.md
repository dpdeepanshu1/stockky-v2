# Session 111 (2026-09-25): `notify_sync` no longer blocks the exit-lock thread for up to ~42s

Picked up item #2 from the session109 open-issues list, flagged again at the end of
session110: `notify_sync` can block its caller for a ~42s worst case, and
`exit_engine/exit.py` calls it 14 times inline while holding the per-mode exit
lock. Scope: `services/real-trade-service` only (`notifier.py` +
`exit_engine/exit.py` + tests). `position-stocks-service` has the same
`notify_sync` shape in its own `notifier.py` and calls it from `orders/*.py`
(entry, exit, overnight_stop, breakeven, eod_squareoff, reconcile) — not touched
this round, flagged as a candidate for its own pass since that service's lock/
loop structure is different enough to want its own look rather than a blind port.

Baseline (sandbox, extracted from this zip): 2716 passed, 1 skipped, 0 xfailed,
8304 stmts, 0 missed. This session ends at **2725 passed, 1 skipped, 0 xfailed,
8320 stmts, 0 missed (100%)**.

## The bug

`_run_exit_tick_sync` (execution/auto_pilot.py) runs `evaluate_mode` on a worker
thread while holding `_get_exit_lock(mode)` — a non-blocking `acquire`, so any
other exit-side operation (the next 5-10s tick, a reconcile, a manual close)
skips outright while this one is running. `evaluate_mode` (exit_engine/exit.py)
calls `notify_sync` after almost every branch: SELL sent, IP-blocked, CDSL-
blocked, fill confirmed, etc — 14 call sites.

`notify_sync` is fully synchronous end to end: `httpx.post` to
notification-scheduler-service (12s timeout) → on any failure, direct Telegram
(15s timeout) → on a non-200/HTML-parse failure, a second direct Telegram
attempt as plain text (another 15s timeout). Worst case ~42s, and it was already
documented as the "never block or fail an order path" contract's blind spot for
this one caller.

So a single slow Telegram delivery for position A's exit — the notification
service being briefly unreachable, or a Telegram API hiccup, nothing at all
wrong with the trade itself — held the exit lock for up to 42s. During that
window: every OTHER open position due for a protective stop-loss / emergency
gap-down check in the same `evaluate_mode` pass waited behind it; and the next
5-10s exit tick skipped entirely rather than queuing, because the lock was still
held. Positions that should have been checked as unprotected went unprotected
for longer than intended, and for no reason connected to their own price action.

## The fix — `notifier.py`: `notify_fire_and_forget`

Rather than touching the shape of the 14 call sites (each one is a multi-line
f-string built inline), the delivery logic itself was split so the blocking and
non-blocking paths share one implementation:

* `_deliver_sync(text)` — the actual network attempt (service, then direct
  Telegram), unchanged logic, just pulled out of `notify_sync`'s body.
* `notify_sync(text)` — unchanged public behaviour: `_should_send` dedup check,
  then `_deliver_sync`, blocking. Still the right choice for the existing
  one-off callers (`adaptive_thresholds.py`'s startup notice,
  `dhan_credentials.py`'s TOTP alerts) that have nothing else waiting on them.
* `notify_fire_and_forget(text)` — new. Does the *same* `_should_send` dedup
  check on the calling thread (cheap, in-memory — keeping the "identical
  message within 5 minutes is suppressed" guarantee exact, with no race between
  two near-simultaneous callers), then hands `_deliver_sync` to a daemon
  background thread and returns immediately. No return value: by the time
  delivery finishes there's no one left in the exit loop to hand a result to —
  the same "never block or fail an order path" contract, taken to its
  conclusion for a caller that genuinely cannot wait. `_deliver_background`
  wraps the call in its own try/except so a bug in delivery can't crash the
  unjoined thread loudly; thread-start failure itself is also swallowed
  (`threading.Thread(...).start()` inside a try/except).

`exit_engine/exit.py`'s only change is its import line:

```python
from notifier import notify_fire_and_forget as notify_sync
```

with a comment explaining why. Every one of the 14 call sites, and every
existing test that does `monkeypatch.setattr(ex, "notify_sync", ...)`, is
untouched — they were already fire-and-forget statements with no caller ever
using the return value, so aliasing the import was sufficient and kept the diff
to two files.

## Tests

`tests/test_notifier.py` gained `TestNotifyFireAndForget` (8 tests): returns
`None` and does not block while delivery is deliberately held open on a
scripted slow transport (proven with a `threading.Event`, not a timing guess);
delivers on a background thread, not the caller's; that thread is a daemon;
duplicate-within-window is suppressed without ever starting a thread (spied via
a wrapped `threading.Thread`); falls back to direct Telegram in the background
same as the blocking variant; thread-start failure and a delivery exception
inside the background thread are both swallowed and logged. Plus one wiring
test — `ex.notify_sync is notifier.notify_fire_and_forget` (and is NOT
`notifier.notify_sync`) — added specifically because every exit test mocks the
attribute wholesale and none of them would otherwise notice a regression back
to the blocking import.

30 mutations run against the new code (dedup check removed, made synchronous,
exception swallow removed, thread not daemon, exit.py import reverted to
blocking `notify_sync`, `notify_sync` stubbed past `_deliver_sync`, and more):
0 survivors — the daemon-flag and import-identity mutations initially survived
against the first draft of the test suite (nothing was checking either), so the
two tests above were added specifically to close those gaps before the round
was called done.

`notifier.py`: 124 stmts, 100%. `exit_engine/exit.py`: 492 stmts, 100%
(unchanged — no new branches, just an import alias). Full real-trade-service
suite: 2725 passed / 1 skipped / 0 xfailed, 8320 stmts, 0 missed. pyflakes clean
on both touched files. `position-stocks-service` untouched, re-ran to confirm:
1337 passed (unchanged).

## Not done / still open

* `position-stocks-service`'s equivalent `notify_sync` call sites
  (`orders/entry.py`, `orders/breakeven.py`, `orders/eod_squareoff.py`,
  `orders/reconcile.py`, `orders/overnight_stop.py`) — same shape of risk, not
  ported this round (see scope note above).
* Nothing else from the open-issues list was picked up this round — this was a
  single focused fix.
