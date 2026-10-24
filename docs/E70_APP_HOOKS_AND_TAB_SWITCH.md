# E70 - E31 step 14: App.tsx finished - tab switch, three hooks, dead code removed

`frontend/src/App.tsx` 1,163 -> 354 lines. `App` is now the shell: theme, session keep-alive, database wake, idle tick, Ctrl/Cmd+K, the "goto trades" event, navigation and the toast. Everything else moved, code unchanged (the moved blocks were cut from the E69 file by line range, not retyped).

## What moved
| New file | Lines | What it holds |
|---|---|---|
| `components/app/TabContent.tsx` | 145 | the tab switch (which page the main area shows) and the 11 `lazy()` page imports; exports `loadTrades` (App still fires it before the open-deposit event) |
| `hooks/useWatchlist.ts` | 53 | `watchlist` state, `handleWatchlistUpdate`, `handleAddToWatchlist`, `handleAddManyToWatchlist` |
| `hooks/useBackendStatus.ts` | 40 | `backendUp`, `isWaking`, `checkBackend` (also reloads the watchlist after a good ping), `handleWakeBackend`, the ping on mount |
| `hooks/useScanRunner.ts` | 621 | the `view` state, stream / poll / realtime transports, resume after a refresh, restore of the last scan, `handleSearch`, `handleScan`, `handleScanWatchlist`, `handleStopScan`, `handleRetry`, the two "send to Telegram" buttons |
| `hooks/scanResult.ts` | 34 | `buildScanResultFromRows` (pure; rows from a live stream -> a `ScanResult`) so it can be tested without React |

The per-tab `ErrorBoundary` and the `Suspense` stay in `App.tsx` around `<TabContent>` (the design-v4 guard test and the lazy chunks both need that). `App` builds one typed `dashboard: DashboardViewProps` object and passes it as `dashboard={dashboard}`; TypeScript now fails the build if a dashboard prop goes missing.

Hook call order in `App` is `useWatchlist` -> `useBackendStatus` -> `useScanRunner`, which is the order the old effects ran in (the ping, then the restore of the last scan).

## Removed
* `TabButton.tsx`, `BackendStatusDot.tsx` and `SettingsBanner` (nothing used them; you asked for all remaining fixes, so they are deleted).
* The `showSettings` flag. **Bug found on the way:** the banner button "Fix in Settings" and the error card button "Check backend settings" called `setShowSettings(true)`, and the only thing that flag drove was hiding the backend-down banner (`SettingsBanner`, the thing meant to appear, was never rendered). Both buttons now open the Settings page (`openSettings = () => goTo("settings")`), where the API URL is edited. The backend-down banner is hidden while you are on the Settings page. This is the one behaviour change in this release.

## Tests
`frontend/src/appHelpers.test.mjs` (in `npm test`): TabContent keeps every page header / admin gate / lazy page and each lazy import resolves; the same 11 lazy pages; `App` passes every `TabContent` prop and every dashboard prop; `<TabContent>` still sits inside Suspense and the per-tab ErrorBoundary; each hook is exported, called by `App`, no longer defined in `App`, and its imports resolve; hook call order; the scan runner keeps its storage keys and API calls and `App` holds none of them; the three deleted components are gone; both settings buttons call `openSettings`. It also runs `buildScanResultFromRows` on complete, partial, empty, all-sell and zero-total inputs.
No calibration switch: a refactor plus one UI fix has no trading behaviour to switch.

## How it was checked (no React types and no registry access in the sandbox)
* TypeScript over all of `src` with a temporary config: no unresolved names, no wrong-type props, no new error kinds; the total dropped from 9,129 to 9,105 (the deleted files); the only new messages are "cannot find module react" for the four new files, which have no React types here.
* All 12 test files pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. On the VM run `npm ci && npx tsc && npx vite build`.

## Check on the VM
1. Dashboard: search TCS, Quick scan (progress card), Stop, Full market scan; reload mid-scan and confirm it resumes; trigger an error (wrong API URL) and press Retry.
2. Stop the gateway: the red banner appears; "Fix in Settings" must open Settings (and the banner hide there); "Wake Backend" and "Retry" work.
3. Open every tab once (spinner, then the page); watchlist: add, remove, "Scan Watchlist"; Ctrl/Cmd+K, pick a watchlist symbol.
4. Low-balance deposit prompt still jumps to Trades and opens the deposit panel.

## What is left in `App`
About 350 lines. The remaining candidates are small (`useAppShell` for theme / keep-alive / idle tick / shortcuts) and not needed for the file split to count as done.
