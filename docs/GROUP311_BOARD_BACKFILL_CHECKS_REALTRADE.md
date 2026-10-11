# group 311 (2026-10-11) - restart backfill, live checks A/B, sector map, Real Trade hatching, palette popup, board width fix

Cumulative on groups 304-310. Rebuild: `docker compose up -d --build position-stocks-service frontend`.
Display + shadow logging only; nothing here can change an order (`BOARD_GATE_MODE` is untouched). Not investment advice.

## What changed
- **Bug fix (frontend)** `MarketBoard.useWidth`: the measured element only exists after data loads, so the old mount-time effect never
  observed it and the width stayed 0 - the treemaps would not have drawn. Now a callback ref. Found by rendering in headless Chromium.
- **Restart backfill** `feed/bar_backfill.py` + `bar_store.insert_history()`: symbols whose first bar starts >3 min after 09:15 are queued
  (top `BOARD_BACKFILL_TOP_N` by turnover); the board runner fetches today's 1-minute candles from market-data `/history/{sym}?period=1d&interval=1m`
  (`BOARD_BACKFILL_PER_CYCLE` per pass, 6 s apart while a queue exists, retry no sooner than `BOARD_BACKFILL_RETRY_S`) and merges only
  minutes strictly earlier than the first live bar. The day open then comes from the first candle (a PROXY; the exchange open still wins).
  Config: `BOARD_BACKFILL_ENABLED/TOP_N/PER_CYCLE/TIMEOUT_S/RETRY_S`. `GET /board/status` -> `backfill`; strip shows "filling morning bars".
- **Checks A and B, runnable** `GET /board/self-check` and `python3 scripts/check_board_data.py [url]` (default `http://localhost:8006`).
  A: Dhan instrument file layout / count / name sanity (`fno_list.parse_report` now reads the compact AND the detailed layout - the
  detailed column names are from memory, unverified). B: `open` / `avg_price` present and consistent in `/quotes/bulk` (run 09:20-15:20 IST).
- **Sector map** about 900 names across 28 sectors (was about 350). `GET /board/status` -> `sector_coverage.top_other` lists the most-traded
  symbols still "Other"; add them with `BOARD_SECTOR_EXTRA="SYM=Sector"`.
- **Real Trade board** rows carry `zone_score` (0-100 heuristic), `invalidation`, `risk_per_share`. Hatched tile = not even one share fits
  equity x risk-per-trade % (or cash); stop = the candidate's own proposed stop, else the zone's far edge (approximate, labelled in the
  popup). Mirrors `risk_engine` checks 5 / 5b; display only. `components/board/riskCap.ts`.
- **Shared card style** `components/board/boardCard.tsx` (graphite / slate / rounded-2xl, like StatCard) on strip, sector rail, journal, popup.
- **Command palette** type a symbol -> "SYMBOL - zones, levels, R:R" opens the board popup (rank fields show "-" when it is not ranked).

## Tests / verification (sandbox)
New `tests/test_group311_board_backfill_selfcheck.py` (24). Run under a small pytest stand-in with stubbed websockets / httpx / pyotp:
24 new + 51 existing board tests pass; `test_fill_forward_real_sqlite` needs sqlalchemy and was NOT run here. Frontend: `tsc --noEmit`,
`vite build`, riskCap / treemapLayout / journalStats Node tests pass; the board rendered in headless Chromium at 390 px (180 tiles, 18 hatched,
no horizontal overflow, no console errors) against mocked data. Frame time on a real phone and the full suite were not measured.

## NOT done / not verified
1. Run the real suite on the VM: `cd services/position-stocks-service && python -m pytest -q`.
2. Run `scripts/check_board_data.py` on the VM (needs live network; B only while the market is open). F&O badge stays on the built-in list until A is ok.
3. Backfill depends on market-data `/history` returning 1-minute candles for period=1d; untested live.
4. Hover card on tiles not built (popup opens on click / tap / Enter / palette).
5. Real Trade still has no zone scoring of its own; the score comes from the position-stocks board.
