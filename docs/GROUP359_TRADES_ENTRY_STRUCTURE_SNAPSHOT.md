# Group 359 - structure snapshot on paper trades (Trades tab)

The Trades tab runs on decision-prediction-service's training app (paper trades), a separate service from real-trade / position-stocks. A closed paper trade could not be compared with what the daily market structure said when it was opened. This group stores that snapshot AT ENTRY and shows it. **Informational only: no trade, stop, target or decision is changed.**

## decision-prediction-service/training
- `structure_entry.py` (new, pure stdlib, never raises):
  - `sanitize_entry(record)`: keeps an allow-listed, size-capped copy of the decision service's `structure` record (mode, applied, original/policy decision, <= 8 rules with text <= 200 chars, structural stop/target/rr, a fixed set of numeric / text features, summary line). Mode `off` / unknown / not a dict -> `None` (nothing is invented). Over 4000 bytes -> trimmed.
  - `attach(feature_snapshot, record)`: returns a COPY with `structure_entry`, or `None` = "leave the stored value alone". **Written once:** if a usable entry already exists it is never replaced (it documents the moment of entry). Also reads a `structure` key inside the feature snapshot.
  - `read_entry(...)`: what `GET /api/trades` returns as `structure`.
- `app.py`:
  - `PredictionSnapshotCreate.structure: Optional[dict]` (the pick model; `ActionableCommitRequest` picks use it).
  - Stored inside the existing `prediction_snapshots.feature_snapshot` JSON as `structure_entry`: **no schema change, no migration.** Wired in `store_prediction`, the `/api/actionable/commit` new-row path, and a once-only backfill when an earlier same-day row for that symbol/decision has no entry (done BEFORE the paper trade is opened, never part of the force-refresh field list).
  - `GET /api/trades` adds `structure` per trade (one batched lookup by `prediction_id`, 400 ids per query; a failed lookup just leaves `structure` null, the list still returns).

## Frontend
- `ScanPanel.toActionablePick` now sends `structure: d.structure` (used by "to Trade" in the scan panel and the DecisionCard commit).
- `api.ts`: `ActionablePick.structure`, `PaperTrade.structure`.
- `structureView.ts`: `tradeEntryView` (plain-word label: supported / would have changed X -> Y (shadow, not applied) / changed X -> Y / no read / "no structure snapshot (trade opened before snapshots were stored)") and `tradeStructureTally` (closed trades split by supported vs flagged at entry, average P&L %, `smallSample` until both groups have 20 trades). Node tests added.
- `Trades.tsx`: "Structure at entry" block in an open position's expanded view (label, rules, structural stop / target / R:R, summary line), a "Structure" badge column in the closed table, and a one-line tally above it. Trades opened before this group show "—" / the "before snapshots were stored" text; nothing is back-filled (the structure at that time is unknown).

## Config
None added.

## Tests
- `training/tests/test_group359_structure_entry.py` (11): allow-list, unusable records, strict `applied`, size caps, copy-not-mutate, write-once, JSON-string read, garbage input, and `app.py` wiring checked as source (fastapi / sqlalchemy are not installed in the sandbox, so `app.py` itself could not be imported).
- `frontend/src/structureView.test.mjs`: entry view and tally cases.

## Limits / next
- The tally is descriptive, not evidence (small samples, mixed regimes). Same caveat as every `STRUCTURE_*` threshold.
- Trades entered through paths that do not send `structure` (manual trades, other callers of `/actionable/commit`) get no snapshot.
- Open: the post-close `structure_only` refresh job + gateway counters; sniper outcome tracking.
