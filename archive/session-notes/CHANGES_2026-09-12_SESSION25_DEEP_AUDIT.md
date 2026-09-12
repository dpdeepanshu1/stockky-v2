# Session 25 — deep re-audit round (2026-09-12)

## Scope
Deep re-review of previously-flagged "not yet reviewed" files/paths from the
prior audit round:

- `oracle_compat.py`
- `market_context/sector_signal.py`
- `watchlist_engine/sources.py`
- `candidate_engine/candidates.py` — `_quality_gate_fund_tech`
  (sector-peer comparison), `_refresh_standard_candidates`/`refresh_candidates`
  dedup
- `main.py` — remaining routes: EOD square-off feature toggle, `/dhan/*`
  endpoints, gate-arming flow (`/arm`, `/disarm`, `/cycle/run/{mode}`)

`pipeline_status.py` was skimmed again but not deep-audited (unchanged
conclusion from the prior round: observability-only, wrapped in try/except at
every call site, cannot affect trading logic — low value to dig further).

## Found & fixed

**`market_context/sector_signal.py` — wrong/mistyped NSE symbol in
`NSE_SECTOR_MAP`.** The entry for ICICI Lombard General Insurance was keyed
as `"ICICIlombard"` (mixed case, and not the company's actual NSE ticker).
Every other key in the map is the real NSE trading symbol in upper case
(matching how `TradeCandidate.symbol` / `sector_bonus_for_symbol`'s lookups
are always upper-cased elsewhere in the pipeline — see
`watchlist_engine/sources.py` and `entry_engine/entry.py`'s callers). Because
`sector_bonus_for_symbol()` does a plain `dict.get(symbol)`, this entry could
never match any real candidate symbol — ICICI Lombard's real ticker
(`ICICIGI`) silently always got `bonus 0.0` (unmapped), never the
Financial-Services US-sector nudge intended for it. Not a crash, not
data-corrupting — a quietly-dead map entry. Fixed the key to `ICICIGI`.
Swept the rest of `NSE_SECTOR_MAP` programmatically for any other
non-uppercase keys; this was the only one.

## Re-confirmed correct (no changes)

- `oracle_compat.py`: call-timeout wiring (`_attach_call_timeout`,
  `ORACLE_CALL_TIMEOUT_MS`) and the Postgres/Oracle DDL/upsert dialect
  branches are internally consistent; this file already carries its own
  2026-09-12 call-timeout fix from an earlier pass.
- `watchlist_engine/sources.py`: Tier 1→2→3 fallback ladder, `_normalize_tier1`
  field mapping (verified against `_bucket_map` and the real
  `/surprise/ipo/list` payload shape), and `_tier3_volume_shock`'s call
  signature into `candidate_engine._fetch_volume_shock_universe` all line up.
- `candidates.py` `_quality_gate_fund_tech`: self-exclusion from its own
  sector-peer comparison window is handled correctly by the caller
  (`sector_peers = [p for p in ... if p.get("symbol") != sym]`), so a
  candidate can't inflate its own percentile rank.
- `_refresh_standard_candidates` / `_refresh_volume_shock_candidates` /
  `refresh_candidates`: dedup-by-symbol (keep highest conviction),
  exclude-set wiring between the two tracks (`standard_seen` feeding into
  `shock_exclude`), and the Gate-6-requeue-aware cooldown shrink
  (`_recently_candidated_symbols`) are all correctly wired.
- `main.py` `/dhan/*` routes, `/arm`, `/disarm`, `/cycle/run/{mode}`: gate
  sequencing (admin → Dhan → risk-config) and the per-mode `threading.Lock`
  usage in `/cycle/run/{mode}` (session21c fix) are intact and consistent
  with `execution/auto_pilot.py`'s locking contract.

## Verification
- `python3 -m py_compile` clean on the touched file.
- Real Python import of `market_context.sector_signal` succeeds (module has
  no import-time side effects beyond `import config`).

## Status
No live-trading-affecting bug found this round beyond the sector-signal
symbol typo above (which was already a silent no-op, not a live-money bug —
US_SECTOR_SIGNAL_ENABLED is off by default and even when on it only nudges
Gate 6 ranking by a capped few points). Nothing here needs a DEMO re-run
before REAL; this is a pure data-correctness fix in an optional, additive,
off-by-default feature.
