# E67 - E31 step 11: App.tsx helper components as their own files

`frontend/src/App.tsx` 2,147 -> 1,675 lines. Everything that sat below the `App` function moved to `frontend/src/components/app/`, code unchanged:

| File | Contents |
|---|---|
| `clocks.tsx` | `StatusClock`, `MarketClock` |
| `SettingsPage.tsx` | `SettingsPage`, `SettingsBanner`, and `powerOffAll` (only the settings page called it, so it moved from the top of `App.tsx`) |
| `OvernightAutomationPanel.tsx` | `OvernightAutomationPanel` (used by `SettingsPage`) |
| `TabButton.tsx` | `TabButton` |
| `BackendStatusDot.tsx` | `BackendStatusDot` |

`App.tsx` imports `StatusClock`, `MarketClock` and `SettingsPage`; its now-unused imports (`setVisibleInterval`, `apiUrl`, `setApiUrl`, `RateLimitDashboard`) were dropped.

Two text changes, both forced by the new folder: the dynamic `import("./api")` in `StatusClock`/`MarketClock` (`clocks.tsx`) and in `powerOffAll` became `import("../../api")`. A dynamic import is not checked by an import list, so the new guard test looks for it explicitly.

## Unused code found (moved as it was, not deleted)
`TabButton`, `BackendStatusDot` and `SettingsBanner` are not referenced anywhere in `src`. Your standing rule is no dead code, so they are candidates for deletion; I left them because deleting is not a pure move. Tell me and I remove them (and their files).

## Test (new)
`frontend/src/appHelpers.test.mjs` (in `npm test`): each helper is defined in its new file and no longer in `App.tsx`; `App.tsx` imports and renders the clocks and the settings page; no file in `components/app/` keeps a `./api` dynamic import or an import that does not resolve; the `ErrorBoundary` / `main-content` contract that `design-v4.test.mjs` needs is still in `App.tsx`. No calibration switch: a refactor has no behaviour to switch.

## How it was checked (no node_modules, no React types in the sandbox)
* the 463 moved lines are verbatim substrings of the old `App.tsx` (apart from the `export` keyword and the two paths above);
* TypeScript compiler over all of `src` (temporary config, no type packages): no unresolved names or imports other than the missing `react` / `react-dom` typings, no new error kinds (the 8 extra errors are the per-file "no React types" noise from 5 new files);
* all 12 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. On the VM, open the app and check that the header clock and market clock tick, and open Settings (do not click the power-off button; it really stops the backend).

## Next slice
The `App` component itself (1,615 lines of state, effects, navigation and the dashboard view). Likely cuts: the navigation / top bar, the dashboard view, the stock page. It holds a lot of shared state, so each cut should pass props only, like the RealAutoTrade tabs.
