# Group 184 - data polls pause while the browser tab is hidden (frontend)

Cumulative on group 183b. This is the "per-tab polling" item: "about 10 endpoints polled per tab" (group 174 notes: it is the
browser's polling, not a log setting, and needed your call on which tabs). Rebuild: `docker compose build frontend && docker compose up -d`.

## The call I made
Not "which tabs can refresh slower". The Positions / Pipeline / Watchlist tabs poll at 2-15 s because exits and quotes
need that, so a visible tab keeps exactly its cadence. What costs requests for nothing is a tab nobody is looking at:
the browser kept running every poll while the page was in the background (another browser tab, locked phone, minimised
window). Those polls now stop while the page is hidden and run once the moment it is shown again, so the screen is
current when you look at it instead of up to one period later.

## Change
- New `src/visibleInterval.ts`: `setVisibleInterval(fn, ms)`, a drop-in for `setInterval` that skips ticks while
  `document.visibilityState === "hidden"`, runs once when the page becomes visible again if the last run was at least one
  period ago, and returns its cleanup function. A throwing poll does not stop the timer.
- Switched to it (data polls only):
  - `PositionStocksTab`: all six (15 s loadAll, 30 s Dhan live / account / restricted / real-trade account, 15 s candidate log) and the 2 s pipeline poll.
  - `RealAutoTrade`: 30 s status, 2 s pipeline, 5 s watchlist candidates, 10 s positions.
  - `App.tsx`: 20 s overnight-hold panel. `RateLimitDashboard` 30 s, `ServiceManager` 45 s, `MarketSentimentHeader` 60 s, `DecisionCard` 45 s quote.
- NOT changed: clocks (no network), job-progress polls that must run until a job ends (scan, data feed, IPO, surprise, hot
  premarket, training), the keep-alive and websocket code and `Trades`/`Training` idle polls (they already skip hidden pages).

## Checked
- `frontend/scripts/check_visible_interval.ts` (7 checks: runs while visible, silent while hidden, one run on return,
  no double fire, cleanup removes timer and listener, throwing poll keeps running, works without `document`) passes:
  `cd frontend && npx tsx scripts/check_visible_interval.ts`.
- All 8 edited files parse cleanly (TypeScript syntax check).
- **Full `tsc --noEmit` was NOT run**: this sandbox has no network, so the React type packages could not be installed.
  `npm run build` runs `tsc`, so the image build is the type check; the changes are small and the types are simple
  (`() => unknown` accepts the existing async functions).
- **Not browser-tested.** Try: open Positions, switch to another browser tab for a minute, watch the gateway log stay
  quiet, switch back and see one refresh at once.

## Effect
Each open app tab in a background browser tab used to send roughly 10 requests per 15-30 s round. Now zero while hidden.
