# groups 304-310 (2026-10-11) - Market Board, open-price fix, shadow logging, chase guard

Cumulative on group303. Rebuild: `docker compose up -d --build position-stocks-service frontend`.
Plan: PLAN_market_board_integration.md (decisions 1.1-1.4). Everything new is display + shadow logging; nothing can change an
order unless you set `BOARD_GATE_MODE=enforce`. Not investment advice; the signals are hypotheses until the shadow study says otherwise.

## What changed
- **304 (bug fix)** `feed/md_poller.py` now passes the quote row's `open` into the day stats and `avg_price` into a new
  `ws_client.get_exchange_vwap()`. Before this, `POSITION_FEED_SOURCE=market_data` left `day_open=None`, so `opening_gate` (fails
  closed) would refuse every 09:15-10:00 entry. The default feed (`angelone_ws`) is unchanged.
- **305** `feed/bar_store.py` (1-minute bars from ticks, top-N by turnover), `screening/market_structure.py` (pure maths: strength with
  one-sided gate, rank/percentile, conviction heuristic, 5m aggregation, ATR, swings, order block / FVG / BOS zones, levels, R:R,
  chase), `feed/fno_list.py` (F&O badge/filter; Dhan FUTSTK list with built-in fallback), `feed/sector_map.py`,
  `prev_day.avg_range_pct` (35-session normal daily move), `screening/board.py`, routes `GET /board`, `/board/{symbol}`,
  `/board/status`, `/board/shadow-report` (read-only, no admin login), table `scalp_board_log` (new, additive).
- **306/307 frontend** `frontend/src/components/board/`: MarketBoard, MarketStrip, SectorRail, FlowLines (canvas), BoardTile (SVG,
  sliding tiles), treemapLayout, StockPopup (BottomSheet), ZoneChart. New "Board" sub-tab in Position Stocks. No new dependency.
- **308** "Board" tab in Real Trade: same board (data from position-stocks), highlights this service's own positions/candidates,
  display only. Chips of your positions/candidates show even if the heatmap service is down.
- **309** `screening/board_log.py` (queued writer, forward returns +15/+30/+60 min and close), `screening/board_runner.py`
  (5-minute shadow snapshots, forward fill), `orders/board_gate.py` (chase guard; `off|shadow|enforce`, fails open; called in
  `orders/entry.py` after every existing gate and before capital is reserved), `screening/board_report.py` (promotion rule).
- **310 (partial)** `JournalPanel`: P&L-by-day calendar, win rate by scan window, shadow-study summary (under the Position Stocks board).

## Config (all blank-safe; defaults in config.py)
`BOARD_ENABLED=1`, `BOARD_GATE_MODE=shadow`, `BOARD_UNIVERSE_DEFAULT=all`, `BOARD_TOP_SYMBOLS=400`, `BOARD_MIN_DAY_VOLUME=200000`,
`BOARD_ONESIDED_RATIO=2.0`, `BOARD_ZONE_TF_MIN=5`, `BOARD_ZONE_ATR_MULT=1.5`, `BOARD_CHASE_ATR=1.5`, `BOARD_LOG_EVERY_S=300`,
`BOARD_PROMOTE_MIN_OBS=30`, `BOARD_PROMOTE_MIN_SESSIONS=10`, `BOARD_COST_PCT=0.12`, `FNO_SYMBOLS_EXTRA/EXCLUDE`, `FNO_LIST_REFRESH=1`.
Rollback: `BOARD_ENABLED=0` (board routes 404, no loop, no logging, gate off); `BOARD_GATE_MODE=off` stops only the chase-guard logging.

## Promotion rule (proposed, change in config)
30+ observations and 10+ sessions on the 30-minute horizon, mean net return (after the flat `BOARD_COST_PCT`) above zero, and a
bootstrap interval of (signal - control) that excludes zero. The chase guard is inverted: promote when what it blocks does clearly worse.

## Tests
position-stocks: 3327 passed, 2 skipped (full suite). New: test_group304 (8), test_group305_market_structure, _bar_store_fno_sector,
_board_build, test_group309_board_log_gate_report, test_group309_board_runner_http (real HTTP via TestClient). One existing test
updated (`test_main.py` lifespan: the bar store adds a second tick callback). Frontend: `tsc --noEmit` and `npm run build` pass;
`node --experimental-strip-types frontend/src/components/board/treemapLayout.test.mjs` and `journalStats.test.mjs` pass.

## NOT done / not verified (be aware)
1. **Run checks A and B from the plan on the VM** (F&O underlyings in the Dhan instrument file; `open`/`avg_price` in `/quotes/bulk`).
   The F&O parser and the open-price pass-through are written to those shapes but were never run against the live data. Until A is
   confirmed the F&O badge may come from the built-in list.
2. No cold-start backfill: after a mid-session restart the bar store only has bars from restart onward; symbols whose first bar is
   >3 minutes after 09:15 and with no exchange open are left out of the ranking (counted as `no_day_open`).
3. Real Trade has no zone scoring of its own and no "over risk cap" hatching (the risk cap is adaptive server-side); plan items for 308
   beyond the display-only board were not built. Real-money gating stays off.
4. 310: command-palette symbol popup and the shared card style were not built. The popup opens on click/tap/Enter (not hover).
5. The animated board and canvas flow lines were compiled and type-checked but not viewed in a browser or measured on a phone.
6. Sector map covers well-known names (~350); everything else shows as "Other".
7. Memory: about 400 symbols x up to 375 bars is estimated at 10-15 MB; check `docker stats` and `GET /board/status` after a session.
