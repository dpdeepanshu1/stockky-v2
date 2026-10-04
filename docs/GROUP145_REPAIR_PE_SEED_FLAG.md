# group145 (2026-10-04) - repair-seeded PE gets the seed flag the feed merge reads

Cumulative on group144. Application code changed in api-gateway only: `docker compose up -d --build api-gateway`.

## Seen in the logs
`repair APOLLO / HEROMOTOCO / INDIGO ...: seeded baseline PE=22.5` and `ROCE=15` (deliberate placeholders when upstreams are cold).

## Problem
`main.py` repair wrote the PE seed with the flag `pe_seed`. `data_feed.merge_feed_payload` protects real values only against
the flag `pe_ratio_seed` (the name the bulk feed uses; ROCE and sentiment already match). So a repair PE seed was not seen as
a seed: it was not protected from overwriting a real stored PE (only possible in a race, since repair seeds only a missing PE),
and the stored 22.5 was not labelled as a seed. Nothing in trading reads these flags (checked: only data_feed merge and the repair write them).

## Change
Repair also sets `pe_ratio_seed = True`; `pe_seed` is kept for anything already reading it.

## Tests
`test_main_patch_single_stock_feed.py`: cold-seed test also asserts `pe_ratio_seed`; new test that a repair-style PE seed does not
overwrite a real stored PE through `merge_feed_payload`. Real pytest: 524 passed in these two files (with test_data_feed.py).

## Correction to the remaining list
My last two lists kept item 25 open. It is closed: group 114 recorded your review (all six values kept, dates moved to 2026-10-04)
and the boot log says "all regime constants reviewed within 30d". The next review is due by 2026-11-03.
