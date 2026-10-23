# E66 - E31 step 10: RealAutoTrade.tsx "overview" tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 1,385 -> 879 lines. The Overview tab JSX (388 lines: arming sequence, login-when-expired, Dhan account / token / eDIS / network check, risk configuration, account snapshot, order-flow diagram, manual trade ticket) moved to `frontend/src/components/realAuto/tabs/OverviewTab.tsx` (578 lines), unchanged. Together with E57-E65 every tab of `RealAutoTrade` is now its own component.

`PortfolioSummary` and `GateStep` moved with it (only this tab used them), code unchanged. The inline `riskForm` state type became the exported `RiskForm` type (same fields); `RealAutoTrade` imports it.

`OverviewTab` takes 48 props: `mode`, `status`, `loggedIn`, `loading`, the four gate flags + `nextGateNeeded`, `positions`, the login fields and `doLogin`, the arm / disarm / pause / confirm-risk handlers, `showCloseAll` + setter + `loadStatus`, the Dhan state (account, token countdown, form fields, connect / regenerate, eDIS, network check), the risk form state and save handler, `cumBrokerage` and `loadPositionsAndOrders`. All state, effects and handlers stay in `RealAutoTrade`, so behaviour is unchanged. Imports no longer used there were dropped (`ManualTradeTicket`, `CloseAllDialog`, `OrderFlowDiagram`, `StatCard`, `SectionHdr` and the six `format` helpers).

## Test
`frontend/src/realAutoTabs.test.mjs` (in `npm test`) now also checks: the Overview tab and both helpers live in `OverviewTab.tsx` and are gone from `RealAutoTrade`; the tab keeps close-all, the manual ticket, the order-flow diagram and the risk / eDIS / token handlers; and every prop the tab declares is passed by `RealAutoTrade`. No calibration switch: a refactor has no behaviour to switch.

## How it was checked (no node_modules, no React types in the sandbox)
* the 388 JSX lines and the two helpers are identical to the original (list comparison, `True`);
* TypeScript compiler over all of `src` (temporary config, no type packages): no new error kinds apart from the one expected "cannot find module react" for the new file's type import; no unresolved names; a deliberate wrong prop type in the call was caught, so the 48 props are really type-checked; total unchanged at 9,119;
* the 11 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM. This tab holds the arm / disarm / pause / close-all buttons and the Dhan and risk forms, so open Real Trade -> Overview once and click through: login after expiry, confirm risk, arm / disarm, PAUSE ALL, CLOSE ALL dialog (cancel it), Dhan connect form, regenerate token, TPIN / eDIS, network check, save risk config, and check the portfolio summary numbers match the previous build.

## Next slice
`App.tsx` (2,147 lines) is the only large frontend file left to split.
