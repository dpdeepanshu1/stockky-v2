# group101 (2026-10-04) - remaining log-audit items: 7, 10, 11, 23, 28 (plus 19 reviewed)

Cumulative on group100. Rebuild what changed: `docker compose build api-gateway market-data-service analysis-intelligence-service decision-prediction-service && docker compose up -d`.

## Item 7 - Render-era wake pings still sent on the always-on VM
Rule (same as api-gateway/technical): pings are off when `ORACLE_DSN` is set, `WAKE_PINGS=1/0` overrides. Now also gated:
- api-gateway `market_history` (`/health?warm=true` before every history call) and `/ops/keepalive?deep=true` (returns `{"skipped": true}` with no upstream calls).
- decision service: `/health?warm=true` no longer pings its four downstreams; the technical-fallback warm ping is gated.
- prediction (`/history` fetch) and training (chart history) warm pings are gated; each service got its own `_wake_pings_enabled()` copy.
- Not changed: `/ops/neon-keepalive` (a DB keep-alive, not a dyno wake) and `/training/health` (a health probe). The compose file is untouched: if `ORACLE_DSN` is empty on your VM, set `WAKE_PINGS=0` for those containers.
- Tests: `decision/tests/test_wake_pings_gated.py` (AST guard that every `params={"warm": ...}` call sits under the gate in decision, prediction and training; helper rule; no downstream call on `health(warm=True)`), plus 3 in `api-gateway/tests/test_wake_pings_and_movers_durable.py`.

## Item 23 - NSE quote-equity 403 for the two remaining callers (market-data-service)
`_waterfall_nse_direct_price` and `_fetch_nse_fundamentals` now check and feed the group96 pause (`bhavcopy.nse_quote_blocked` / `_note_quote_status`) through two tolerant wrappers in `main.py`. One 401/403/429 steps all three quote-equity callers aside for `NSE_QUOTE_BLOCK_SECONDS` (0 = off). If bhavcopy cannot supply the helpers the call proceeds as before. The datacenter-IP 403 itself is not fixed. Tests: `tests/test_nse_quote_pause_shared.py` (7).

## Items 11 + 28 - polluted and dead entries in `stockky:searched_symbols`
`_load_searched()` now cleans on read: normalises spelling, drops non-strings/blank/index/delisted/non-equity entries (`_filter_equities`), maps curated one-to-one aliases (HEROMOTORS -> HEROMOTOCO), de-duplicates, and writes the cleaned list back once when it differs (cap 200). A failed write-back is non-fatal. AAKASH and ANNAPURNA leave the stored list the next time it is read. Not changed: the saved watchlist (your own list; Hot Picks already skips dead symbols), and misspellings that have no alias entry (they stay until they age out, as before). Tests: `tests/test_searched_list_selfclean.py` (8).

## Item 10 - no sector/industry in the payload
A fundamentals payload with no non-blank `industry`/`sector`/`sectorDisp` string used to be compared with the generic list (RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK). It now compares against no peers: `rank_against_peers` reports sector `UNKNOWN` and "No peers compared in UNKNOWN", `compute_peer_relative` returns a neutral 50. Explicit peer lists, known sectors and a present-but-unrecognised sector (`Basic Materials` -> DEFAULT list) are unchanged. Two existing tests used an empty payload and relied on the generic list; they now carry a sector. Tests: `tests/test_peer_no_sector_no_generic_peers.py` (12 cases).

## Item 19 - other Telegram senders: reviewed, no change
The remaining senders are already bounded: the gate-off alert has a 30-minute per-mode cooldown, eDIS alerts fire at most once per morning per mode, the TOTP messages are per-refresh events, and the notifier drops identical text for 5 minutes. I did not add throttles I could not justify from your log.

## Not done (unchanged from group100)
Items 6 (environment), 9 remainder (news sources need a decision), 25 (needs your review of the six regime constants), 12 (other routes), 24 (other drift paths), 27 (premarket baselines without `symbols`), 20 (Movers panel until one market session), 4 (`analyze()` bare symbol), 29 remainder (armed-but-logged-out pollers' server gates), 30 (`SESSION_SECRET` shared on purpose).

## Tests run here
api-gateway 8101 passed (8090 + 11 new); analysis-intelligence 2161; market-data 684; decision 38; prediction 20; training 38.
