# Stockky — Candidate Delivery-Gate, Time-Stop Label & Portfolio-Risk Fixes (2026-09-01)

5 files changed, syntax-checked with `python3 -m py_compile` (0 errors) —
the entire `services/real-trade-service` tree recompiled clean after all
edits in this round. Continues the same-day audit into
`candidate_engine/candidates.py`, `exit_engine/exit.py`,
`entry_engine/entry.py`, `main.py`, `manual_engine.py`, `portfolio/
portfolio.py`, and `execution/auto_pilot.py` (flagged as not-yet-reviewed
in the prior round's summary).

## Issue 1 — `_volume_shock_analysis`'s delivery-% gate never actually ran
**File:** `services/real-trade-service/candidate_engine/candidates.py`

The module's own 2026-09-01 re-backtest note documents a real finding: for
base-tier volume-shock candidates, requiring `delivery_pct >= 30%` (when
known) lifts win rate 44.8%→45.7% and mean return +0.27%→+0.41%. The code
implementing that gate read `quote.get("delivery_pct") or
quote.get("deliv_per") or 0` — but `quote` here is `_fetch_quote()`'s result,
which comes from market-data-service's `/quote/{symbol}` and `/quotes/bulk`
endpoints. Both are built by `_pad_quote_response()`, whose fixed output
schema (`symbol/name/price/cmp/previous_close/day_change_pct/day_high/
day_low/volume/atr/market_cap/pe_ratio/source/fetched_at`) never includes
either key. Delivery data is served from a completely separate endpoint
(`GET /delivery/{symbol}`, `bhavcopy.get_delivery`) that was never called
from here.

Net effect: `delivery_pct` was silently always `0` in this function.
`high_delivery` was always `None`, the payload's `delivery_pct`/
`high_delivery` fields sent to the frontend were always `None` even when
real NSE delivery data existed for the symbol, and the entire
`BASE_TIER_MIN_DELIVERY_PCT` reject branch was dead code — it could never
fire, since the branch's own guard (`delivery_pct > 0`) was never true.

- Added `_fetch_delivery()`, calling `GET {MARKET_DATA_URL}/delivery/{symbol}`
  (20s timeout, best-effort — same fail-open shape as the rest of this
  file's fetch helpers).
- `_volume_shock_analysis()` now calls it, but **only for base-tier
  candidates** (`not upper_circuit and not high_conviction`) — those two
  tiers never consulted `delivery_pct` for their reject decision, so there
  is no reason to pay for the extra HTTP round-trip (server-side Redis
  cached, but can still be a cold NSE/bhavcopy fetch) on them.
- `get_delivery()`'s neutral fallback (`delivery_pct=50.0,
  source="fallback_neutral"`, used when no real data exists for the symbol)
  is now explicitly excluded from counting as "known" — previously any real
  50.0+ reading would have passed the `>= 30%` bar anyway, but treating the
  neutral placeholder as real data would have silently masked the
  missing-data case rather than correctly reporting it as unavailable
  (`delivery_pct: None` in the payload, `high_delivery: None`).

## Issue 2 — Time-stop exits logged as `EMERGENCY_EXIT` in the audit trail
**File:** `services/real-trade-service/exit_engine/exit.py` (`evaluate_mode`)

Two separate exit paths both called
`_write_exit_decision(db, position, "EMERGENCY_EXIT", reasoning, ltp)`:

1. The genuine gap-down emergency exit (§0 in the function) — correct, this
   is what the label is for.
2. The time-stop exit (§3) — a copy-paste of the same action string. The
   `reasoning` text correctly says `"Time-stop: held N days..."`, and the
   tally dict already tracks `time_stops` and `emergency_exits` as separate
   counters, but the row written to `TradeExitDecision.action` (and
   therefore what the dashboard/audit trail displays for that decision) said
   `EMERGENCY_EXIT` regardless — the two exit reasons were indistinguishable
   in the persisted decision history even though the code internally knew
   the difference.

- Time-stop closes now log `action="FULL_EXIT"` — consistent with
  `models.py`'s documented action taxonomy for `TradeExitDecision.action`
  (`"WAIT" | "ENTER" | "HOLD" | "TRAIL_STOP" | "PARTIAL_EXIT" | "FULL_EXIT" |
  "EMERGENCY_EXIT"`, which has no separate time-stop value) and with the
  stop-hit branch (§1) just above it, which already uses `"FULL_EXIT"` for
  the same kind of event (a full position close that isn't the gap-down
  case).
- No frontend code in this repo matches on the literal string
  `"EMERGENCY_EXIT"` (checked `frontend/src/`), so this is safe to change
  without a paired frontend update.
- The gap-down branch (§0) and its `emergency_exits` counter are untouched.

## Issue 3 — `open_positions_total_risk` overstated risk for de-risked winners
**Files:**
- `services/real-trade-service/entry_engine/entry.py` (`_account_state`)
- `services/real-trade-service/main.py` (manual trade-ticket review route)
- `services/real-trade-service/manual_engine.py` (`_account_state`)

All three build the `AccountState` that feeds risk_engine's §6 portfolio-risk
cap (`prospective_total = open_positions_total_risk + order_risk`, rejected
if it exceeds `equity × max_portfolio_risk_pct`). Each computed
`open_positions_total_risk` as:

```python
sum(abs(p.avg_entry_price - (p.current_stop or p.avg_entry_price)) * p.qty_open
    for p in positions)
```

Once a position's stop has been moved to or above entry —
`exit_engine/exit.py`'s breakeven-stop (§4) and age-aware ATR-trail (§5)
both do exactly this once a trade is in profit —
`avg_entry_price - current_stop` goes negative: hitting that stop now locks
in a **gain**, not a loss. `abs()` turned that guaranteed-profit distance
into a positive "risk" number and added it to the portfolio total, so every
de-risked winner kept counting against the portfolio-risk cap at its full
original (pre-breakeven) risk long after it stopped actually being able to
lose that money. Net effect: the more winning positions were open, the more
capacity the cap wrongly withheld from new, genuinely risk-carrying entries.

- All three call sites now use `max(0.0, p.avg_entry_price -
  (p.current_stop or p.avg_entry_price)) * p.qty_open` — a position whose
  stop is at/above entry contributes `0` to the portfolio-risk total instead
  of a phantom positive number, matching what the cap is actually meant to
  measure (capital that would be lost if every open stop got hit right now).
- Purely a downsize of an over-conservative number — this can only ever
  *free up* capacity for new entries that were previously blocked by an
  inflated total, never open the system to more real risk than
  `max_portfolio_risk_pct` already allows.

## Folder structure (drop into repo root, overwrite in place)
```
stockky-v2-main/
├── CANDIDATE_DELIVERY_AND_TIMESTOP_LABEL_FIX_2026-09-01.md   ← this file
└── services/
    └── real-trade-service/
        ├── candidate_engine/
        │   └── candidates.py       ← Issue 1
        ├── exit_engine/
        │   └── exit.py             ← Issue 2
        ├── entry_engine/
        │   └── entry.py            ← Issue 3
        ├── main.py                 ← Issue 3
        └── manual_engine.py        ← Issue 3
```

## No ops action needed
All three fixes are pure code changes to logic already wired up — no new
env vars, no schema migration (every touched column already exists).

## Issue 4 — adaptive_thresholds.py "30 days of history" gate measured readings, not days

**File:** `services/real-trade-service/adaptive_thresholds.py`

`adaptive_regime_threshold()` and `adaptive_status()` gated the adaptive
percentile calculation on `ADAPTIVE_MIN_HISTORY_DAYS` (30) by comparing it
against `len(scores)` — the count of raw `MarketRegimeHistory` rows. But
`record_market_score()` is called once per regime-cache refresh
(`entry_engine`'s `_REGIME_TTL_S` = 120s, effectively once per ~180s
auto-pilot cycle during market hours), not once per calendar day. 30 rows
therefore accumulates in roughly an hour of a single trading session, not
30 distinct days — the adaptive gate could start overriding the static
`ENTRY_REGIME_MIN_SCORE` off a couple hours of intraday data, defeating the
module's own "FALLBACK GUARANTEE" (never activate before 30 days of real
history).

**Fix:** both functions now compute the count of *distinct calendar days*
present in the trailing-90-day window (`{r.recorded_at.date() for r in
rows}`) and gate activation/reporting on that instead of the raw row
count. `adaptive_status()`'s response gained `history_days_available`
alongside the existing `history_readings_available` so the dashboard can
show both.

Audited alongside this (no bugs found, internally consistent):
`execution/dhan_client.py`, `cycle_runner.py`, `execution/reconcile.py`,
`execution/equity_sync.py`, `auth/dhan_credentials.py` (incl. TOTP-refresh
interplay with `execution/auto_pilot.py`'s `_totp_refresh_loop`), and
`market_feed/feed.py`. One cosmetic-only note: `dhan_client.py`'s module
docstring still describes a `modify_order` re-arm check, but no
`modify_order` function exists in the file or is called anywhere in the
service — dead documentation, not a functional bug.

**Not yet reviewed:** `risk_engine/engine.py` in full (only previously-
touched lines have had a fresh read), everything outside
`real-trade-service` (analysis-intelligence-service, decision-prediction-
service, notification-scheduler-service, api-gateway, market-data-service
beyond `candidates.py`'s delivery-pct fix).
