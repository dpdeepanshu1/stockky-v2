# Group 286 - Dhan 20-level depth (market-data) and size-down from it (position-stocks)

## What
1. **market-data `dhan_data/depth20.py`** (opt-in, `DHAN_DEPTH20_ENABLED=1`, default off): Dhan's 20-level market-depth
   websocket (`wss://depth-api-feed.dhan.co/twentydepth`), **on demand**. Nothing is subscribed until somebody asks;
   a symbol nobody asks about for `DHAN_DEPTH20_TTL_S` (120 s) is unsubscribed; the connection closes after
   `DHAN_DEPTH20_IDLE_CLOSE_S` with nothing watched. At most 50 symbols (Dhan's limit); least recently asked is dropped first.
   NSE equity only (indices are refused by Dhan for full depth).
2. **`GET /depth/{symbol}?qty=&slip_pct=&wait_s=`** (market-data): always HTTP 200 with `available`; when false, `reason` is one of
   `depth20_disabled`, `dhan_disabled`, `dhan_unavailable`, `unsupported`, `warming`, `stale`, `error:<Type>`.
   Fields: best_bid/ask, spread_pct, bid/ask qty and value over 20 levels, imbalance, how far level 20 sits from the touch;
   with `qty`: average price and impact % of buying/selling that many shares (`buy_complete` false if the 20 levels hold fewer);
   with `slip_pct`: `buy_qty_within_slip` / `sell_qty_within_slip`. Also in `GET /internal/dhan-status` under `depth20`.
3. **position-stocks `orders/depth_gate.max_qty_from_depth20()`**, wired in `orders/entry.py` after the best-5 cap (group 283):
   cap = `ENTRY_DEPTH20_MAX_SHARE_PCT` % (50) of the ask shares within `ENTRY_DEPTH20_SLIP_PCT` % of the best ask.
   **Off by default** (`ENTRY_DEPTH20_SLIP_PCT=0`). Minimum 1 share. The unused part of the capital reservation goes back to the
   ledger (same path as group 283). The second cap sees the quantity left after the first.
   Fails open on everything: depth off, warming, stale, unsupported symbol, timeout, bad body, any error -> no shrink.
   A first ask for a symbol may wait up to `ENTRY_DEPTH20_WAIT_S` (1 s) for its book.

## Not verified live (read this before turning it on)
The packet layout comes from Dhan's public docs (12-byte header: int16 length, u8 code, u8 segment, int32 security id,
u32 sequence; 20 x float64 price, uint32 qty, uint32 orders = 332 bytes; code 41 bid, 51 ask; little-endian assumed;
disconnect code 50 with int16 reason). Tested with `struct.pack`, **not** against a real frame. First live session:
set `DHAN_DEPTH20_ENABLED=1` in market-data, call `/depth/<a liquid symbol>?qty=100&slip_pct=0.3`, then read
`/internal/dhan-status` -> `depth20`: `bid_packets`/`ask_packets` should climb, `bad_packets` and `unknown_codes` stay empty.
Compare `best_bid`/`best_ask` with `/quote`. Only then set `ENTRY_DEPTH20_SLIP_PCT` (e.g. 0.3) in position-stocks.

## Tests
market-data `tests/test_group286_depth20.py` (44: parsing, stacked/truncated messages, book maths, watch/LRU/expiry, public answer),
`tests/test_group286_depth20_endpoint.py`; position-stocks `tests/test_group286_depth20_gate.py`,
`tests/test_entry.py::TestDepth20SizeDownWiring`. Env variables are documented in `.env.example` and `.env.oracle.recommended`
(`ENTRY_BOOK_MAX_SHARE_PCT` from group 283 was missing from `.env.example` and is added).

Results here: position-stocks 3077 passed, 1 failed (`test_dhan_client::TestGetSdkClient::test_valid_creds_returns_client`,
`dhanhq` SDK not installed in this sandbox; fails the same on the group 285 zip). market-data: the same 16 failures as the
group 285 zip (sandbox dependencies), nothing new.
