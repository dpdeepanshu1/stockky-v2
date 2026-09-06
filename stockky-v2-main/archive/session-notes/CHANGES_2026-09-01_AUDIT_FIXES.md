# Stockky — Audit Fixes (2026-09-01)

> This copy ships inside the FULL repo zip (`stockky-v2-FULL-2026-09-01.zip`) —
> every fix below is already merged into the tree. Extract and replace your
> existing folder; nothing further to apply.

4 files changed, syntax-checked with `python3 -m py_compile` (0 errors).
Built on top of the 2026-08-31 "final" package (real-mode entry/exit fix +
scheduled automation) — this is additive, not a replacement of that zip.

## Issue 1 — `Tick` missing `volume` field (real-trade-service)
**File:** `services/real-trade-service/market_feed/feed.py`

`entry_engine/entry.py` already reads `getattr(tick, 'volume', None)` to
compute `avg_traded_value` for the risk engine's liquidity floor check, but
`Tick` never had a `volume` slot and `get_quote()` never populated one — so
the check silently ran with `avg_traded_value = None` on every real tick.

- Added `volume: Optional[int] = None` to `Tick.__slots__` / `__init__`.
- Source 1 (`/live-quote/{symbol}`, live_quotes table): now reads
  `lq.get("volume")`.
- Source 2 (`/quote/{symbol}`, market-data-service): now reads
  `q.get("volume")`.
  Both market-data-service endpoints already return a `"volume"` key, so no
  market-data-service change was needed.

## Issue 2 — Hot Picks score-repair window too narrow
**File:** `services/api-gateway/hotpicks_store.py` (`hotpicks_repair_scores`)

The repair SELECT filtered `updated_at >= NOW() - INTERVAL '24 hours'`. Rows
older than 24h (weekend gap, or a day the market wasn't scanned) were
invisible to the repair pass, which then reported "Nothing missing scores"
even when cards were showing `—`.

- Widened the window to 72 hours. `_row_needs_scores()` is still the real
  filter deciding what gets repaired — this is just the outer time bound.
- `hotpicks_repair_batch` (the separate price-only repair) was **not**
  touched — the audit only flagged the score-repair path.

## Issue 3 — Direct `gate.*` attribute reads without migration-race safety
**Files:**
- `services/real-trade-service/main.py` (`/gate-status/{mode}`)
- `services/real-trade-service/execution/auto_pilot.py` (`_schedule_tick`)

On first boot against an existing DB, the additive migration for the
scheduled-automation columns runs in `init_schema()`, but if an ORM instance
is constructed before that finishes, direct attribute access can misbehave.
`_schedule_tick` already used `getattr(gate, "prepick_enabled", False)` for
the `*_enabled` flags but not for the `*_last_run` comparisons; `main.py`'s
status route used neither.

- `main.py`: all six reads (`prepick_enabled`, `prepick_last_run`,
  `enter_at_open_enabled`, `enter_at_open_last_run`, `eod_squareoff_enabled`,
  `eod_squareoff_last_run`) now go through `getattr(gate, "...", default)`.
- `auto_pilot.py`: the three `gate.*_last_run != today` comparisons now use
  `getattr(gate, "...", None) != today`.

## Issue 4 — not fixed (by request)
`os.getenv("DECISION_URL", DECISION_URL)` redundancy in the repair endpoint
is a code-quality note only, not a functional bug — left as-is.

## Folder structure (drop into repo root, overwrite in place)
```
stockky-v2-main/
├── CHANGES_2026-09-01_AUDIT_FIXES.md          ← this file
└── services/
    ├── api-gateway/
    │   └── hotpicks_store.py                  ← Issue 2
    └── real-trade-service/
        ├── main.py                            ← Issue 3
        ├── execution/
        │   └── auto_pilot.py                  ← Issue 3
        └── market_feed/
            └── feed.py                        ← Issue 1
```

## No ops action needed for these 4 fixes
Unlike the 2026-08-31 package, none of these require new env vars — they're
pure code fixes to logic already wired up.
