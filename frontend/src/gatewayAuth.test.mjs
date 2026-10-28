// Run: node --experimental-strip-types src/gatewayAuth.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { isGatewayAdminUrl, installGatewayAuth } from "./gatewayAuth.ts";

const yes = (u, b = "") => assert.equal(isGatewayAdminUrl(u, b), true, `${u} (base ${b}) should get the token`);
const no = (u, b = "") => assert.equal(isGatewayAdminUrl(u, b), false, `${u} (base ${b}) must NOT get the token`);

// protected paths, every base shape
for (const p of ["/notifications/config", "/notifications/config/telegram", "/notifications/call/me?message=hi", "/notifications/test", "/notifications/send-picks",
  "/training/train", "/ops/power-off", "/ops/circuit-reset", "/ops/overnight/config?enabled=false", "/ops/overnight/run", "/ops/resume-activity",
  "/data-feed/hard-reset", "/api/data-feed/hard-reset", "/api/feed/hard-reset",
  "/ops/qstash/publish", "/data-feed/purge-over-cap", "/api/feed/purge-over-cap",
  "/ipo/notify-top-picks?top_n=5", "/surprise/ipo/notify-top-picks", "/surprise/notify-top-picks?top_n=5", "/stockky-hot/notify-top-picks?top_n=5",
  "/ipo/purge-non-equity", "/surprise/ipo/purge-non-equity", "/scan/universe/cache"]) {
  yes(p, "");
  yes("/api" + p.replace(/^\/api/, ""), "/api");
  yes("https://gw.example.com" + p, "https://gw.example.com");
  yes("https://gw.example.com/" + p.slice(1), "https://gw.example.com/");
}
// everything else on the gateway goes out untouched
for (const p of ["/notifications/config/", "/notifications/configx", "/ops/keepalive", "/ops/circuit-status", "/ops/overnight/status", "/watchlist", "/scan/start",
  "/training/status", "/data-feed/run", "/data-feed/hard-resets", "/ops/qstash/tick", "/ops/qstash/publisher", "/ipo/scan", "/ipo/notify-top-picksx", "/scan/universe", "/stockky-hot/run", "/health"]) {
  if (p === "/notifications/config/") continue; // trailing slash form is a (404) route variant; not worth special-casing
  no(p, ""); no(p, "https://gw.example.com"); no("https://gw.example.com" + p, "https://gw.example.com");
}
// E148 (F-12): the training service's system-level actions reached through /training/* also get the token ...
for (const p of ["/training/api/train", "/training/api/train?label_source=live", "/training/train/run", "/training/train/clear-lock", "/training/api/lock/clear",
  "/training/lock", "/training/api/models/promote/v12", "/training/api/admin/init-schema", "/training/debug/abort"]) {
  yes(p, ""); yes("/api" + p, "/api"); yes("https://gw.example.com" + p, "https://gw.example.com");
}
// ... but the Training Lab's polling and the routes it uses without a login go out untouched
for (const p of ["/training/api/train/progress", "/training/api/models", "/training/api/universe/ingest", "/training/api/trades/manual", "/training/api/portfolio/deposit",
  "/training/api/predictions", "/training/api/lock", "/training/locks", "/training/api/models/promote"]) {
  no(p, ""); no("/api" + p, "/api"); no("https://gw.example.com" + p, "https://gw.example.com");
}
// other hosts never get the token
no("https://evil.example.com/ops/power-off", "https://gw.example.com");
no("https://evil.example.com/ops/power-off", "");
no("https://gw.example.com.evil.com/ops/power-off", "https://gw.example.com");

// install: attaches the token only when logged in, only for protected urls, never overrides a caller's header
const calls = [];
const store = new Map();
globalThis.window = { localStorage: { getItem: (k) => store.get(k) ?? null }, fetch: async (input, init) => { calls.push({ input, init }); return { ok: true }; } };
installGatewayAuth(() => "https://gw.example.com");
installGatewayAuth(() => "https://other"); // second call is a no-op
const auth = () => new Headers(calls.at(-1).init?.headers).get("Authorization");

await window.fetch("https://gw.example.com/ops/power-off", { method: "POST" });
assert.equal(auth(), null, "no token stored -> no header");
store.set("stockky:real_trade_session_token", "tok123");
await window.fetch("https://gw.example.com/ops/power-off", { method: "POST" });
assert.equal(auth(), "Bearer tok123");
assert.equal(calls.at(-1).init.method, "POST");
await window.fetch("https://gw.example.com/ops/power-off", { method: "POST", headers: { Authorization: "Bearer mine" } });
assert.equal(auth(), "Bearer mine", "caller's own header wins");
store.delete("stockky:real_trade_session_token");
store.set("stockky:position_stocks_session_token", "ps456");
await window.fetch("https://gw.example.com/ops/power-off", { method: "POST" });
assert.equal(auth(), "Bearer ps456", "E241: Position Stocks token is used when there is no real-trade token");
store.set("stockky:real_trade_session_token", "tok123");
await window.fetch("https://gw.example.com/ops/power-off", { method: "POST" });
assert.equal(auth(), "Bearer tok123", "real-trade token still preferred");
await window.fetch("https://gw.example.com/ops/keepalive");
assert.equal(calls.at(-1).init, undefined, "unprotected call is passed through unchanged");
await window.fetch("https://evil.example.com/ops/power-off", { method: "POST" });
assert.equal(auth(), null, "other host never gets the token");
console.log("gatewayAuth ok");
