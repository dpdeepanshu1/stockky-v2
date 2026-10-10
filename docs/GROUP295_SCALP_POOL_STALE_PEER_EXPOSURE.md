# Group 295 - the scalp pool stops crediting a stale real-trade-service exposure figure (position-stocks-service)

Follow-up to group 181. Listed there under "Things to know": "if that service is down the last value stays". The credit
(`share% * real-trade-service's open-position value`) was added with no age check, so a figure real-trade-service had not
refreshed for hours (service down, positions closed since) kept inflating the pool's risk-sizing baseline, bounded only by the
free-cash cap.

## What changed
- `capital/shared_exposure.py`: `get_other_service_exposure_age(db)` - seconds since real-trade-service last published, None when
  there is no row / no timestamp / a DB error. Never raises. (real-trade-service already writes an explicit heartbeat on every
  equity sync, so an unchanged figure is not mistaken for a stale one.)
- `capital/ledger.py`: in `sync_from_broker`, a peer figure older than `SCALP_POOL_PEER_EXPOSURE_MAX_AGE_S` (default **900**, the
  same "stale" mark real-trade-service uses for this table; `0` = never stale; blank/unreadable/negative = 900) is not credited.
  One WARNING per 5-minute age bucket says so. An age that cannot be read keeps the credit as before (fail-open).
- `.env.oracle.example`: the new setting, commented, next to the group 181 line.
- `SCALP_POOL_CREDIT_PEER_EXPOSURE=0` still switches the whole credit off.

## Not changed
- This is the "figure is old" case only. I did not add a limit on how fast the credit may rise between two fresh syncs: the
  "jump" you mentioned in the open list needs the actual numbers (trade_positions / holdings_sync_reconcile rows and the
  published exposure at the time), and I would be guessing a threshold.

## Tests
`tests/test_group295_peer_exposure_staleness.py` (18 new): fresh credited, stale not, exact-limit boundary, configurable limit, `0`
off, blank-safe parsing, naive timestamp read as UTC, unreadable age keeps the credit, no row, age read never raises, credit switch
wins, one warning per bucket, a refreshed figure is credited again. 9 mutations on the new logic, all caught (one needed a boundary
test added first).

position-stocks full suite: **3198 passed** (3180 + 18). real-trade-service is unchanged in this group. Rebuild position-stocks-service.
