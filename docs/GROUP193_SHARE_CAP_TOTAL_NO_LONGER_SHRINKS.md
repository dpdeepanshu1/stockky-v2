# Group 193 - the 50% share-cap total no longer shrinks (real-trade-service, position-stocks-service)

Cumulative on group 192. Priority 1 of the remaining open-issue list. Rebuild real-trade-service and
position-stocks-service.

## Symptom
Almost every real-trade candidate was held at WAIT by `capital_share_cap`, with only ~10,072 of own exposure counted.

## Cause (from the code; no live logs were available)
The cap compares real-trade's exposure with 50% of (Dhan free cash + own exposure + position-stocks exposure).
1. A BUY Dhan is still filling is not a TradePosition until reconcile books the fill, but Dhan already blocks its
   cash. Free cash fell while the order was counted nowhere: total too small, exposure undercounted.
2. position-stocks published its exposure only at the end of a successful `ledger.sync_from_broker`, which runs only
   inside a cycle that passed the enabled/armed/entry-cutoff gates. A failed or <= 0 Dhan funds read, a disarmed
   service, or a service past 14:30 still holding positions published nothing. `updated_at` also moved only when the
   value changed, so a quiet service looked stale.

## Fix
- real-trade `execution/shared_exposure.py`: `get_in_flight_buy_value` (remaining qty x limit price of REAL BUY orders
  in PENDING/PLACED/PARTIAL created within `SHARE_CAP_IN_FLIGHT_MAX_AGE_MINUTES`, default 120, 0 = off; fail-open) and
  `get_other_service_exposure_age` (+ throttled warning when the peer figure is missing or older than 15 min).
- real-trade `risk_engine/engine.py`: `AccountState.in_flight_buy_value` and `other_service_exposure_age_s`; exposure =
  open + in-flight, and both are in the total. The reject message now shows free cash, open, in-flight, position-stocks
  amounts and the peer figure's age ("no published figure" when missing). Defaults of 0/None keep old behaviour.
- Wired in `entry_engine/entry.py`, `manual_engine.py` and the dry-run in `main.py` (REAL only).
- position-stocks `capital/ledger.py`: `publish_exposure()` publishes from the service's own DB; called first in
  `sync_from_broker` (so its early returns no longer skip it) and from the fast reconcile loop in `main.py` every
  ~6 s regardless of gates/market hours (change-detected, 30 s heartbeat).
- Both `publish_own_exposure` copies set `updated_at` on every publish.

## Not changed
Exits placed but not yet confirmed still hold shares and are not in the published figure (they settle in seconds).

## Tests
`real-trade-service/tests/test_group193_share_cap_total.py` (24), `position-stocks-service/tests/test_group193_publish_exposure.py` (7);
3 old ledger tests updated (a failed funds read now still publishes). Sandbox: position-stocks 2682 passed; real-trade
3359 passed, 1 skipped + the 5 that also fail on the uploaded zip (4 in test_group171, 1 in test_group172; pass in isolation).
Not live-tested: after deploy, the next `capital_share_cap` line shows the breakdown.
