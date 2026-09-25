# Session 112, round 17 (2026-09-25): `feed/` coverage — scrip_master + ws_client + angelone_session (position-stocks-service)

Next items off round 13/14's priority list, now that `execution/dhan_client.py`
is closed out (round 16). All three `feed/` modules were the largest remaining
gap under position-stocks-service: low single-digit coverage, no test file for
scrip_master or angelone_session, and ws_client only covered by the narrow
session99 secret-redaction regression.

## What was added

### `tests/test_scrip_master.py` (new)

Covers `feed/scrip_master.py` (22% → target ~90%+).

scrip_master is module-state-heavy: all tests call `_reset()` at setup to
clear `_token_map`, `_loaded_at`, `_fail_count`, `_next_retry_at`, and
`_disk_tried` so tests don't bleed into each other.

Pure helpers tested directly (no mocking):
  * `_clean`: uppercasing, `.NS`/`.BO` stripping, None handling
  * `_iter_json_array`: single-chunk/chunked/BOM/empty/non-array raises/
    oversized-pending-buffer raises
  * `_rows_to_map`: NSE-EQ filter, BSE exclusion, empty token exclusion,
    non-dict row skip, `-EQ` suffix strip, token coerced to str

Stateful helpers (monkeypatched disk or `_fetch_map`):
  * `_save_disk` + `_warm_from_disk`: full round-trip, stale-cache ignored,
    missing file, corrupt JSON, bad path non-raise, second warm call is no-op,
    existing map not overwritten
  * `_record_failure`: backoff set, backoff doubles, backoff capped at max
  * `_is_stale`: true when empty, false when fresh, true when old
  * `_refresh_locked`: success swaps map, exception records failure, empty
    result records failure, skips when not stale, force flag bypasses, skips
    during backoff
  * `ensure_loaded`: cold start blocks and populates, fresh map returns
    immediately, backoff skips, stale map triggers background refresh,
    warm-from-disk seeded on cold start
  * `_load_sync`: forces refresh ignoring staleness

Public API:
  * `get_token`: found, lowercased, `.NS` suffix, not found → None
  * `get_tokens_bulk`: resolves present, drops missing, handles `.NS`, empty input
  * `get_all_nse_eq`: returns copy, mutating copy doesn't affect module state
  * `status`: all fields, empty state, failure count

### `tests/test_ws_client.py` (new)

Covers `feed/ws_client.py` beyond the existing
`test_ws_client_secret_redaction.py` (which stays untouched).

Pure frame helpers:
  * `_parse_best5`: bid+ask found, ask-only, zero-price skip, only first
    level used, too-short/empty data → None/None
  * `_parse_frame`: mode-3 full frame, LTP paise division (including Wockhardt
    precision), too-short → None, zero/negative LTP → None, negative volume
    clamped to 0, mode-1 frame (no volume/depth), depth extracted in full
    frame, token stripped of null bytes, empty bytes → None

Subscribe + map:
  * `_build_subscribe_msg`: structure, unsubscribe action=0
  * `_build_reverse_map`: token→symbol inversion

Accessors (seeded directly on module state):
  * `get_tick_buffer`: empty, returns list copy (mutating copy doesn't affect
    deque)
  * `get_last_ltp`: None when empty, returns last tick
  * `get_last_volume`: 0 when absent, returns stored
  * `get_best_bid_ask`: None when absent, returns (bid, ask) pair
  * `register_on_tick`: callback receives (symbol, ltp, volume, ts)

Time-bounded buffer pruning (`_MAX_BUFFER_AGE_S` fix):
  * Old tick pruned on next append
  * Recent tick (1 min old) kept
  * Confirms `_MAX_BUFFER_AGE_S == 65 * 60` and `_MAX_TICKS == 50_000`

`ws_status`: all 6 fields (running, connected, subscribed_symbols, task_done,
reconnect_attempts, last_tick_at), with and without asyncio.Task.

`_redact_secrets` extra edge cases (non-string, None, multi-param URL,
no-secrets unchanged).

No asyncio WS network calls are made — all pure logic and state mutation.

### `tests/test_angelone_session.py` (new)

Covers `feed/angelone_session.py` (41% → target ~90%+).

  * `AngelOneSession.is_configured()`: all-set → True; each of the 4 fields
    missing → False
  * `_get_outbound_ip`: success path, failure → None
  * `_resolve_client_public_ip`: static IP returned directly, cache hit, cache
    miss triggers fetch, fetch failure → 127.0.0.1 fallback, expired cache
    refetched
  * `_login`: successful login sets token/feed_token/expiry; status=false raises
    RuntimeError with message; not configured raises; expiry is ~20h from now;
    correct headers (X-PrivateKey, X-ClientPublicIP) and URL
  * `ensure_session`: no token → triggers login; fresh token → skips; expired
    token → re-login; concurrent calls via asyncio.gather → only one login fires
    (lock serialisation)
  * `get_session()`: returns AngelOneSession singleton, same object each call

All network calls (httpx.AsyncClient.post, httpx.get) fully monkeypatched.

## Verification

Neither `httpx`, `pyotp`, `websockets`, nor any other dependency is installed in
this sandbox. The three test files were written by tracing each source file
line-by-line:
  * All pure-logic paths (JSON array parser, rows-to-map filter, frame byte
    offsets, best5 flag swap, buffer pruning math, backoff doubling) were
    hand-verified by extracting the function bodies and checking the arithmetic.
  * SDK/network-facing tests are traced against the actual source signatures —
    if anything fails to collect it's most likely an AsyncMock/patch target
    mismatch, easy to spot from the failure.

The `_iter_json_array` oversized-pending test directly sets `_MAX_PENDING_CHARS`
to 5, exercises the ValueError path, then restores the original value — same
pattern round 16's pure-math verifications used for tick-size band boundaries.

## Next by priority (unchanged from round 13/14's list, minus closed items)

`main.py` (22%, 2029 lines — the largest remaining file by statement count).
