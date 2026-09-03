# Changes made by Claude — 2026-09-03 session

This zip is your uploaded `stockky-v2-main__11_.zip` (which already contained
your own implementation of the watchlist/exit-engine/resilience plan from
earlier in this conversation) with the following additional files changed.
Everything else is untouched — safe to fully replace your folder with this.

## Files changed

1. **`docker-compose.yml`**
   - Added `EVENT_URL: http://analysis-intelligence-service:8000/event` to
     `real-trade-service`'s environment block (was missing — Tier 2 of the
     watchlist degradation ladder was silently falling back to a dead
     Render URL).
   - `real-trade-service`: `cpus` 0.30→0.70, `mem_limit` 450m→1000m,
     `mem_reservation` 150m→400m (now highest priority in the stack).
   - `market-data-service`: `cpus` 0.65→0.55; `decision-prediction-service`:
     `cpus` 0.45→0.40 (trimmed to make room for the above).
   - Removed `real-trade-service`'s `depends_on` on api-gateway /
     market-data-service / notification-scheduler-service, so it starts
     independently on VM reboot instead of waiting on them.
   - Added a `healthcheck:` block to `real-trade-service` (hits `/health`
     via Python stdlib `urllib`, no extra image dependency).

2. **`services/real-trade-service/resilience/circuit_breaker.py`**
   - Added `_alert()` helper and wired it into `record_failure()` (fires on
     transition to OPEN — degraded mode) and `record_success()` (fires on
     recovery to CLOSED), via the existing `notifier.py` / Telegram config.
     Degraded mode was previously completely silent.

3. **`frontend/tailwind.config.js`**
   - Groww-style reskin. Token *names* unchanged (`ink`, `graphite`,
     `slate`, `mist`, `paper`, `signal.*`) so no component files needed
     edits — values now point at CSS variables (`var(--bg)` etc.) instead
     of fixed hex, so the existing dark/light toggle keeps working.
   - `signal.buy/sell/hold/prepare/avoid` updated to crisper Groww-style
     green/red/amber/blue/grey.
   - Added `borderRadius.card`, switched default font stack to prioritize
     DM Sans.

4. **`frontend/src/index.css`**
   - Redesigned the `html.light` theme block: near-white background, white
     cards, mint-green `#00b386` / coral-red `#ff5b52`, soft card shadow.
   - Bumped `--radius-sm`/`--radius-md` and added `--radius-lg` (was a
     tight 4-6px terminal look, now 8/14/20px).
   - `--font-sans` now leads with `"DM Sans"`.

5. **`frontend/index.html`**
   - Added DM Sans to the Google Fonts import alongside the existing
     Inter / JetBrains Mono.

6. **`frontend/src/App.tsx`**
   - Default theme changed from `"dark"` to `"light"` on first load only
     (existing users' saved `localStorage` preference is untouched).

## Update — dynamic watchlist widening (same session, later pass)

Issue #6 from the audit ("Tier 2 coverage bounded by a static watchlist")
is now resolved, not just flagged. Implemented as genuinely dynamic —
re-evaluated during market hours, not a one-time widen.

7. **`services/analysis-intelligence-service/event/main.py`**
   - `_load_state()` now tracks `subscription_meta` per symbol: `source`
     (`"user"` vs `"auto"`) and `added_at`. Backfills anything pre-existing
     as `"user"` so nothing already tracked can be accidentally pruned.
   - `/subscribe` accepts an optional `source` field (defaults to `"user"`,
     so the existing caller in `notification-scheduler-service` needs no
     change). A symbol already tagged `"user"` is never downgraded by a
     later `"auto"` subscribe.
   - **New `/unsubscribe` endpoint** — didn't exist before (the list could
     only grow). Accepts an optional `only_source` filter so a caller can
     safely remove only the symbols it added, never touching the user's
     manual watchlist.
   - `/subscriptions` accepts an optional `?source=` query filter.

8. **`services/real-trade-service/watchlist_engine/dynamic_universe.py`**
   (new file) — every ~20 minutes, market hours only: pulls the current
   "what's actually moving" set from the same volume-shock scanner Tier 3
   already uses (`candidate_engine._fetch_volume_shock_universe` — proven,
   no new detection mechanism invented), diffs it against what's currently
   auto-subscribed, `/subscribe`s newly-active names with `source="auto"`,
   and `/unsubscribe`s (`only_source="auto"`) names that fell out of
   activity. Capped at 60 auto-tracked symbols so a noisy day can't make
   the event tracker's own scan loop unboundedly slow. Every failure path
   is caught and logged, never raised — this must never block the trading
   cycle it runs inside.

9. **`services/real-trade-service/cycle_runner.py`**
   - Added a `dynamic_universe` stage, calling `refresh_dynamic_universe(db)`
     once per cycle before the existing watchlist stage — cheap to call
     every cycle since the module internally no-ops outside its own
     ~20-min refresh window and outside market hours. Wrapped in the same
     best-effort try/except pattern as the other 2026-09-02/03 stages.

**Net effect:** the auto-tracked portion of the watchlist now actually
changes across the trading day — stocks get added when they become active
and dropped when they go quiet — instead of being fixed once. The user's
manually-added notification watchlist is completely unaffected either way.

