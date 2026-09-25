# Session 112, round 20 (2026-09-25): `main.py` — closing the last coverage gaps to 100% (position-stocks-service)

Picks up where round 18's `test_main.py` left off. That round took `main.py`
from 22% to 92% but explicitly scoped out `lifespan()` (FastAPI startup/
shutdown — the single largest remaining block, 77 lines, completely
untested) and left a handful of smaller branches uncovered in
`_fast_reconcile_loop`, `_run_cycle`, `_capital_cooldown_filter`, `get_db`,
and `/dhan/live-orders`.

Confirmed against a real run (the person's own instance, `python3 -m pytest
-q --cov=. --cov-report=term-missing` from `services/position-stocks-service`
with all real dependencies installed) that `main.py` sat at 700 stmts / 56
missed / 92%, missing lines `155-231, 328, 341-343, 351-352, 371-372,
375-376, 886, 981, 1914`. Every one of those is closed this round.

## What was added (all in `tests/test_main.py`)

### `TestLifespan` (new class) — `lifespan()`, lines 155-231

Drives the real `@asynccontextmanager` function end-to-end:
`async with m.lifespan(m.app): pass` runs the startup half up to `yield`,
then `__aexit__` runs the shutdown half. `_trading_loop`/`_fast_reconcile_loop`
are replaced with a coroutine that awaits a never-set `asyncio.Event`, so
`asyncio.create_task(...)` produces real, cancellable tasks without ever
looping — lifespan's own cancel-and-await-`CancelledError` shutdown code
then runs for real against them.

  * Happy path: `boot_forensics.record_boot`, `auth.admin_auth.log_auth_config`,
    `_db.init_tables`, `shared_symbol_lock.cleanup_stale`, `ws_client.start`/
    `register_on_tick`/`stop`, and the two background tasks all fire exactly
    once, in order, with `_engine_tick_hook` as the registered callback
  * `boot_forensics.record_boot` and `auth.admin_auth.log_auth_config` each
    raising — both caught (`except Exception: logger.debug(...)`), startup
    still completes
  * `config.RISK_PER_TRADE_PCT_CONFIRMED = False` → warning logged
  * `config.ADMIN_PASSWORD_HASH`/`SESSION_SECRET` empty → warning logged
  * `shared_symbol_lock.cleanup_stale` returning released locks → warning
    logged with the count
  * `boot_forensics.mark_clean_shutdown` raising on the shutdown path →
    caught by the bare `except: pass`, shutdown still completes

### `_fast_reconcile_loop` — remaining branches

Round 18 covered the exception-swallowed paths for stagnation-exit and the
market-closed/stuck-pending paths, but not:
  * `run_stagnation_exit`/`run_breakeven_stop` returning a truthy count →
    the `logger.info(...)` success-log branch for each (previously only
    exercised with `0`/`None`, or via a lambda whose return value was
    `.__setitem__`'s `None`)
  * `run_breakeven_stop` raising → its own `except Exception` branch
    (only the stagnation-exit exception path had a test before)
  * `run_eod_squareoff` raising when the EOD check is due → its
    `except Exception` branch
  * `run_retention_cleanup` raising → its `except Exception` branch
  * `is_market_open_ist()` raising — the ONE call site outside every inner
    `try` block (it runs between the two `with factory() as db:` blocks) —
    the only way to reach the loop's own outer `except Exception:` handler
    rather than one of the five inner per-stage ones

### `_run_cycle` — no candidate clears the Quality Gate

`test_no_candidates_ends_cycle` (round 18) covers the *empty scan()* early
return, which is a different code path from every quality-gate candidate
existing but failing quality — the latter reaches the
`if entered_candidate: ... else:` branch and never hit the `else`
(the "No candidate cleared the Quality Gate" stage detail) until now.

### `_capital_cooldown_filter` — the two untaken branches inside the loop

Existing tests only ever exercised "no starved symbols" (short-circuits
before the loop) and "still cooling down" (`skipped`). Added:
  * A candidate whose symbol isn't in `_capital_starved` at all (some
    *other* symbol is) — falls straight to `kept` without touching the
    cooldown/growth math
  * A starved symbol whose cooldown window has already elapsed — comes off
    the map, `kept`
  * A starved symbol retried early because available capital has grown
    enough since the skip — same `kept`/pop-from-map path, different
    trigger

### `get_db()` and `/dhan/live-orders`

  * `get_db()`'s existing test only constructed the generator and closed it
    without ever calling `next()` — the `yield from _db.get_db()` body
    itself never ran. Patches `db.get_db` to a fake generator and actually
    pulls one value through.
  * `/dhan/live-orders`: `_is_ours()`'s `if not isinstance(o, dict): return
    False` guard — every existing test's Dhan response was a list of
    dicts; added one with a bare string in the list alongside a real
    tagged order.

## Bug found and fixed: broken `%`-format warning in `lifespan()`

Writing the "RISK_PER_TRADE_PCT_CONFIRMED not set" warning test surfaced a
real formatting bug in the log call itself: the message contained
`"RISK_PER_TRADE_PCT=<your chosen %> in env..."` — a bare, unescaped `%`
that isn't a valid format spec once `config.RISK_PER_TRADE_PCT` is
substituted in for the earlier `%.1f%%`. Every time this branch actually
fired (`RISK_PER_TRADE_PCT_CONFIRMED` unset/false at startup — exactly a
misconfigured-deployment scenario), `logger.warning(...)` raised inside
stdlib logging's own formatter. Python's default `StreamHandler.emit()`
swallows that via `handleError()`, so it never crashed the service, but the
intended warning was silently replaced by a bare "Logging error" traceback
on stderr instead of ever being readable — for precisely the misconfiguration
this warning exists to flag. Fixed by escaping the stray `%` as `%%`; no
behavior change beyond the message actually rendering now.

## Verification

  * `tests/test_main.py` alone: **125 passed**, `main.py` 92% → **100%**
    (0 missing statements).
  * Full service suite (`tests/`, `--cov=.`): **2238 passed, 2 skipped**,
    no regressions. Total service coverage 15291→15528 statements (new
    test code counted too), 147→96 missed, ~99% either way.
  * `py_compile` clean on `main.py` and `tests/test_main.py`.

## Next by priority

Every production module in this service is now ≥89% covered; nothing left
above single-digit-miss-count. Remaining gaps, smallest-effort-first:
  * `db.py` (98%, 3 lines: 296-298) and `orders/adaptive.py`/`orders/entry.py`/
    `orders/reconcile.py`/`screening/engine.py` (each 99%, 1 line) — all
    one-line defensive branches, likely not worth a dedicated session.
  * `config.py` (92%, 9 lines: 31-32, 38-39, 471-475) and
    `execution/dhan_client.py` (97%, 11 lines) — small, contained gaps if
    ever prioritized.
  * `feed/scrip_master.py` (89%, 22 lines: 133-144, 157-164, 277, 285-286,
    291) — the largest remaining production-code gap in the service by
    line count, though still a small one in absolute terms compared to
    where `main.py`/`ws_client.py` started.
