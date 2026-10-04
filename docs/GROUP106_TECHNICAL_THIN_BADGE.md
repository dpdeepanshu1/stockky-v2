# group106 (2026-10-04) - show the `technical_thin` label in the UI (frontend only)

Cumulative on group105. Frontend only; rebuild the frontend (`npm run build` / your usual deploy). No backend change.

## Change
- `api.ts`: `Decision.technical_thin?: boolean` (optional, absent on fast-path rows).
- `ScanPanel.tsx`: a small amber `THIN TECH` tag in each scan row's inline status line (next to the DATA quality tag) when `technical_thin === true`. The tooltip explains it.
- `ConvictionCard.tsx` (used by Hot Picks): `technical_thin?: boolean` on `ConvictionData`, and a `THIN TECH` strip under the quality gate showing "Technicals on minimal price history" when true.

## Label only
Nothing is filtered, sorted or scored by it. Rows without the field (fast path, errors) and rows where it is false show nothing extra.

## Not changed / not verified
- Other cards that show a decision (DecisionCard, SignalStream, Surprise/IPO views) were not touched; `/stock/{symbol}` already shows the flag through `data_quality.flags`.
- No frontend test runner exists. Checked with `tsc --noEmit` (clean); not looked at in a browser here, so check the tag's look on a real scan once.
