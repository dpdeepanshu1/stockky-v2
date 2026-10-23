# E68 - E31 step 12: App.tsx navigation and top bars as their own components

`frontend/src/App.tsx` 1,675 -> 1,477 lines. Five JSX blocks of the `App` component moved to `frontend/src/components/app/AppNav.tsx` (259 lines), unchanged:

| Component | What it is | Props |
|---|---|---|
| `DesktopSidebar` | left sidebar: brand, grouped links, Services, Search | 6 |
| `MobileTopBar` | mobile header: menu button, page title, session chip, backend chip, search | 9 |
| `MobileDrawer` | mobile slide-over menu (the `mobileMoreOpen &&` condition stays in `App`) | 9 |
| `DesktopTopBar` | search, session chip, live-price chip, backend chip, database wake chip, clock, theme button | 10 |
| `BackendDownBanner` | "Can't reach the backend" strip (the `backendUp === "down" && !showSettings` condition stays in `App`) | 4 |

`navItems`, `goTo`, the theme, the backend / websocket / database-wake state and all handlers stay in `App`. The `Tab` type and a new `NavItem` type moved to `components/app/appTypes.ts` (both files need them). `Fragment`, `SessionChip`, `StatusClock` and `IconName` are no longer imported by `App.tsx`; `StatusClock` is now rendered by `DesktopTopBar`.

## Test
`frontend/src/appHelpers.test.mjs` (in `npm test`) now also checks: the five components are exported by `AppNav.tsx`, keep their markers and are gone from `App.tsx`; `App` passes every prop each one declares; the "Main navigation" landmark and the drawer dialog label are kept; `Tab` lives in `appTypes.ts`. No calibration switch: a refactor has no behaviour to switch.

## How it was checked (no node_modules, no React types in the sandbox)
* the 208 moved JSX lines are verbatim substrings of the old `App.tsx` (list comparison, all five `OK`);
* TypeScript compiler over all of `src` (temporary config, no type packages): no unresolved names, no new error kinds (the 2 extra errors are the missing `react` typings for the one new `.tsx` file);
* all 12 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. On the VM: click every sidebar item; narrow the window and open / close the mobile drawer (menu, Services, Wake databases, theme); click the backend chip (down: wakes the backend, up: opens Settings); stop the backend once to see the red "Can't reach the backend" strip with its three buttons.

## Next slice
What is left in `App` is about 1,267 lines of state, effects and handlers plus the main area: the bottom nav (~25 lines), the dashboard view with its idle / loading / error / stock / scan states (~230 lines, the most state) and the tab switch (~170 lines). Next: the dashboard view.
