# E64 - E31 step 8: RealAutoTrade.tsx "log" (Activity) tab as its own component

`frontend/src/components/RealAutoTrade.tsx` 1,460 -> 1429 lines. The Activity tab JSX (33 lines) moved to `frontend/src/components/realAuto/tabs/LogTab.tsx` (49 lines), unchanged.

`LogTab` takes 3 props: `mode`, `auditRows`, `loadAudit`. The audit state and loader stay in `RealAutoTrade` (the loader is also called when the tab opens). No import became unused: `AuditLogRow` and `fmtTime` are still used elsewhere in the file.

## How it was checked (no node_modules, no React types in the sandbox)
* the 33 JSX lines are identical to the original (`diff` empty);
* TypeScript compiler over all of `src`: no new error kinds, no unresolved-name or import errors, nothing in the new file; total 9,124 -> 9,123;
* the 10 node test files in `npm test` pass.
**Not run here:** `npm ci`, full `tsc` with React types, `vite build`. Run them on the VM.

## Next slices
Still inside `RealAutoTrade`: board ~45 lines and overview ~394 lines. Next: board, then overview (the largest, with the most state; likely best cut into 3-4 cards: arming sequence, Dhan account, risk configuration, account snapshot).
