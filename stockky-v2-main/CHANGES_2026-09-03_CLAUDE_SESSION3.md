# Stockky — Session 2026-09-03 (Claude, this conversation): closing the
5 real gaps from the last status recap

## 1. Frontend token consistency — 15 more components fixed
`DataFeed.tsx`, `DataHealthAudit.tsx`, `DecisionCard.tsx`, `FeedHealthPanel.tsx`,
`HotStocks.tsx`, `IpoFeedHealth.tsx`, `IpoTracker.tsx`, `NotificationsPanel.tsx`,
`RateLimitDashboard.tsx`, `ScanPanel.tsx`, `SignalStream.tsx`,
`SurpriseStocks.tsx`, `Trades.tsx`, `Training.tsx`, `TrainingProgressPanel.tsx`
all had the same raw-color bypass `RealAutoTrade.tsx` had (zinc/emerald/
rose/amber/red/sky/gray/blue/green/violet/yellow/indigo instead of the
token system) — same mechanical, verified-safe remap applied: every raw
color reference now resolves through `ink/graphite/slate/mist/paper/
signal.*`, `font-mono` → `font-display tabular-nums`, radius bumped.
Verified zero raw color refs remain in any of the 15 files; brace balance
checked before/after on every file (two pre-existing paren offsets in
`DecisionCard.tsx`/`SurpriseStocks.tsx` confirmed identical before and
after — proven not introduced by this edit, not just assumed).

## 2. Dynamic-universe source widened
`services/api-gateway/main.py` — new `GET /market/momentum-movers`,
a thin read-only wrapper around the existing `_get_momentum_movers()`
(full-market NSE gainers/losers/volume-gainers, already used internally
by hot-picks — not a new detection mechanism).
`watchlist_engine/dynamic_universe.py` — `_compute_desired_universe()`
now unions this with the existing volume-shock scanner instead of relying
on volume-shock alone; either source failing is non-fatal, the other
still contributes.

## 3. Healthchecks added to the other 5 services
`market-data-service`, `analysis-intelligence-service`,
`decision-prediction-service`, `notification-scheduler-service`,
`api-gateway` — same pattern as real-trade-service's (python stdlib
`urllib` hitting `/health`, no extra image dependency).

**Honest note on how this went:** my first attempt at this used a
python regex script to bulk-edit `docker-compose.yml` and it silently
corrupted the file — a `re.split`/rejoin pattern ate the newline between
`driver: json-file` and `options:` inside the `x-logging` anchor block,
breaking YAML parsing. Caught it with `yaml.safe_load` before shipping,
reverted to the last known-good version, and redid all 5 additions with
explicit `str_replace` edits instead, each verified individually. Full
file re-validated with `yaml.safe_load` at the end — confirmed all 6
backend services have a `healthcheck:` block and the resource-priority/
`EVENT_URL` fixes from two rounds ago are still intact, untouched.

## 4. `/events/raw-feed` cache — root cause found and fixed
Confirmed by reading the code: `raw_feed()` is read-only, it never
populates the event cache itself — `/check` is what calls `_fetch_events`
per subscription and warms the cache. Searched the entire codebase for
anything calling `/check` on a schedule: **nothing did.** This meant Tier
2's cache could stay permanently cold for any symbol — including every
new symbol the dynamic-universe widening (built two rounds ago) adds.
Fixed by having `dynamic_universe.py` trigger `GET {EVENT_URL}/check`
after every sync (~every 20 min, market hours only) — piggybacks on the
existing timer rather than adding a new scheduling mechanism. Generous
90s timeout (`/check` staggers 1s per symbol against Yahoo rate limits,
so a full 60-symbol pass takes real time); failure is logged, never
blocks the cycle.

## 5. Decay-profile calibration tool
`services/real-trade-service/scripts/calibrate_decay_profiles.py` (new).
This is the one gap that genuinely can't be "fixed" by writing better
numbers — there's no way to back-test `decay.py`'s `entry_band_pct`/
`decay_half_life_days`/`max_hold_days` without real outcome data, and
fabricating plausible-looking numbers would be worse than leaving them
as estimates. Built the actual analysis tool instead: per catalyst_type,
reads `WatchlistEntry` + the resulting closed `TradePosition`/
`TradeExitDecision` rows, and reports `missed_rate` (chase-guard
rejecting too often → band too tight), `time_stop_rate` (positions cut
off before the move finished → hold time too short), and median days-
held/pnl. Below `MIN_SAMPLES=8` per catalyst type it explicitly prints
"not enough data yet" rather than a number that's mostly noise — most
catalyst types will show this on first run, which is correct, not a bug.
Run it weekly as real trade history accumulates:
`python scripts/calibrate_decay_profiles.py --mode REAL --days 30`

## Verified
- All 3 touched backend files: `ast.parse` clean.
- `docker-compose.yml`: `yaml.safe_load` clean, all 6 backend services
  confirmed to have `healthcheck:`, resource-priority/EVENT_URL fixes
  confirmed still present after the corrupt-then-restore-then-redo cycle.
- All 16 touched frontend files (15 new + RealAutoTrade re-verified):
  brace-balanced, zero raw color refs remain.

---

## Correction, same day: re-audit found the frontend sweep was incomplete

Re-running my own claim through a genuinely exhaustive check (every color
family, every `.tsx` file in the tree, not the curated 15-file list)
found real gaps my first pass missed:
- `DataFeed.tsx`, `Training.tsx` — still had `violet`/`yellow` (I'd only
  extended the color map for 7 of the 15 files, not all of them).
- `MarketSentimentHeader.tsx`, `ScanPanel_UniverseButton_Snippet.tsx` —
  two files I'd never touched at all, both with raw colors.
- `RateLimitDashboard.tsx`, `Trades.tsx`, `WatchlistManager.tsx`,
  `TrainingProgressPanel.tsx`, `NotificationsPanel.tsx` — all had `cyan`,
  a color family not in any previous mapping pass.

All fixed with the same mechanical remap, then re-swept exhaustively
across every color family (`zinc/emerald/rose/amber/red/sky/gray/blue/
green/violet/yellow/indigo/orange/purple/teal/cyan/lime/fuchsia/pink/
neutral/stone`) across every `.tsx` file with no curated list — genuinely
zero raw color references remain anywhere in `frontend/src/components/`,
confirmed by the sweep finding nothing, not by assumption.

## Decay-profile calibration — real-data pull tool added
`services/real-trade-service/scripts/Stockky_Decay_Calibration.ipynb`
(new) — a Colab notebook that connects read-only to the Oracle DB (thin-
mode `oracledb`, no Instant Client needed), runs the same per-catalyst-
type analysis as `calibrate_decay_profiles.py`, and exports only the
aggregated summary (counts/rates/medians — no symbol names, no prices,
no credentials) as a CSV to hand back for real calibration numbers.
Credentials are entered via masked `getpass` prompts each run, never
saved to disk or included in the export. The notebook itself recommends
the simpler alternative first: just run the existing local script
directly on the Oracle VM and share its printed output, which never
requires putting DB credentials into Colab at all.
