# A11: account-wide daily loss cap (both engines)

**Problem.** real-trade-service and position-stocks-service trade one Dhan account (about 50/50). Each stopped its own new BUYs off its own loss against its own capital (real-trade 3 % of its equity, position-stocks 4 % of its pool). Neither looked at the other, so a bad day on both sides could lose more of the account than either cap intended while both engines still read "within my limit".

**Change.** One extra layer. Each service publishes its realized P&L for the IST day and its capital base to the new table `stockky_shared_daily_pnl` (`models.SharedDailyPnl`, created by `create_all`, no ALTER). When the combined net loss (own + peer) / (own base + peer base) reaches `ACCOUNT_MAX_DAILY_LOSS_PCT` (default 3.0, 0 = off), new BUYs are blocked in both engines.

- real-trade: `risk_engine/engine.py` check 3a, check name `account_daily_loss_limit`, verdict `BLOCKED_GLOBAL`. Wired into auto entries, manual BUYs and the dry-run endpoint. Published from `execution/equity_sync.py`.
- position-stocks: `capital/ledger.py::reserve_capital()` refuses the entry (both entry paths go through it). Published from `release_capital()` (on every realized P&L change) and the `publish_exposure()` heartbeat.
- Exits are never blocked. The per-service caps and kill switches are unchanged and still report first.
- Own figures are always live; only the peer's row is read from the table. A peer row from an earlier IST day is ignored. A missing or unreadable peer row makes the sum equal to this service's own figures (same as its own cap). Every DB error is swallowed (fail-open), same convention as `shared_exposure.py`.
- Profit on one side offsets loss on the other (it is an account-level number).

**Honest limit.** With today's caps (3 % of real-trade's half, 4 % of the pool) the worst case when both trip is already about 3.5 % of the account, so a 3 % account cap only bites on mixed days (for example real-trade at -2.5 % of its half and the pool at -3.8 % of its pool is about 3.15 % of the account). Lower `ACCOUNT_MAX_DAILY_LOSS_PCT` if you want it to bind earlier. The peer's figure lags by the peer's publish cadence (real-trade: each equity sync; position-stocks: each realized exit plus a 30 s heartbeat).

**Env.** `ACCOUNT_MAX_DAILY_LOSS_PCT` (same name read by both services; set it once). `0` turns the layer off with no code change.

**Tests.** `services/real-trade-service/tests/test_a11_account_daily_loss.py`, `services/position-stocks-service/tests/test_a11_account_daily_loss.py`.
