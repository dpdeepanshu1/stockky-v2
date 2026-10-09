# Group 283 - C3 size-down from the best-5 book (position-stocks-service)

**What:** `orders/depth_gate.max_qty_from_book()` caps an entry's share count so its order value stays within
`ENTRY_BOOK_MAX_SHARE_PCT` % of ONE side of Dhan's best-5 book (`book_value_5 / 2`, from market-data `/quote`).
`orders/entry.py` applies it right after the risk-sized quantity is computed and hands the unused part of the
capital reservation back to the ledger (`release_capital`, no P&L). Minimum quantity stays 1.

**Off by default** (`ENTRY_BOOK_MAX_SHARE_PCT=0`). Needs `ENTRY_DEPTH_GATE=1` (same /quote read, cached 5 s).
Unknown depth, a timeout, a bad price or any error never shrinks an order (fails open, never raises).
Example: `ENTRY_BOOK_MAX_SHARE_PCT=10`, book Rs 4,00,000 -> one side Rs 2,00,000 -> max order Rs 20,000.

**Not in this group:** the C5 candle-VWAP switch (this zip has no `screening/intraday_candles.py`, and no group 281/282
files), 20-level depth, and per-candidate depth during screening.

**Tests:** `tests/test_group280_depth_gate.py::TestMaxQtyFromBook`, `tests/test_entry.py::TestBookSizeDownWiring`.
Sandbox had no pytest/sqlalchemy: files compile, and the sizing function was run against the real module with a stubbed
fetch. Run `bash run_tests.sh` on the VM.
