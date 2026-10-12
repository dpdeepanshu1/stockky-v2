# Group 338 (2026-10-12) - Top picks cards and a long-only shortlist

Audit result: the best-3-to-5 shortlist already existed in both services (groups 332 / 335 / 337) and already drives which stock is tried first. What was missing:

| Gap found | Fix |
|---|---|
| Board tab showed a plain text list, only after an entry cycle had run (empty before 09:15, when not armed, between cycles) | New **Top picks cards** on both Board tabs, always populated once the board has ranked the session |
| Pick rows had no price, day move, zone, stop or target | Cards carry ltp, change %, sparkline, entry / stop / target / R:R, zone score, conviction, reasons, and what became of the pick |
| A candidate the Market Board ranks BEARISH could still be entered first (long-only engines, only a penalty / small nudge) | Board-bearish picks are dropped from the shortlist whenever a non-bearish one remains (never an empty shortlist because of it; upper-circuit keeps its bypass) |

## What the cards show
- **Engine picks**: the last entry cycle's shortlist (fresh for `TOP_PICKS_FRESH_S`, 15 min), enriched with the live board row, with its outcome (Entered / Skipped / First in line / Reserve / Not needed).
- **Board preview**: if fewer than `TOP_PICKS_MIN_SHOWN` (3) engine picks are fresh, the strongest bullish board rows (conviction >= `TOP_PICKS_MIN_CONVICTION` 60, priced, not held) fill the list up to `TOP_PICKS_N` (5). Labelled "Board preview": no entry gate or risk check has looked at them, and in real-trade they may not even be candidates.
- Display only. Nothing in the entry path reads the cards.

## Where
| | position-stocks-service | real-trade-service |
|---|---|---|
| Endpoint | `GET /top-picks` | `GET /top-picks/{DEMO\|REAL}` |
| Cards module | `screening/top_picks.py` | `entry_engine/top_picks.py` |
| Long-only rule | `screening/shortlist.py` | `entry_engine/shortlist.py` (+ `board_side` on each approved entry in `entry.py`) |
| Switch | `SHORTLIST_DROP_BEARISH=0` | `ENTRY_SHORTLIST_DROP_BEARISH=false` |
| Tuning | `TOP_PICKS_N`, `TOP_PICKS_MIN_SHOWN`, `TOP_PICKS_FRESH_S`, `TOP_PICKS_MIN_CONVICTION` | same names |
| Frontend | `components/board/TopPicks.tsx`, `topPicks.ts` (pure helpers + `topPicks.test.mjs`), styles in `board.css`; mounted above the shortlist panel in `PositionStocksTab` and `RealAutoTrade` |

## Not changed (known limits)
- In position-stocks only the top `max(QUALITY_GATE_TOP_N, SHORTLIST_MAX)` scan names are quality-checked, so the board re-orders those few; it does not pull in a strong board name the scanner ranked low.
- Board-derived stop = the board zone's far edge and target = entry + zone R:R x risk. These are approximations; engine picks show the engine's own stop / target.
- Nothing here guarantees profit. It only stops the engines picking against the board and makes the choice visible.

## Verification status
Run in the sandbox (no pytest / service dependencies / vite available):
- `py_compile` of every changed Python file; the two new `test_group338_top_picks.py` files (10 tests) through a minimal pytest stand-in - all pass; `topPicks.test.mjs` plus the existing board `.mjs` tests - pass; strict `tsc` of `topPicks.ts`; syntax check of the edited TSX / TS files; the card CSS rendered in Chromium.
- NOT run: the full pytest suites (`test_group332_shortlist.py` in position-stocks was edited to switch the new rule off for its penalty test), `npm run build` (needs vite and the React types). Before deploying:
  `cd services/position-stocks-service && python3 -m pytest -q tests`, `cd services/real-trade-service && python3 -m pytest -q tests`, `cd frontend && npm ci && npm run build`.
