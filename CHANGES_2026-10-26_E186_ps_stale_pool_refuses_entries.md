# E186 - position-stocks: new entries kept being sized from a stale scalp pool after Dhan's funds read failed

Includes E185 (and E184).

## What I read
`capital/ledger.py::sync_from_broker` (all of it, plus `_pick_balance`, `_log_balance_key`, `sync_peer_pnl`, `publish_exposure`, `reserve_capital`, `reserve_additional`)
and its callers (`main.py` cycle step "Capital Sync", `POST /ledger/sync`).

## Finding (fixed)
`sync_from_broker` refreshes the pool (50% of Dhan's free cash plus what the pool has committed) at the start of every cycle. When Dhan's funds call raised, the
balance field was missing, or the balance read <= 0, it logged and returned 0.0 WITHOUT touching the ledger. Nothing read `last_synced_from_broker_at`, so
`reserve_capital` went on sizing and approving entries from the last pool for as long as the failure lasted - hours, if the token had expired - even though the pool
could now be larger than the cash the account really has (the shortfall comes out of real-trade-service's half of the same account). Two messages also said "synced"
when nothing had synced: the cycle's "Capital Sync" stage ("scalp pool synced from Dhan - total_allocated_capital=0.00") and `POST /ledger/sync` ({"status": "synced"}).

## Change
- `capital/ledger.py`: `_sync_age_s(row)` and `_MAX_SYNC_AGE_S = 900` (the same 15-minute stale mark real-trade-service uses for the shared exposure table; the loop
  syncs every ~13 s). `reserve_capital` refuses a new entry (returns None, pool untouched) while the last successful sync is older than that, with one warning per
  5 minutes. Exits, reconcile and EOD are not affected. A row that never recorded a sync time (None) is not blocked, so an older row or a hand-built one behaves as before.
  A successful sync clears it on the next cycle.
- `main.py`: the cycle's Capital Sync stage reports "NOT synced - Dhan's funds read failed or returned <= 0 ..." when `sync_from_broker` returns 0; `POST /ledger/sync` answers
  `status: "not_synced"` (with a detail) instead of "synced" in that case. The `total_allocated_capital` field keeps its meaning.
- No new environment variable, column or migration (the limit is a constant, as in E183).

## Tests
`tests/test_ledger_stale_sync_e186.py` (5 tests: recent sync allowed, stale refused with the pool untouched, naive SQLite timestamp read as UTC, never-synced not blocked,
a successful sync clears the block) and one in `tests/test_main.py` (route says not_synced). Not run with pytest here (not installed); `_sync_age_s` was run directly
(never synced / aware / naive / junk / future timestamps) and passed.

## Noted, not changed
- `sync_from_broker` calls `publish_exposure(db, force=True)` every cycle, so two Oracle writes every ~13 s; the non-forced path already writes on change and every 30 s
  and still runs before the early returns. Left because `tests/test_group193_publish_exposure.py` pins the current call; say if you want it relaxed.
- The funds-failure ERROR and the peer-pnl WARNING repeat every cycle while a failure lasts (not deduped: existing tests assert them).
- `open_count == 0` resets `available_capital` to the full pool each sync, so a manual `POST /ledger/sync` landing between `reserve_capital` and the position row being
  written could return a reservation to the pool. The cycle itself is sequential, so only the manual route can hit this.
