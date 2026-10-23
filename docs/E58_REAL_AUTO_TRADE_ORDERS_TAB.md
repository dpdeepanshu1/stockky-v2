# E58 - E31 step 2: RealAutoTrade.tsx "orders" tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 2,658 -> 2,556 lines. The Orders tab JSX (98 lines) moved to `frontend/src/components/realAuto/tabs/OrdersTab.tsx` (124 lines), unchanged.

| What | Where now |
| --- | --- |
| `OrdersTab` component | `realAuto/tabs/OrdersTab.tsx` |
| `isSameLocalDay` helper | same file (the Orders tab was its only user; removed from `RealAutoTrade`) |

`OrdersTab` takes seven props: `mode`, `orders`, `orderRange`, `setOrderRange`, `loadPositionsAndOrders`, `doCancelOrder`, `actionBusy`. State, effects, polling and the cancel handler stay in `RealAutoTrade`, so behaviour does not change; only where the JSX lives.

## How it was checked (the sandbox has no node_modules and no React types)
* the 98 JSX lines are identical to the original (`diff` empty); `isSameLocalDay` is identical apart from indentation;
* TypeScript compiler over all of `src`: the set of errors is the same before and after (same messages, only line numbers shifted), none in `OrdersTab.tsx`; the total went 9,130 -> 9,129 because the sandbox lacks React types, so most of those are "implicit any / JSX" noise, not real;
* the 10 node test files in `npm test` pass, including `design-v4.test.mjs`.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM.

## Next slices
Tabs still inside `RealAutoTrade` (approx. lines): board 45, overview 394, live 213, positions 107, watchlist 152, pipeline 281, charges 317, log (to end). Suggested order, smallest and least state first: positions, watchlist, live, then pipeline, charges, overview.