**Still open:** the desired-universe source is currently only the
volume-shock scanner (proven, already running, but momentum/volume-based
only). A broader source — e.g. api-gateway's NSE gainers/losers/volume-
gainers boards (`_get_momentum_movers`, already full-market, not
index-restricted) — could feed this too for wider catalyst-type coverage,
not just volume spikes. Not wired in this pass; the existing internal
function isn't exposed as its own clean endpoint yet, so it needs a small
new route on api-gateway first, same pattern as `/events/raw-feed` was
added last round.

## Still open — not implemented, flagged for a decision, not a bug
- **Broader dynamic-universe source** (see note above — momentum movers
  board via api-gateway, not wired in yet).
- **Full Groww-style layout redesign** (per-component, not just the theme
  tokens) — the 27 frontend components weren't individually rebuilt, only
  reskinned via the shared token system. Say which screens matter most if
  you want this taken further.
- **Healthchecks for the other 5 services** (market-data, analysis-
  intelligence, decision-prediction, notification-scheduler, api-gateway)
  — only real-trade-service has one, since it was the stated priority.

## Update — Groww-style layout rebuild (scoped, same session)

Full 27-component rebuild was explicitly scoped down rather than attempted
shallow-and-fast — this is a real system placing real trades, and blind
edits across 13.5k lines of components risked breaking things no one would
catch until it mattered. Instead, built the three things named, properly:

10. **`frontend/src/components/BottomSheet.tsx`** (new) — reusable Groww-
    style bottom sheet primitive. Slides up from the bottom with a drag
    handle and swipe-down-to-dismiss on mobile; renders as a centered
    rounded dialog on desktop (`sm:` breakpoint). Owns overlay, gesture,
    header row, optional footer — callers just supply content.

11. **`frontend/src/components/BuySniperModal.tsx`** — rebuilt on
    `BottomSheet`. Suggestion cards restyled to Groww card composition:
    symbol avatar circle, rounded-2xl cards, pill badges, bigger price
    typography.

12. **`frontend/src/components/StockChart.tsx`** — full rebuild:
    - Groww-style drag/touch scrub: dragging across the chart updates a
      big price header live and shows a marker dot at the touched point,
      reverting to the latest price on release.
    - Pill-style (rounded-full) period selector, replacing the old small
      mono-font buttons.
    - **Real bug fixed, not just style**: grid/axis/tooltip colors were
      hardcoded dark-terminal hex, so the chart didn't adapt when the
      light theme shipped two rounds ago. Now reads `--border`, `--muted`,
      `--panel`, `--fg`, `--buy`, `--sell` from the active theme via a
      small hook, so it's correct in both themes.

13. **`frontend/src/components/DecisionCard.tsx`** — the buy-quantity
    confirmation dialog (the highest-stakes single interaction in the
    app) converted from a raw centered modal to `BottomSheet`. Business
    logic in the surrounding 1493-line file was deliberately left
    untouched — only this self-contained dialog block and the new import.

**Deliberately not done, said plainly:** the remaining ~24 components
(HotStocks, Trades, RealAutoTrade, ScanPanel, SurpriseStocks, IpoTracker,
Training, etc.) were not individually rebuilt. They already inherit the
Groww color/radius/font tokens from the earlier theme-level upgrade, but
their specific card compositions and any of their own modals are still in
the original terminal-style layout. If you want more of them converted to
`BottomSheet` + Groww card composition, say which ones matter most and
they'll be done with the same care as these three, not in bulk.

## Update — Real Trade Service tab, complete redesign (same session)

`RealAutoTrade.tsx` (1816 lines, all 7 tabs: overview/live/positions/
orders/watchlist/pipeline/log) and `trading/ManualTradeTicket.tsx` (the
manual buy/sell order ticket used inside it) redesigned in full.

Unlike the rest of the app, this file bypassed the token system entirely —
it used raw Tailwind colors (`zinc-*`, `emerald-*`, `rose-*`, `amber-*`,
plus stray `red-*`/`sky-*`) instead of `ink`/`graphite`/`paper`/`mist`/
`signal.*`. Fixed systematically, not spot-patched:

14. **Every raw color class → token system**, applied mechanically across
    the whole file (verified zero raw `zinc/emerald/rose/amber/red/sky`
    references remain in either file) so it now correctly follows the
    dark/light toggle like the rest of the app, including inside
    `pnlColor()`, `Dot`, `StatCard`, `SectionHdr` and every tab's inline
    card markup.
15. **`font-mono` → `font-display tabular-nums`** everywhere (151
    instances) — numbers stay aligned via `tabular-nums`, without the
    monospace terminal look Groww doesn't use.
16. **Radius bumped** (`rounded-md`→xl, `rounded-lg`→xl, `rounded-xl`→2xl)
    across the whole file for the same soft-card look as the rest of the
    app.
17. **Tab bar rebuilt** as a Groww-style pill segmented control (rounded-
    full track, solid green active pill) — was a row of bordered boxes.
18. **Position cards** now have the symbol-avatar-circle treatment
    matching `BuySniperModal`'s card composition.

**Scope note, same as before:** this was a systematic, verifiable pass
(every color token traced and confirmed swapped, brace/paren balance
checked after each stage) rather than a line-by-line prose rewrite —
appropriate for a file this size on a system that places real orders. No
data-fetching, state, or `realTradeApi` call was touched; only class
strings and the tab-bar/position-card markup structure.
