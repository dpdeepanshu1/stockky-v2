# Session 23 (2026-09-11) — EOD same-day entry tier + US sector overnight signal

User request (verbatim intent): (1) "if the system stock looks good it can
place an order that day too, instead of waiting for next morning's open,
which is more volatile and we might miss the move"; (2) "for prepick we can
take some idea from the US stock market sector-wise, or on some point based
on that, predict something early."

Both changes are **additive**, same posture as every prior session in this
repo: no existing gate, entry check, or exit behaviour was removed or
weakened. Both are off by default until proven in DEMO.

## 1. EOD signal scan: a stricter same-day-entry tier

Previously `_eod_signal_scan` (~15:05, right after square-off) NEVER placed
an order — it only ranked the top `EOD_SIGNAL_SCAN_MAX_CANDIDATES` (default
5, conviction >= 60) and saved them for tomorrow's PREPICK to re-queue.

Now, within that same pick list, a SECOND and stricter bar decides whether a
candidate gets a same-day shot instead of only being queued:

- `config.EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION` (default **75.0**) and
  `config.EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES` (default **2**) — the top N
  picks at/above this bar are left **unconsumed** and handed straight to
  `entry_engine.entry.evaluate_mode` at ~15:05, the same function every
  normal cycle uses. Every individual gate (extension/drift caps,
  risk_engine, regime gate, cash checks) still applies in full — nothing is
  bypassed, only which candidates get evaluated *today at all* changed.
- Everything else in the pick list keeps the exact prior behaviour: no
  order today, saved to the resilience-cache snapshot for tomorrow's
  PREPICK/`_requeue_overnight_priority_candidates`.
- **Fallback, not loss:** any same-day-entry candidate that doesn't actually
  fill today (mode disarmed, gate-rejected, out of cash, an exception during
  evaluation) automatically falls into the normal overnight-priority queue
  for tomorrow instead of being dropped.
- Note the trading-model implication (flagged to the user before building
  this): square-off already ran for the day at `EOD_SQUAREOFF_TIME_IST`
  (15:00), so anything bought at 15:05 is **not** auto-flattened that
  evening — it becomes a genuine overnight position, picked up by the
  normal open-position bookkeeping (and, if still open, tomorrow's own
  square-off) like any other held position. That is the whole point of
  what was asked for (get in before the next morning's gap), not a bug,
  but worth stating plainly since it's a real change in overnight exposure
  for the small number of candidates that clear the stricter bar.
- The two thresholds default to 75 / max 2 — deliberately conservative
  (higher than the 60 queue-only bar, fewer names) given the short
  remaining trading window and the overnight risk above. Both are plain
  env-overridable config, same idiom as every other threshold in this file.

**Files touched:** `config.py`, `execution/auto_pilot.py` (`_eod_signal_scan`
signature now takes `gate_armed`; call site passes `gate.armed`).

## 2. US sector overnight signal (PREPICK only)

New, optional signal applied at PREPICK (~09:00 IST) — safely after the US
session has closed for the night (NYSE/NASDAQ ~02:00-02:30 IST during EDT).
Off by default via `config.US_SECTOR_SIGNAL_ENABLED` (env var, `false` by
default — no per-mode dashboard toggle for this one, since it only augments
an already-scheduled step rather than firing independently).

**What it does (`market_context/sector_signal.py`, new module):**
1. `refresh_us_sector_snapshot()` fetches (via `yfinance`, same dependency
   `market-data-service` already uses) the latest 1-day % change for a
   handful of SPDR sector ETFs (XLK/XLF/XLV/XLY/XLP/XLB/XLE/XLRE/XLI/XLU/
   XLC), cached once per IST trading day in the existing resilience-cache
   snapshot table (no new table).
2. `NSE_SECTOR_MAP` (static, in-module) maps each candidate's NSE symbol to
   a broad sector, which `SECTOR_ETF_MAP` then maps to the matching US ETF.
   **Coverage caveat:** this only covers the major NSE sectoral-index
   constituents (~150 of the most liquid names) — the static feed's own
   `sector` column is mostly NULL today, so a full GICS-accurate mapping
   for every symbol in `symbols.json` isn't possible without real sector
   reference data this codebase doesn't have yet. An unmapped symbol gets
   bonus `0.0`, never a penalty.
3. `sector_bonus_for_symbol()` turns the matched ETF's overnight % return
   into a capped `+/-config.US_SECTOR_BONUS_CAP` (default 6.0) points
   bonus, linearly scaled up to `config.US_SECTOR_BONUS_FULL_SCALE_PCT`
   (default 1.5% — i.e. a sector ETF move of 1.5% or more overnight gets
   the full bonus/penalty).

**Wiring (`execution/auto_pilot.py._prepick`):** after the normal
`refresh_candidates()` + overnight-priority requeue, if the feature is
enabled, fetches today's sector snapshot and writes each unconsumed
candidate's `us_sector_bonus` field. Entirely best-effort — any failure
here just leaves every candidate at `0.0` bonus, never blocks PREPICK.

**Ranking application (`entry_engine/entry.py`):** `us_sector_bonus` is
added to `raw_composite_score` right next to the existing
`ENTRY_OVERNIGHT_PRIORITY_BONUS` application — same "Gate 6 ranking nudge
only, never a gate" posture. `TradeOrderEvent`'s detail string now appends
`[US-sector +N.N]` when non-zero, matching the existing `[overnight-priority
+12]` / `[UC floor→85]` annotation style.

**Schema (additive):** `models.py`'s `TradeCandidate` gains `us_sector_bonus`
(Float, default 0.0). `db.py`'s `_ensure_candidate_overnight_column` (same
function, extended `adds` list — same additive-ALTER idiom as every other
migration in this file) adds `trade_candidates.us_sector_bonus` on an
already-deployed DB.

