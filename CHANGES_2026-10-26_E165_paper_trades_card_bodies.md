# E165 - Paper Trades: a position with no P&L is no longer drawn as a gain; card bodies read values safely (2026-10-26)

Includes E164. Sixth card-body screen (plan order: ..., Training, Paper Trades, then Watchlist, Alerts, Settings). Status: `docs/AUDIT_STATUS_E165.md`.
Roll back: in `frontend/src/components/Trades.tsx` restore `fmtPct` to `${Number(n) > 0 ? "+" : ""}${n}%`, remove `dirCls` / `dirArrow`, put back `const isUp = (trade.pnl_pct ?? 0) >= 0;` and the `isUp` / `(t.pnl_pct ?? 0) >= 0` / `r.realized_pnl >= 0` colour tests, the `{Number(t.pnl_pct)}%` backup line, `₹${trade.target}` / `₹${trade.stop_loss}` and the raw `{trade.quantity}`; drop the helpers from the import and from `components/tradesView.ts`; remove the "E165" blocks from `tradesView.test.mjs`. (No CSS change: the arrow class is E161's.)

## Bugs fixed
- **A position or closed trade with no P&L yet was drawn green** (up edge, green percent): the old test was `(pnl_pct ?? 0) >= 0`. Missing is now a dash with a neutral edge; a real 0 is flat.
- **Long decimals** (for example "+2.3456789%") are rounded to 2 places; clean values such as "+3.5%" are unchanged.
- **Backup list:** `{Number(t.pnl_pct)}%` showed "NaN%" for a non-number; now a dash or nothing.
- **Open position details:** Target and Stop loss use the same ₹ formatting as prices (and a dash when missing or 0); a missing quantity shows "—" instead of an empty " sh".
- ▲ / ▼ in front of the percent in open positions, closed trades and the backup list, so direction is not colour only. Realized P&L colour in the daily / weekly report is neutral when missing.

## Unchanged
Summary strip (E153), cash and add-funds panel, open / close / add buttons and sheets, money mode banner, backups, the net-of-charges cell, headings and column names.

## Files
`frontend/src/components/tradesView.ts` (+ `pnlView`, `dirOf`, `levelText`, `countText`, pure), `frontend/src/components/Trades.tsx`, `frontend/src/tradesView.test.mjs` (extended, still 44 suites).

## Tests
Run here: `npm test` passes (44 suites, exit 0). Four deliberate breakages caught (missing drawn as flat, level 0 shown, rounding removed, negative count shown). esbuild bundles the screen; strict `tsc` on `tradesView.ts` alone passes. NOT run: `tsc --noEmit` on the whole app, `vite build`, a browser, backend pytest.
