# Session 22 (2026-09-10) — EOD square-off moved earlier + new EOD overnight signal scan

User request (verbatim intent): move EOD square-off off 15:15 so fewer orders
race Dhan's intraday cutoff near the close, and add a new end-of-day step
that looks at positive signals (news/results/events/momentum) right before
close and queues those stocks to be bought immediately at the next day's
open, instead of waiting to be rediscovered by the normal intraday scan.

Both changes are **additive** — no existing gate, entry check, or exit
behaviour was removed or weakened. Every individual entry gate (extension/
drift caps, risk_engine, regime gate, cash checks) still applies in full to
anything sourced from the new feature.

## 1. EOD square-off time: 15:15 → 15:00

- `config.py`: `EOD_SQUAREOFF_TIME_IST` default changed from `"15:15"` to
  `"15:00"`. Still fully overridable via env var — nothing structural
  changed about how the schedule loop reads it.
- `execution/auto_pilot.py`: the `parse_hhmm(config.EOD_SQUAREOFF_TIME_IST, 15, 15)`
  fallback default was also updated to `(15, 0)` for consistency (only
  matters if the env string somehow fails to parse — extremely unlikely,
  but should never silently disagree with the documented default).
- Rationale: decision #33 (session21e, from the user's own Dhan order-book
  screenshots) found that SELLs placed close to Dhan's intraday cutoff
  produced a large retry-storm of failed orders (205 failed vs 12 success
  in that sample). Firing square-off 15 minutes earlier gives materially
  more buffer for slow fills/partial retries to clear before the cutoff
  instead of racing it, and leaves a clean window before the 15:30 close
  for the new EOD signal scan (below) to run against still-live prices
  without any chance of re-entering something square-off just flattened.
- No DB migration needed for this part — it's a plain config default.

## 2. New: EOD signal scan → overnight-priority queue → next-day priority entry

A fourth scheduled feature, same on/off idiom as PREPICK / ENTER_AT_OPEN /
EOD_SQUAREOFF (per-mode dashboard toggle is the sole authority, fires at
most once per IST trading day, independently toggleable).

**Timing:** `EOD_SIGNAL_SCAN_TIME_IST` (default `15:05`, right after
square-off, still ~25 minutes before the 15:30 close).

**What it does (`execution/auto_pilot._eod_signal_scan`):**
1. Runs `candidate_engine.candidates.refresh_candidates()` one more time —
   the exact same scan every other cycle already uses, nothing new is
   fetched that wasn't already fetchable; this just catches anything
   (results/board outcomes/bulk deals/volume-shock momentum) that only
   showed up late in the day.
2. Filters to candidates with a genuinely actionable decision label
   (`BUY NOW`, `PREPARE TO BUY`, `VOLUME_SHOCK*`) and conviction score
   ≥ `EOD_SIGNAL_SCAN_MIN_CONVICTION` (default 60.0), keeps the top
   `EOD_SIGNAL_SCAN_MAX_CANDIDATES` (default 5) by conviction.
3. **Places no order today** — minutes before close is never worth
   entering (no time for the setup to work, and square-off would have
   just undone it anyway). Instead saves the picks to the existing
   resilience cache (`resilience/local_cache.py`, already used elsewhere
   for per-day suppression flags — no new table) under key
   `overnight_priority:{mode}`.
4. Explicitly consumes every candidate it looked at this pass (with a
   logged `TradeDecision` WAIT row explaining why) so a normal auto-pilot
   cycle running minutes later doesn't independently re-evaluate and
   silently WAIT them under a different, unrelated reason.

**Next morning (`execution/auto_pilot._prepick`, extended):**
- After the normal `refresh_candidates()` call, calls the new
  `_requeue_overnight_priority_candidates()`, which reads yesterday's
  snapshot (guarding: must exist, must not already be marked consumed,
  and its `trading_date` must be strictly before today — never reuses a
  same-day or stale snapshot) and inserts a fresh `TradeCandidate` row
  per symbol not already queued/open, flagged `overnight_priority=True`.
  Marks the snapshot consumed afterward so it can't be re-applied twice.
