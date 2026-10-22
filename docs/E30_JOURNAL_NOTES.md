# E30 - reason and feeling on every trade (E37, 2026-10-23)

**Notes only.** Nothing reads a note to allow, block, score or size a trade. It is your own record, shown next to the result.

| What | Detail |
| --- | --- |
| Fields | `reason` (free text, up to 300 characters, whitespace collapsed) and `emotion` (calm, confident, hopeful, unsure, impatient, fomo, fear, greed, revenge). Both optional |
| Where you add it | (1) Position Stocks -> Positions -> **Manual buy**: new *Feeling* and *Why* boxes, saved with the order. (2) Position Stocks -> Board tab -> **Trade notes**: "add note" / "edit" on any of the last 15 trades, manual or automatic. Manual trades with no note are listed first |
| What it shows | Board tab -> **Win rate by feeling**: trades, win rate and P&L per feeling, settled trades only (same rules as `/trades/history`: pending and error rows are not counted). Under 30 trades a feeling is marked "small sample". FOMO / fear / greed / revenge / impatient are shown in the warning colour as a label, not a verdict |
| Storage | new table `scalp_trade_journal` (one row per position, `create_all()` makes it, no ALTER). It keeps its own copy of symbol, scan window, status, outcome and P&L, because the 3-day retention cleanup deletes old positions: the notes and the stats outlive them. `GET /journal/notes` refreshes the copy from positions that still exist |
| Routes | `GET /journal/notes` (read), `PUT /journal/notes/{position_id}` (admin login; both fields blank removes the note), `POST /positions/manual/buy` takes optional `reason` / `emotion` |
| Switch | `JOURNAL_NOTES_ENABLED` (default on) on the Real Money Calibration page (Journal). Off: saving a note is refused (409), a manual buy ignores its reason / feeling, saved notes stay |
| Safety | a bad reason / emotion is refused (400) **before** any order is placed. If the order succeeds but the note cannot be saved, the buy is still reported as placed with `journal_saved: false` and the UI says so |

Check after the rebuild (position-stocks-service and frontend): place or pick a trade, add "FOMO" + a reason on the Board tab, reload the page (the note is still there), then `GET <position-stocks>/journal/notes` shows it under `notes` and `by_emotion` once the trade is closed.

**Not done:** notes for Real Trade (real-trade-service has its own manual engine and ledger; this is Position Stocks only), editing notes older than the 500-trade history window from the UI (the API takes any position that still exists), and any automatic warning such as "you lose after FOMO". The numbers are small-sample until you have 30 settled trades per feeling.
