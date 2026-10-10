# Group 296 - the watchlist trigger no longer queues a symbol the account already holds (real-trade-service)

Follow-up to the open "AURIONPRO queued-while-held check".

## What was found
`entry_engine/entry.py::evaluate_watchlist_entries` queues a `BUY NOW` candidate for every active watchlist row whose price is
inside its band. It never looked at held positions. The candidate generator (`candidates.py`) already excludes held symbols, and
the risk engine rejects a second buy (`no_pyramiding`), so no double buy could happen. But a held symbol with an active row was
queued, picked up by the entry engine and rejected, again and again.

## What changed
- `_held_symbols_for_trigger(db, mode)`: the symbols held in the mode (OPEN, PARTIALLY_CLOSED, PENDING_EXIT - the same set the
  risk engine uses), only while `TradeRiskConfig.allow_pyramiding` is off for that mode (no config row counts as off). Empty when
  pyramiding is allowed, the switch is off, or any read fails (fail-open: the risk engine still rejects).
- A band-ok row for a held symbol is not queued. It stays `active` (not missed, not expired) and queues normally once the
  position closes. The tally gets `held_skipped` only when it is non-zero, so existing tally comparisons are unchanged.
- The skip joins the existing "SKIPPED (not queued, stay active)" summary line, throttled per row like the adverse-move lines.
- `ENTRY_WATCHLIST_SKIP_HELD` (default on; `0`/`false`/`no`/`off` = old behaviour), added to `.env.example` and
  `.env.oracle.recommended`.

## Not changed
- I could not reproduce the AURIONPRO case itself (no log in the repo), so this closes the gap the code showed, not a specific
  trade. If AURIONPRO was bought twice, that is a different problem (the risk engine and the shared symbol lock are untouched).

## Tests
`tests/test_group296_watchlist_skip_held.py` (24): held statuses, closed positions do not block, other mode, pyramiding allowed /
off, per-mode risk config, switch values, fail-open, row stays active and queues after close, count adds up, log throttle,
unchanged tally shape. 11 mutations on the new logic, all caught (two needed extra tests first).

Full real-trade suite: **4180 passed, 1 skipped** (4156 + 24), Python 3.12. Other services unchanged in this group.
