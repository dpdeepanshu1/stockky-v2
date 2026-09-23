# Session 4 (2026-09-12) — position-stocks-service: master toggle + 1m window

Scope: `services/position-stocks-service/` only. See
`services/position-stocks-service/TRACKING.md` §3.11/§3.12 and `STATUS.md` for full
detail — this is a short pointer, not a duplicate.

## 1. Master module enable/disable toggle
- `models.py`: `ScalpGateState.service_enabled` (Boolean, default True) — independent
  of `is_armed`. `is_armed` gates whether real orders can be placed; `service_enabled`
  gates the whole module (screening + entries).
- `main.py`: `POST /service/enable`, `POST /service/disable`, `service_enabled` field
  added to `/status`.
- Frontend: `positionStocksApi.ts` (`service_enabled` on `ScalpStatus`,
  `serviceEnable()`/`serviceDisable()`), `PositionStocksTab.tsx` ("Module" status tile,
  Enable/Pause buttons, paused-state banner).
- Exit reconciliation and EOD squareoff intentionally ignore this flag (§3.7 "no
  exceptions").

## 2. Bug fix found while wiring the toggle in
- `main.py`'s trading loop checked `gate.is_armed` before the EOD squareoff check —
  a disarmed service with open positions silently skipped the hard 3pm flat sweep.
  Reordered: reconciliation → EOD squareoff (both unconditional) → service_enabled
  gate → is_armed gate → screening/entry.

## 3. 1-minute screening window
- `config.py`: `SCAN_WINDOWS_MINUTES = [1, 5, 15, 60]`, `MIN_PCT_CHANGE_1M` (default
  0.5%, lower than 5m's 1.0% by design).
- `screening/engine.py`: `_WINDOW_THRESHOLDS` gets a 4th entry, same composite-score
  ranking path as the other three windows — no special-casing downstream.
- Frontend: window filter + grouped screener view include "1m"; screener grid widened
  4 → wide layout to fit.

## Verification done in sandbox
- All touched Python files: `ast.parse` + `py_compile` clean.
- Both touched TypeScript files (`positionStocksApi.ts`, `PositionStocksTab.tsx`):
  real TS parser (`ts.createSourceFile`), zero syntax errors.
- Not build-tested against real npm/tsc with full type resolution — same sandbox
  networking limitation noted in session 3's STATUS.md. Recommend
  `npm install && npm run build` once on the VM (STATUS.md Next steps #4).

## Migration caveat
`create_all()` never ALTERs existing tables. If `scalp_gate_state` already exists in
the deployed DB from an earlier (crashed) boot, add the column manually before
redeploying:
```sql
ALTER TABLE scalp_gate_state ADD service_enabled NUMBER(1) DEFAULT 1;
```
Per the deploy log (TRACKING.md §7), no boot has gotten past `init_tables()`
successfully yet, so this is very likely a non-issue — but worth checking first.

## Update (same session, continued): migration caveat + real build, both resolved
- `db.py::init_tables()` now calls a new `_ensure_columns()` after `create_all()` —
  idempotent, inspector-based, dialect-aware (Oracle/Postgres) column migration.
  Covers `scalp_gate_state.service_enabled` today; add a tuple to
  `_COLUMN_MIGRATIONS` for any future column added to an existing model. Removes
  the manual "check before you deploy" step entirely.
- Ran a real `npm install` (177 packages, registry reachable this session) +
  `npm run build` (`tsc && vite build`) — **zero TypeScript errors**, build
  succeeded. This is a genuine full-type-resolution build, not the syntax-only
  parse used in prior sessions when the registry wasn't reachable.
