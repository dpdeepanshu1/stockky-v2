// Run: node --experimental-strip-types reconcile.test.mjs  (Node >= 22.6). E144: helpers of the "Reconciliation" section (reconcileView.ts).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { istStamp, driftText, reconcileView, pendingView, PENDING_GIVE_UP_DAYS } from "./reconcileView.ts";

// istStamp: fixed +05:30, zone-less stamp is UTC, junk is "-"
assert.equal(istStamp("2026-10-25T09:30:00+00:00"), "2026-10-25 15:00 IST");
assert.equal(istStamp("2026-10-25T09:30:00Z"), "2026-10-25 15:00 IST");
assert.equal(istStamp("2026-10-25T09:30:00"), "2026-10-25 15:00 IST");
assert.equal(istStamp("2026-10-25T20:00:00+00:00"), "2026-10-26 01:30 IST", "rolls over midnight");
assert.equal(istStamp("2026-10-25T15:00:00+05:30"), "2026-10-25 15:00 IST");
assert.equal(istStamp("nonsense"), "-");
assert.equal(istStamp(null), "-");

// driftText: one sentence per kind
assert.equal(driftText({ kind: "missing_at_broker", severity: "warn", symbol: "AAA", book_qty: 10, broker_qty: 0 }), "AAA: the book holds 10, the broker holds none");
assert.equal(driftText({ kind: "book_over_broker", severity: "warn", symbol: "BBB", book_qty: 10, broker_qty: 4 }), "BBB: the book holds 10, the broker holds 4");
assert.match(driftText({ kind: "broker_over_book", severity: "info", symbol: "CCC", book_qty: 5, broker_qty: 8 }), /^CCC: the broker holds 8, the book 5 \(.*not an alarm\)$/);
assert.match(driftText({ kind: "stale_order", severity: "warn", symbol: "DDD", order_id: 77, side: "BUY", status: "PLACED", age_minutes: 120 }), /^order #77 DDD BUY is still PLACED after 120 min$/);
const many = Array.from({ length: 15 }, (_, i) => `S${i}`);
assert.match(driftText({ kind: "broker_only", severity: "info", symbols: many, count: 15 }), /^15 broker holding\(s\).*S11, …/);
assert.equal(driftText({ kind: "something_new", severity: "warn" }), "something_new");

// reconcileView
const rep = (o = {}) => ({ ran_at: "2026-10-25T09:30:00+00:00", mode: "REAL", drift: [], warn_count: 0, error: null, book_positions: 3, ...o });
assert.equal(reconcileView(null).kind, "unavailable");
const clean = reconcileView(rep());
assert.equal(clean.kind, "clean");
assert.equal(clean.tone, "good");
assert.equal(clean.headline, "Book and broker agree. 3 open positions in the book.");
assert.equal(clean.ranAt, "2026-10-25 15:00 IST");
assert.match(reconcileView(rep({ book_positions: 1 })).headline, /1 open position in the book\.$/);
assert.equal(reconcileView(rep({ book_positions: undefined })).headline, "Book and broker agree.");
// info rows alone are NOT a warning (shared account)
const infoOnly = reconcileView(rep({ drift: [{ kind: "broker_only", severity: "info", symbols: ["X"], count: 1 }] }));
assert.equal(infoOnly.kind, "clean");
assert.equal(infoOnly.infos.length, 1);
assert.equal(infoOnly.warns.length, 0);
const drift = reconcileView(rep({ warn_count: 2, drift: [
  { kind: "missing_at_broker", severity: "warn", symbol: "AAA", book_qty: 10, broker_qty: 0 },
  { kind: "stale_order", severity: "warn", symbol: "DDD", order_id: 5, side: "SELL", status: "PARTIAL", age_minutes: 95 },
  { kind: "broker_only", severity: "info", symbols: ["Z"], count: 1 }] }));
assert.equal(drift.kind, "drift");
assert.equal(drift.tone, "warn");
assert.match(drift.headline, /^2 warnings: the book and the broker disagree\. Nothing was changed\./);
assert.equal(drift.warns.length, 2);
assert.equal(drift.infos.length, 1);
assert.match(reconcileView(rep({ drift: [{ kind: "missing_at_broker", severity: "warn", symbol: "A", book_qty: 1, broker_qty: 0 }] })).headline, /^1 warning:/);
// a failed pass is shown as such, never as "clean"
const bad = reconcileView(rep({ error: "DhanNotConnectedError: no token" }));
assert.equal(bad.kind, "error");
assert.equal(bad.tone, "bad");
assert.match(bad.headline, /could not finish \(DhanNotConnectedError: no token\)\. The book was not changed/);
assert.doesNotThrow(() => reconcileView({ drift: null, error: null }));

// pendingView
const row = (o = {}) => ({ id: 1, symbol: "AAA", status: "EOD_SQUAREOFF", closed_day_ist: "2026-10-25", age_days: 0, dhan_exit_order_id: "9", entry_price: 100, exit_price: 100, realized_pnl: 0, resolvable_via: "order_list (same day)", error_message: "EOD_PENDING_RECONCILE", ...o });
assert.equal(pendingView(null).kind, "unavailable");
assert.equal(pendingView({ count: 0 }).kind, "unavailable");
const c = pendingView({ count: 0, rows: [] });
assert.equal(c.kind, "clean");
assert.equal(c.tone, "good");
const today = pendingView({ count: 1, rows: [row()] });
assert.equal(today.kind, "stuck");
assert.equal(today.tone, undefined, "a same-day pending exit is normal");
assert.match(today.headline, /^1 exit still waiting for the real fill price \(normal for a few minutes/);
assert.equal(today.lines[0].age, "today");
const older = pendingView({ count: 3, rows: [row({ id: 1 }), row({ id: 2, age_days: 1, resolvable_via: "trade_history (prior day)", realized_pnl: -42.4 }), row({ id: 3, age_days: PENDING_GIVE_UP_DAYS, realized_pnl: 1234 })] });
assert.equal(older.tone, "warn");
assert.match(older.headline, /^3 exits still waiting for the real fill price; 2 from an earlier day\.$/);
assert.deepEqual(older.lines.map((l) => [l.age, l.tone, l.pnl]), [["today", undefined, "₹0"], ["1 day", "warn", "-₹42"], ["3 days", "bad", "₹1,234"]]);
assert.equal(pendingView({ count: 1, rows: [row({ age_days: null, realized_pnl: null })] }).lines[0].age, "-");

// CONTRACT with the backends: every key the TS types promise is written by the Python that builds the JSON.
const root = new URL("../../", import.meta.url);
const hr = readFileSync(new URL("services/real-trade-service/execution/hourly_reconcile.py", root), "utf8");
for (const k of ["ran_at", "mode", "drift", "warn_count", "error", "book_positions"]) assert.ok(hr.includes(`"${k}"`) || hr.includes(`report["${k}"]`), `hourly_reconcile no longer sets ${k}`);
for (const k of ["kind", "severity", "symbol", "book_qty", "broker_qty", "order_id", "side", "status", "age_minutes", "symbols", "count"]) assert.ok(hr.includes(`"${k}"`), `compute_drift no longer sends ${k}`);
const ps = readFileSync(new URL("services/position-stocks-service/orders/reconcile.py", root), "utf8");
const fn = ps.slice(ps.indexOf("def list_pending_reconcile"), ps.indexOf("def resolve_stuck_pending"));
for (const k of ["id", "symbol", "status", "closed_day_ist", "age_days", "dhan_exit_order_id", "entry_price", "exit_price", "realized_pnl", "resolvable_via", "error_message"]) assert.ok(fn.includes(`"${k}"`), `list_pending_reconcile no longer sends ${k}`);
const rtApi = readFileSync(new URL("frontend/src/realTradeApi.ts", root), "utf8");
assert.match(rtApi, /rtRequest<ReconcileReportDoc>\("\/reconcile-report\/REAL"/, "the page must read the stored report, never run=true");
assert.ok(!/reconcile-report[^"`]*run=true/.test(rtApi));

// WIRING: the section is on the page, both panels poll with the shared helper and only read.
const tab = readFileSync(new URL("frontend/src/components/CalibrationControlTab.tsx", root), "utf8");
assert.match(tab, /section === "reconcile" && <ReconcilePanel svc=\{svc\} \/>/);
assert.match(tab, /\{ id: "reconcile", label: "Reconciliation" \}/);
const panel = readFileSync(new URL("frontend/src/components/calibration/ReconcilePanel.tsx", root), "utf8");
assert.match(panel, /setVisibleInterval/);
assert.ok(!/method:\s*"(POST|PUT|DELETE)"/.test(panel));
assert.deepEqual([...new Set([...panel.matchAll(/\b(?:realTradeApi|positionStocksApi)\.(\w+)\(/g)].map((m) => m[1]))].sort(), ["reconcilePending", "reconcileReport"], "the reconciliation section only calls the two read-only endpoints");

console.log("reconcile.test.mjs: all assertions passed");
