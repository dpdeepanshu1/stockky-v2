// Run: node --experimental-strip-types src/alertsFeed.test.mjs   (E556, round 539 B1)
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { groupAlertsByDay, alertRow, dayLabel, alertsSummary } from "./components/alertsFeed.ts";

// 2026-11-15 10:00 IST = 04:30 UTC
const NOW = Date.UTC(2026, 10, 15, 4, 30);
const at = (y, mo, d, h, mi) => (Date.UTC(y, mo, d, h, mi) - 330 * 60000) / 1000; // IST wall clock -> epoch seconds
const it = (id, ts, o = {}) => ({ id, ts, title: "t" + id, message: "m", channel: "telegram", urgency: "normal", delivered: true, ...o });

// day labels use IST, not the browser zone: 23:50 IST on the 14th is still "Yesterday" although it is 18:20 UTC
assert.equal(dayLabel(at(2026, 10, 15, 0, 5) * 1000, NOW), "Today");          // 00:05 IST is 18:35 UTC the day before
assert.equal(dayLabel(at(2026, 10, 14, 23, 50) * 1000, NOW), "Yesterday");
assert.equal(dayLabel(at(2026, 10, 12, 9, 0) * 1000, NOW), "12 Nov");
assert.equal(dayLabel(at(2025, 11, 30, 9, 0) * 1000, NOW), "30 Dec 2025");

// grouping: newest first, one group per day, unread counted against the read marker
const h = { read_at: at(2026, 10, 15, 9, 0), items: [
  it("a", at(2026, 10, 14, 16, 0)), it("b", at(2026, 10, 15, 9, 30)), it("c", at(2026, 10, 15, 8, 0)), it("d", at(2026, 10, 12, 10, 0)),
] };
const g = groupAlertsByDay(h, NOW);
assert.deepEqual(g.map((d) => d.label), ["Today", "Yesterday", "12 Nov"]);
assert.deepEqual(g[0].rows.map((r) => r.id), ["b", "c"]);
assert.equal(g[0].unreadCount, 1); assert.equal(g[1].unreadCount, 0);
assert.equal(g[0].rows[0].time, "09:30");

// failed and urgent rows carry a symbol and a word, never colour alone
const bad = alertRow(it("x", at(2026, 10, 15, 9, 0), { delivered: false }), null);
assert.deepEqual([bad.tone, bad.symbol, bad.label], ["bad", "✕", "Not delivered"]);
const urg = alertRow(it("y", at(2026, 10, 15, 9, 0), { urgency: "HIGH" }), null);
assert.deepEqual([urg.tone, urg.symbol, urg.label], ["warn", "!", "Urgent"]);
const plain = alertRow(it("z", at(2026, 10, 15, 9, 0)), null);
assert.deepEqual([plain.tone, plain.symbol, plain.label, plain.unread], ["neutral", "", "", true]);

// long text is cut, whitespace collapsed; an empty title is named; no marker means everything is unread
const long = alertRow(it("l", at(2026, 10, 15, 9, 0), { title: "  ", message: "a\n\n b ".repeat(80) }), null);
assert.ok(long.preview.length <= 140 && long.preview.endsWith("…") && !/\n/.test(long.preview) && long.title === "(no title)");

// bad input never throws
assert.deepEqual(groupAlertsByDay(null, NOW), []);
assert.deepEqual(groupAlertsByDay({ items: [null, { id: "n", ts: NaN }], read_at: null }, NOW), []);
assert.equal(alertsSummary(null), "Nothing sent yet");
assert.equal(alertsSummary({ count: 12, unread: 3 }), "12 kept (last 200) · 3 new");
assert.equal(alertsSummary({ count: 12, unread: 0 }), "12 kept (last 200) · all read");

// wiring: api methods, the admin path regex, the panel, and the component only reads / marks
const api = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
assert.match(api, /getNotificationHistory:[\s\S]*\/notifications\/history\?limit=/);
assert.match(api, /markAlertsRead:[\s\S]*"\/notifications\/read-all", \{ method: "POST" \}/);
const { isGatewayAdminUrl } = await import("./gatewayAuth.ts");
assert.equal(isGatewayAdminUrl("/api/notifications/history?limit=50", "/api"), true);
assert.equal(isGatewayAdminUrl("/api/notifications/read-all", "/api"), true);
assert.equal(isGatewayAdminUrl("/api/notifications/health", "/api"), false);
assert.match(readFileSync(new URL("./components/NotificationsPanel.tsx", import.meta.url), "utf8"), /<AlertsFeed \/>/);
const comp = readFileSync(new URL("./components/AlertsFeed.tsx", import.meta.url), "utf8");
assert.doesNotMatch(comp, /DELETE|clearNotification|testNotifications|sendPicks/);
console.log("alertsFeed.test.mjs OK");
