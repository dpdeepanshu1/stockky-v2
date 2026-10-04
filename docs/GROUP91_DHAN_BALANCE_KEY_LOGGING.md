# group91 (2026-10-04) - item 31: Dhan balance field "verify" warning (ledger + equity_sync)

Cumulative on group90. Run `python -m pytest tests -q` in `position-stocks-service` and `real-trade-service`, then `docker compose build position-stocks-service real-trade-service && docker compose up -d`.

## What item 31 was
`position-stocks-service/capital/ledger.py::sync_from_broker` sizes the scalp pool from Dhan's funds response and logged a WARNING on every boot: "using Dhan balance field 'availabelBalance' ... verify this reflects a sensible current tradeable balance". real-trade-service's `execution/equity_sync.py` had the same message.

## Finding
Dhan's fundlimit documentation (dhanhq.co/docs/v2/funds) describes `availabelBalance` (the misspelling is Dhan's) as "Available amount to trade", `sodLimit` as the start-of-day amount, and `withdrawableBalance` as the amount available to withdraw to a bank. So the field these services use first IS the documented tradeable balance and the "verify" warning was stale for it. The fallback keys are the real risk: they measure other things, and `availableCash` is not in the documented response at all.

## Fix (both services, same wording)
- Documented keys (`availabelBalance`, `availableBalance`): INFO once on first use or when switching between them or back from a fallback. No warning.
- Fallback keys (`availableCash`, `withdrawableBalance`, `sodLimit`): WARNING when first used or switched to, naming the key, the previous key if any, and what it is (`sodLimit` ignores intraday usage and overstates after spending or losses; `withdrawableBalance` can sit below the tradeable balance; `availableCash` is undocumented).
- Sizing behaviour is unchanged: the key order and the fallback chain are the same, so a response that only has a fallback key still sizes the pool, now with a clearer warning. I did not remove the fallbacks because session53's live evidence showed this account's response populating a key outside the original two.
- ledger only: a field that is present but reports 0 or less now logs "Dhan field 'X' reports N (<= 0) - nothing to allocate, ledger left as it was" instead of "no usable available-balance field". Return value (0.0) and the no-update behaviour are unchanged, and a zero reading no longer counts as the "key in use".

## Not changed / not verified
- I have not seen a live funds response from your account, so I do not know which key it populates. After deploy the log tells you: an INFO line means the documented field, a "FALLBACK" WARNING means it does not.
- A genuine zero balance still leaves the ledger as it was (existing behaviour).

## Tests
- position-stocks-service `tests/test_ledger_coverage.py`: the two old tests that pinned the "using Dhan balance field" WARNING were replaced; new cases for documented-key INFO, spelling switch, three fallback keys, previous-key naming, fallback-to-primary, and the zero-balance message.
- real-trade-service `tests/test_equity_sync_remaining_coverage.py`: same idea, plus an unknown-key note case.
- The new tests fail against the old wording (6 of 12 and 6 of 88 in the two files, checked by emulating it) and pass now. Real pytest in clean venvs: position-stocks-service 2412 passed, real-trade-service 2922 passed + 1 skipped.