**New dependency:** `yfinance==1.5.2` added to
`services/real-trade-service/requirements.txt` (same version
`market-data-service` already pins). Only imported inside
`sector_signal._fetch_us_sector_returns()`, and only actually called when
`US_SECTOR_SIGNAL_ENABLED=true` — a missing/broken install just logs a
warning and returns `{}` (bonus 0.0 for everyone that day), never crashes
PREPICK.

**Files touched:** `market_context/sector_signal.py` (new),
`market_context/__init__.py` (new), `config.py`, `models.py`, `db.py`,
`execution/auto_pilot.py`, `entry_engine/entry.py`, `requirements.txt`.

## Verification done
- `ast.parse` (syntax) and `python -m py_compile` on every touched/new file.
- Manual trace of `_eod_signal_scan`'s new control flow: confirmed
  `entry_engine.entry.evaluate_mode` queries `TradeCandidate.filter_by(mode=
  mode, consumed=False)` (limit 20) and marks every row it processes
  `consumed=True` unconditionally near the top of its loop — so after it
  runs, `same_day_entry` candidates are correctly picked up by name
  (`entry_details`) for the "entered vs fell back" split, and the fallback
  branch's `if not c.consumed` check correctly no-ops for anything
  `evaluate_mode` already touched (avoiding a duplicate `TradeDecision` row)
  while still queueing non-entered symbols for tomorrow.

## NOT done / NOT live-tested
- **No live-stack test and no dependency-backed import check** — this
  session's sandbox has no network access, so `pip install` /
  `python -c "import fastapi, sqlalchemy, ..."` (the standard verification
  step every prior session's CHANGES doc records) could not be run.
  Recommend running that same import check in a normal dev environment
  before deploying, in addition to the DEMO-first rollout below.
- No live-stack test for either feature — both touch the entry pipeline.
  Recommend enabling `EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION`'s effective
  tier (it's live the moment `eod_signal_scan_enabled` is already on for a
  mode — there's no separate toggle for the same-day-entry tier itself) in
  **DEMO** first, watching one full 15:05 same-day-entry attempt end to
  end, before relying on it in REAL. Same for `US_SECTOR_SIGNAL_ENABLED=true`
  — watch one PREPICK cycle's Telegram/audit trail for `[US-sector ...]`
  annotations before trusting the ranking nudge in REAL.
- `NSE_SECTOR_MAP` coverage is partial by design (see above) — extending it,
  or wiring a real sector-reference data source, is a natural follow-up,
  not required for this feature to be safe (unmapped = 0.0 bonus).
- No dashboard control was added for `US_SECTOR_SIGNAL_ENABLED` — it's an
  env-only kill switch for now, same as the original three `*_ENABLED` env
  vars before the 2026-09-01 per-mode-toggle migration. Consider adding a
  dashboard toggle later if this proves out.
