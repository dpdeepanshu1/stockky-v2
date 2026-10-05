# group151 (2026-10-05) - after-hours scan no longer picks ordinary words (IT, OIL, ACE) as stocks

Cumulative on group150. Rebuild real-trade-service: `docker compose build real-trade-service && docker compose up -d`.

## Cause (VM log, 2026-10-05)
The after-hours scan kept `['LENSKART','BPCL','CLEANMAX','WIPRO','OIL','TCS','IT','ACE']`. `IT` is the sector ("IT stocks rally"), `OIL` is "oil prices", `ACE` is a word. All three are real tickers, so the known-universe path of `_extract_symbol` accepted them: it matched ANY token found in the symbol master, ignored `_STOPWORDS` (only the degraded no-master path used it), and returned the FIRST matching token, so "IT stocks: TCS jumps" resolved to IT, not TCS. The market-hours news loop already had a generic-token filter (`_GENERIC_TOKENS`); the after-hours scan, which feeds the next-day watchlist, did not.

## Fix (watchlist_engine/afterhours_scan.py)
- `_GENERIC_WORD_TICKERS` (IT, ENERGY, GOLD, BANK, INDIA, NIFTY, ... same list the intraday loop uses): skipped, and the scan moves on to the next token in the headline.
- `_NAME_ONLY_TICKERS = {OIL, ACE}`: real companies (Oil India, Action Construction Equipment) whose ticker is a word. The bare token never matches; "Oil India" / "Action Construction" in `_NAME_ALIASES` still do. Aliases are still gated by the symbol universe.
- Degraded path (no symbol master) unchanged.

## Tests
`tests/test_afterhours_extract_symbol_word_tickers.py` (13 tests); 5 of them fail on the old logic. real-trade suite: 2733 passed, 1 skipped (3 modules excluded, they do not import in my sandbox: fastapi version mismatch).

## Today's rows are already stored
The 2026-10-05 scan already wrote OIL, IT and ACE to the next-day watchlist. The new code does not delete them. If they are unconsumed and you want them gone, delete those REAL rows for `2026-10-05` from `trade_nextday_watchlist` (check the column names first: `SELECT * FROM trade_nextday_watchlist WHERE symbol IN ('OIL','IT','ACE')`).

## Limits
Only these words are blocked. Another ticker that doubles as a word would still match until it is added to one of the two sets, so extend them when a new false symbol shows up in a scan log.

## Duplicate AngelOne feed thread: checked, no bug
The 250- then 491-symbol "background thread started" lines are the universe refresh. `main.py` calls `stop_feed_background()` (joins the thread) before `start_feed_background()`, which also has a generation counter so a superseded thread exits. No "did not stop" warning appears in any log. No change.
