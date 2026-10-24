# E118: the Time Machine gate can no longer hold up a whole cycle (per-minute inline wait budget)

Before: each uncached candidate could wait up to `TM_GATE_INLINE_BUDGET_S` (2.5 s) inside the live gate. With a slow market-data feed and many candidates in one cycle, those waits added up.
Now: all entries together may wait at most `TM_GATE_WINDOW_BUDGET_S` (default 8 s) inside the gate in any rolling minute. Past that, uncached checks are queued for the background worker and get **no opinion** (the entry carries on as if the gate were off for that stock). Cache hits never spend the budget. `0` = no cap.

- `position-stocks-service/timemachine/gate_core.py` (copied to `real-trade-service`): rolling `_spent` window, `_window_open()`, counter `inline_skipped`, `/time-machine/gate` settings now show `window_budget_s` and `window_spent_s`.
- New flag `TM_GATE_WINDOW_BUDGET_S` in both `config.py`, both `control/catalog.py` (live, shipped 8, looser = neutral); the calibrator never changes it (`autocalib.py`).
- `docs/ENV_REFERENCE.md` regenerated (771 variables). Catalog and shared-copy checks pass.
- Tests added in `test_e115_time_machine_gate.py`: the cap holds for 8 slow candidates; 0 means no cap and cache hits spend nothing.

Checked here: pytest is not installed in this sandbox, so I ran the files with a small stand-in runner. Position Stocks: E115 gate 27/27, time_machine 35/35, catalog coverage 3/3, catalog-vs-config 1/1. Real Trade: E115 gate 8/8, catalog 3/3, 477 1/1. Mutation check done: with the cap switched off, the new cap test fails (8 candidates waited 2.46 s). Run the real pytest suites on the VM once (`stockky_full_test.sh`).
