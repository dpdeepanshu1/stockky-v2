# Group 358 - PREPARE-to-Buy refresh reasons (sniper side, log-only)

`POST /data-feed/refresh-prepare-to-buy` only re-quotes the PREPARE band (58-68) and patches the stored price. It never said WHY a refreshed row deserves another look. This group adds that, **log-only**: nothing removes, re-ranks or re-prices a row.

The previous session was cut off mid-change and none of it was in the group 357 zip, so everything here was rebuilt from its design and re-tested.

## api-gateway
- `buy_sniper.py`
  - `prepare_refresh_note(row, live_price, old_price)`: runs the group 357 `structure_verdict` (shadow) at the fresh price. Keys: `opinion` (`ok` | `no_opinion` | `off`), `turned_bad`, `flipped` (fails now but passed at the previously stored price), `reasons`, `triggers`, `headline`, `no_data`, `live`. A blocker headline wins over a trigger headline; triggers are still kept.
  - `prepare_triggers(summary, price)`: P9 "structure events near the bar" (fresh bullish BOS <= 3 bars and not failed, bullish CHOCH <= 5, bullish sweep <= 3, price inside a live bullish FVG / order block, golden zone). It mirrors `decision-prediction-service/decision/structure_policy.prepare_triggers`; a 4000-case randomised test pins the two together (skipped if the decision service is not in the checkout).
  - `prepare_refresh_report(rows, live_prices, old_prices)`: batch counts `{checked, no_opinion, turned_bad, flipped, with_triggers}` and `notes` (only rows with something to say).
  - **No summary on the feed row = no opinion.** The refresh never fetches structure (it exists to avoid a request storm). Stale / insufficient / split-affected structure = no opinion, never a trigger.
- `main.py` `refresh_prepare_to_buy`: snapshots the stored rows and prices BEFORE patching (a store may return the dict that `patch_feed_price` mutates), records fresh quotes, and adds `structure_mode`, `structure_notes`, `structure_counts` to the reply and an INFO log line per flagged symbol (max 20). Wrapped in try/except: a failure costs only the notes. Early "no candidates" reply and the `message` text are unchanged.

## Frontend
- `structureView.ts`: `prepareNoteLines(notes, max)` (flips first, then turned-bad, then events; garbage -> []). Node test added.
- `ScanPanel.tsx`: after "Refresh Prepare-to-Buy", the notes are listed under the status line (amber = structure now blocks entry, green = structure event).

## Config
`SNIPER_PREPARE_STRUCTURE_MODE` = `off` | `log` (default; unknown value -> log). Independent of `SNIPER_STRUCTURE_MODE`. In `.env.example`; `docs/ENV_REFERENCE.md` regenerated.

## Tests
`tests/test_group358_prepare_structure_notes.py` (20): note / flip logic, each trigger and its negatives, no-opinion cases, off / bad mode, mode independence, report counts, garbage input, the P9 parity test, and the route lifted out of `main.py` by AST (refresh unchanged, notes + logging, off mode, broken notes path, quote failures, no candidates).

## Not done / next
Notes only exist for feed rows that already carry `ms_*` keys. Seeding them is the post-close `structure_only` refresh job (open item 2).
