# Session 46 — `MIN_PREFERRED_SCALP_POSITIONS` decision + real-trade-service reconcile-snapshot staleness fix

Two independent items closed this session: one a user decision that needed
making before it could be wired up (position-stocks-service), one a fresh
bug found from a pasted live audit-log excerpt (real-trade-service).

## 1. `MIN_PREFERRED_SCALP_POSITIONS` — decision made, wired up

Flagged unwired since session 12 (re-confirmed sessions 16, 23, 43):
declared in `config.py`, read nowhere. The blocker was always that its
intended semantics were never specified — asked the user directly; the
answer given was a goal, not a spec ("enter quality stock, buy and sell
on time and maximum profit, lower the loss"), so the implementation below
is a design decision made against that goal, kept deliberately
conservative because this is a real-money-adjacent service.

**Design principle:** being under-preferred is a reason to look at MORE
candidates, never a reason to accept a WORSE one. No risk/quality gate is
touched by this feature.

**position-stocks-service/config.py`** — two new knobs alongside the
existing `MIN_PREFERRED_SCALP_POSITIONS`:
- `MIN_PREFERRED_THRESHOLD_RELAX_PCT` (default 15.0) — bounded relaxation
  applied to `screening/engine.py`'s per-window pct-change thresholds.
- `MIN_PREFERRED_EXTRA_TOP_N` (default 2) — extra ranked candidates sent
  to `quality_gate.py` when under-preferred.

**`screening/engine.py`** — `scan()` gained an `under_preferred: bool`
param. When true, each window's threshold is multiplied by
`(1 - relax_pct/100)`, clamped to `[0, 50]%` relax, with the 1m window's
existing 0.7% noise floor still hard-enforced regardless. This only
changes which candidates get ranked/scored at all — the composite-score
formula, volume floor, range/VWAP/consistency multipliers, and every
other part of the scan are unchanged.

**`main.py`** — computes `under_preferred = len(open_syms) <
config.MIN_PREFERRED_SCALP_POSITIONS` using the same OPEN +
EXIT_LEGS_REJECTED set already used for entry exclusion, passes it into
`scan()`, and widens `QUALITY_GATE_TOP_N` by `MIN_PREFERRED_EXTRA_TOP_N`
candidates when true. `MIN_FUNDAMENTAL_SCORE`, `MIN_TECHNICAL_SCORE`,
`MIN_MARKET_CAP_CR` (quality_gate's real bar), `MAX_SPREAD_PCT`,
`RISK_PER_TRADE_PCT`, the circuit breaker, and the restricted-symbol
filter are all untouched by this feature at every call site. Surfaced in
`GET /status`'s `pipeline_config` block and in the Run Cycle scan-stage
detail text (states the relax % and current open/preferred count) so it's
visible on the dashboard whenever it's active, not a silent behavior
change.

## 2. real-trade-service: `RECONCILE_MISMATCH` repeating with a frozen `snap_as_of`

User pasted a live audit-log excerpt: the same `RECONCILE_MISMATCH` entry
(`mode=REAL snap_as_of=2026-09-15T09:32:49...`) recurring at 07:49, 08:11,
08:35, 08:44, 08:52 the next day and again at 16:25 — `snap_as_of` frozen
for many hours across multiple restarts, with the live/snapshot symbol
sets drifting further apart each time (`MONQ50` newly live-only; various
symbols snapshot-only).

**Root cause:** `resilience/local_cache.py::snapshot_open_positions()`
took `mode` from `positions[0].mode` and, critically, did `if not
positions: return` — a completely silent no-write — whenever the mode had
zero open positions at that cycle. The moment REAL's open-position count
legitimately hit zero, the snapshot stopped updating, permanently. Every
later boot's `reconcile_on_startup()` (called once per FastAPI startup)
then compared the day's actual live positions against that same
long-frozen snapshot and logged a mismatch every single time — not a real
drift, just a snapshot that had stopped being written to.

**Fix:** `snapshot_open_positions(db, mode, positions)` now takes `mode`
as an explicit parameter (the sole caller, `cycle_runner.py`, already has
it in scope) and always writes — including the valid "0 open positions"
state — instead of early-returning. `cycle_runner.py`'s call site updated
to match. This doesn't change what counts as a mismatch, only guarantees
the snapshot it's compared against is never more than one cycle stale.

## Verification
- `python3 -m py_compile` clean: `position-stocks-service/{config,main}.py`,
  `position-stocks-service/screening/engine.py`,
  `real-trade-service/{cycle_runner,resilience/local_cache}.py`.
- `pyflakes` clean on all position-stocks-service files touched (no new
  findings).
- Confirmed `snapshot_open_positions()` has exactly one call site
  (`cycle_runner.py`) and `scan()` has two call sites (`main.py`'s cycle
  path, updated; and the read-only `/candidates` display path, left at
  its default `under_preferred=False` since it's not gated on position
  count).

## Still open — genuinely needs live data or was already answered as "not now"
Unchanged from session 43/44/45: the DATAMATICS SDK MARKET→LIMIT retry
theory, the `emergency_gap_down` retry pattern, the exit-leg fill-price
field-name guess in `orders/reconcile.py`, and `STATUS.md`'s live-only
"Next steps" items all still require either a live incident to reproduce
against or your live VM/market hours — none of these can be closed from
this sandbox, and none were guessed at this session.
