# Group 333 - position-stocks VWAP gate (2026-10-12)

**What:** `services/position-stocks-service/orders/vwap_gate.py`, called in `orders/entry.py::attempt_entry` right after the circuit and pre-open gates and before capital is reserved. A skipped candidate is logged as `SKIPPED` with a `VWAP_BELOW:` / `VWAP_EXTENDED:` reason.

**Rules**
- price < VWAP x (1 - ENTRY_VWAP_TOLERANCE_PCT/100) -> `VWAP_BELOW` (default 0.15%).
- ENTRY_VWAP_MAX_EXTENSION_PCT > 0 and price > VWAP x (1 + ext/100) -> `VWAP_EXTENDED` (default 0 = off).
- No opinion before ENTRY_VWAP_MIN_TIME_IST (09:45), when the quote is older than the depth gate's age limit, when avg_price is missing / <= 0 / NaN, or when the VWAP is more than 15% from the price (a wrong value, not an average).
- Never raises; off with ENTRY_VWAP_GATE_ENABLED=0.

**Data:** the market-data `/quote` row's `avg_price` (Dhan `average_price`), read through `depth_gate._fresh_quote()` - the same cached body as the depth and circuit gates, so no extra network call.

**Not verified:** whether Dhan returns `average_price` for every symbol during market hours could not be checked offline. A symbol without it is simply not judged. Watch the candidate log for `VWAP_BELOW` rows on Monday.

**Tests:** `tests/test_group333_vwap_gate.py`. conftest switches the gate off for older suites (it reads the clock).

**Still open from the group 332 request list:** real NSE pre-open board, liquidity floor, catalyst check, expiry/Friday/pre-holiday rules, window auto-disable + width tuning.
