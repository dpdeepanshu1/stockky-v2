# Session 43 — consolidated open-issue list: fixes applied

Scope: work through the full consolidated open-issue list (13 items across
api-gateway, real-trade-service, position-stocks-service, cross-cutting) and
apply every item that is a genuine, safely-fixable bug. Items that are
confirmed-not-bugs, unverifiable without live access, or would require
inventing new trading logic on a real-money-adjacent service are left open
and re-confirmed rather than guessed at.

## Fixed

### api-gateway

**#1 — `soft_ttl_should_refresh()` wired up (was dead code)**
`data_feed.py` had a complete stale-while-revalidate helper that was
imported into `main.py` but never called anywhere — caches only ever
refreshed reactively, after the hard TTL had already expired and a request
found the key gone.

Wired into `_fetch_fundamental_cached()`'s short-Redis-cache hit path (the
same function already using the refresh-lock mutex from #2): on a cache
hit, if `soft_ttl_should_refresh()` says the key is inside its soft window,
a background task (`_background_refresh_fundamental`, via
`asyncio.create_task`) fetches upstream and repopulates the cache/Data Feed
— using the same `try_refresh_lock`/`release_refresh_lock` mutex as the
reactive path so it can't stampede against a concurrent reactive refresh.
The current request still returns the (still-valid) cached value
immediately; it's the *next* request, after hard expiry, that benefits from
already having a warm cache.

**#2 — refresh-lock leak on non-200 upstream response**
`release_refresh_lock()` was only called inside the `resp.status_code ==
200` branch of the upstream fetch, so a non-200 response (or any exception
before that point) held the lock for its full 5s TTL instead of releasing
immediately. Self-healing (5s TTL), but needlessly staled concurrent
requests for that symbol.

Fixed by extracting the upstream-fetch-and-cache logic into its own
`_refresh_fundamental_upstream()` helper and moving lock acquisition/release
to the caller with a `try/finally`, so the lock now always releases —
200, non-200, or exception — as soon as the fetch attempt finishes. This
same helper is reused by both the reactive path (#2) and the new background
stale-while-revalidate path (#1), so there's one lock-lifecycle to reason
about instead of two.

**#3 — two bare `except: pass` blocks**
- `main.py` (`_get_all_known_symbols()`, was line ~1845): now
  `except Exception as e: logger.debug(...)`.
- `main.py` (trending-stocks fetch loop, was line ~5952): now
  `except Exception as e: logger.debug(...)`.

Both previously caught `KeyboardInterrupt`/`SystemExit` too and gave zero
signal when a lookup silently failed. Now logged at debug level (matching
this file's existing convention for expected, non-fatal per-symbol
failures) and `Exception`-scoped only.

### position-stocks-service

**#11 — `feed/angelone_session.py::rest_headers()` dead code removed**
Confirmed zero callers anywhere in the service (only the audit notes in
`STATUS.md` referenced it, describing it as already-dead). Removed the
method; `_resolve_client_public_ip()` it called is still used by the live
login path and was left untouched.

## Confirmed open — not changed (with reasoning)

**#4 — decision-prediction-service training/prediction + frontend**
Still genuinely outside every audit to date. Not touched this session;
flagging again as its own future dedicated round, same as prior sessions.

**#5 — DATAMATICS 89-retry Dhan SDK MARKET→LIMIT theory**
Still unverifiable without live access to the real dhanhq SDK / a live
incident to reproduce against. Hardening already in place from session40;
the theory itself remains unconfirmed, not something code changes can
close.

**#6 — pre-existing pyflakes backlog in real-trade-service**
Re-checked, still present (unused imports/names only, no logic impact) in
the same 10 files listed previously. Deliberately left untouched again —
this is a live-money service and a pure lint cleanup pass isn't worth the
diff risk in the same session as functional fixes. Still its own flagged
future cleanup task.

**#7 — repeated `emergency_gap_down` SELL retries (session39)**
Still an open unknown, not a closed item — believed to be legitimate
retry-on-broker-rejection behavior, but this has never been confirmed
against live Dhan order-book rejection reasons and can't be from this
sandbox.

**#8 — exit-leg fill price field-name guess (`orders/reconcile.py`)**
Still flagged "ASSUMPTION FLAGGED FOR LIVE VERIFICATION" in the code
itself, exactly as before — Dhan doesn't document `averageTradedPrice` on
nested TARGET_LEG/STOP_LOSS_LEG objects, so this can only be confirmed
against a real filled exit leg, not guessed at further from the sandbox.

**#9 — `shared_order_budget.check_and_reserve()` non-atomic**
Confirmed still read-then-increment rather than a conditional UPDATE. Left
exactly as-is — this was a deliberate fail-open soft-governor design
decision from session 7, not an oversight, and switching it to a strict
atomic check changes real trading behavior (could start rejecting orders
that currently pass) without an explicit decision to do that.

**#10 — `config.MIN_PREFERRED_SCALP_POSITIONS` never wired into gating logic**
Re-confirmed still unused anywhere in `screening/engine.py`, `orders/
entry.py`, or elsewhere. Deliberately **not** wired this session: the
config's own audit-note comment already flags that its intended semantics
(e.g. relaxing entry thresholds to keep at least N positions open) were
never actually specified anywhere, only guessed at. Inventing that gating
behavior now — on a service placing real orders with real capital — isn't
a safe call to make from a guess. Left as a config value with its existing
audit note; needs a real decision from you on what it should do before
anyone wires it up.

**#12 — `STATUS.md` "Next steps" (live Super Order test, EOD-disarm flatten
test, live timestamp check, circuit-breaker-open smoke test)**
Still explicitly untestable from this sandbox — all require your live
VM/market hours. Not bugs, just unverified; unchanged.

### Cross-cutting

**#13 — 5 real-trade-service backend routes with no frontend caller**
Re-verified this pass: all 5 are intentional ops-only diagnostics per their
own docstrings. Not a wiring gap.

## Verification done this session
- `python3 -m py_compile` clean on every file touched
  (`api-gateway/main.py`, `api-gateway/data_feed.py`,
  `position-stocks-service/feed/angelone_session.py`).
- `pyflakes` run on the same files — only pre-existing, untouched findings
  remain (none introduced by this session's edits; none in the functions
  changed here).
- No frontend files touched this session, so no npm build was needed.
