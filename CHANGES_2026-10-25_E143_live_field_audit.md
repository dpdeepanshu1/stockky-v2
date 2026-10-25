# E143 - field-level audit of the four tabs: static pass clean, live checker added (2026-10-25)

Follow-up of E142 (which proved every tab call has a route but not that the JSON carries the fields the tab reads).

## Static pass (here)
Every property of every TypeScript type in `realTradeApi.ts` (411 fields), `positionStocksApi.ts` (293), `calibrationApi.ts` (204) was looked up in the matching services' Python source: **0 fields are never named by the backend** for Auto Trade, Position Stocks and Real Money Calibration. In `api.ts` (378 fields, the dashboard's types) 21 fields are never named, none in the Paper Trades types (`PaperTrade`, `PortfolioSummary`, `TradeReportBucket`): they belong to other pages (fundamentals, institutional holders, notification config, training status, system status) and are probably optional or camel/snake mapped; not changed. This pass is weak (a name existing anywhere passes), hence the live checker.

## New: `scripts/audit_live_fields.py` (GET only, stdlib)
`TOKEN=... python3 scripts/audit_live_fields.py` fetches the typed GET endpoints the four tabs poll (45 calls: Auto Trade and the DEMO/REAL variants, Position Stocks, runner catalog/summary, `/control/flags` of both services, Paper Trades `/training/api/trades*`) and lists every REQUIRED field of the tab's type that the live JSON does not carry (MISSING = blank/NaN on the page). Also reports empty lists (cannot check), non-200 and wrong shapes (list vs object). Exit code 1 on any MISSING. Not covered: nested fields, field types, POST/PUT/DELETE, calls with other path parameters, `any` responses.
Test `scripts/test_audit_live_fields.py` (6): parser (required/optional/extends), shapes, a body synthesised from the REAL client types passes on all four tabs, a dropped field is caught by name, real HTTP. It found one bug in its own first version (the method look-ahead read the NEXT call's POST); now the call's own parentheses are matched.

## Checked here / not checked
Static pass and the 6 tests run here. NOT run: the checker against your live services (needs the VM and the admin token).
