# Group 463 - overall UI polish (presentation only, frontend only)
Rebuild the `frontend` image. No API call, state or trading logic changed. Roll back: delete the "GROUP 463" block at the end of `src/design-v4.css`, and the three small edits below.

- Skip link ("Skip to content") for keyboard / TV-remote users, and `<main id="main-content">` as its target.
- The whole page area is now inside an `ErrorBoundary` keyed by the tab: a crash in one page shows an error card with "Try again" instead of a blank app, and switching tab resets it.
- Toast is a polite live region (screen readers read it), sits below the phone notch and is centred on phones.
- `EmptyState` component (`components/ui/EmptyState.tsx`): used by Market Movers and Watchlist; use it for any other empty list.
- Tables: sticky header, zebra rows, accent hover, tabular numbers. Cards: soft depth in dark and light themes, 1 px lift on hover for clickable cards (not on TV). Thin themed scrollbars, balanced headings, long numbers wrap instead of overflowing.
- Test: `src/design-v4.test.mjs` now has 6 checks (skip link target, per-tab boundary, live region, empty-state CSS and use).
- Not run here: `npm ci`, full `tsc`, `vite build` (no node_modules). Syntax-only `tsc` is clean. Run `bash scripts/docker_frontend_check.sh`.