- The Telegram pre-pick alert now tags carried-over picks with 🌙 and
  reports how many came from the overnight scan vs the normal morning
  scan.
- `ENTER_AT_OPEN` then runs the existing entry cycle unmodified — these
  rows go through every gate exactly like any other candidate.

**Ranking boost, not a gate bypass (`entry_engine/entry.py`):**
- New `config.ENTRY_OVERNIGHT_PRIORITY_BONUS` (default 12.0 points) is
  added to a candidate's raw composite score (capped at 100) **only if**
  `TradeCandidate.overnight_priority` is True, before Gate 6's
  cross-candidate ranking. This is deliberately weaker than
  UPPER_CIRCUIT's composite-floor bypass — overnight-priority candidates
  still have to individually clear the floor (or a top-N slot) like any
  other candidate; the bonus only helps them rank ahead of comparable
  intraday candidates when Gate 6 is choosing among more approved setups
  than `ENTRY_MAX_NEW_PER_CYCLE` allows.
- `TradeOrderEvent` detail string now appends `[overnight-priority +12]`
  when applicable, matching the existing `[UC floor→85]` annotation
  style, so the dashboard/audit trail shows why a candidate ranked where
  it did.

**Schema changes (additive, same migration idiom as every prior session):**
- `models.py`: `TradeGateState` gains `eod_signal_scan_enabled` /
  `_enabled_at` / `_last_run` (same three-column shape as the other three
  scheduled features). `TradeCandidate` gains `overnight_priority`
  (Boolean, default False).
- `db.py`: `_ensure_gate_state_columns` extended with the three new
  columns (both Oracle and Postgres branches); new
  `_ensure_candidate_overnight_column()` (same additive-ALTER idiom as
  every other `_ensure_*_columns` function in this file) adds
  `trade_candidates.overnight_priority` on an already-deployed DB, wired
  into `init_schema()`'s startup sequence.

## Files touched
- `config.py`
- `models.py`
- `db.py`
- `execution/auto_pilot.py`
- `entry_engine/entry.py`

## Verification done
- `ast.parse` (syntax) on every touched file.
- Real Python **imports** (not just `py_compile`) of every touched module
  individually, then the full `main.py` FastAPI app entrypoint end-to-end
  against this service's actual dependencies (sqlalchemy, httpx, fastapi,
  argon2-cffi, oracledb) — all clean, no missing names/attributes.
- Standalone functional test against an in-memory SQLite DB (same harness
  idiom as `offline_test_harness.py`) covering
  `_requeue_overnight_priority_candidates`: no-snapshot (0 added),
  prior-day snapshot (requeues correctly, flags `overnight_priority=True`),
  idempotent re-run after consumption (0 added second time), and a
  same-day snapshot being correctly ignored (defensive guard) — all 4
  passed.
- Isolated arithmetic check of the Gate 6 bonus against
  `_composite_quality_score` (bonus applied correctly, 100-point cap
  respected).

## NOT done / NOT live-tested
- No live-stack test — this is a new scheduled feature touching the entry
  pipeline. Recommend enabling `eod_signal_scan_enabled` in **DEMO** mode
  first, watching one full evening→morning cycle (confirm the 🌙 Telegram
  alerts at ~15:05 and ~09:00/09:20, and that
  `trade_candidates.overnight_priority` rows actually appear and get
  evaluated at the open) before enabling for REAL.
- Both new dashboard toggles (`eod_signal_scan_enabled`) need a frontend
  control added the same way `eod_squareoff_enabled` etc. already have
  one — not touched in this pass (backend/schema/schedule-loop only, per
  the existing division of labor in this codebase between backend session
  work and frontend wiring).
