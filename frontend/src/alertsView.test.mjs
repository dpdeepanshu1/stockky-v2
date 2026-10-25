// Run: node --experimental-strip-types src/alertsView.test.mjs
import assert from "node:assert/strict";
import { alertsKpis } from "./components/alertsView.ts";

assert.deepEqual(alertsKpis(null), []);
assert.deepEqual(alertsKpis(undefined), []);

// nothing connected: warning in words, voice not set up, storage not reported
const none = alertsKpis({});
assert.equal(none.length, 4);
assert.equal(none[0].value, "0/4"); assert.equal(none[0].tone, "warn"); assert.match(none[0].sub, /no alerts will be sent/);
assert.equal(none[1].value, "0"); assert.equal(none[1].tone, "flat");
assert.equal(none[2].value, "Not set up");
assert.equal(none[3].value, "—"); assert.equal(none[3].tone, "flat");

// everything healthy
const ok = alertsKpis({ telegram: { configured: true, enabled: true }, discord: { configured: true, enabled: true }, slack: { configured: false, enabled: false }, callmebot: { configured: true, enabled: true, recipients_count: 2 }, persisted: true });
assert.equal(ok[0].value, "3/4"); assert.equal(ok[0].sub, "Telegram · Discord · CallMeBot"); assert.equal(ok[0].tone, "info");
assert.equal(ok[1].value, "3"); assert.equal(ok[1].tone, "info");
assert.equal(ok[2].value, "On"); assert.equal(ok[2].sub, "2 recipients");
assert.equal(ok[3].value, "Saved"); assert.equal(ok[3].tone, "info");

// connected but every channel switched off is a warning
const off = alertsKpis({ telegram: { configured: true, enabled: false }, persisted: true });
assert.equal(off[1].value, "0"); assert.equal(off[1].tone, "warn"); assert.match(off[1].sub, /Connected but switched off: Telegram/);
// one on, one off names both
const mix = alertsKpis({ telegram: { configured: true, enabled: true }, slack: { configured: true, enabled: false } });
assert.equal(mix[1].value, "1"); assert.equal(mix[1].sub, "Telegram (off: Slack)");
// enabled flag without being configured does not count
assert.equal(alertsKpis({ discord: { configured: false, enabled: true } })[1].value, "0");
// a missing enabled flag is not "on"
assert.equal(alertsKpis({ telegram: { configured: true } })[1].value, "0");

// voice: set up but off; one recipient; unknown count
assert.equal(alertsKpis({ callmebot: { configured: true, enabled: false } })[2].value, "Off");
assert.equal(alertsKpis({ callmebot: { configured: true, enabled: true, recipients_count: 1 } })[2].sub, "1 recipient");
assert.equal(alertsKpis({ callmebot: { configured: true, enabled: true } })[2].sub, "recipient count not reported");
assert.equal(alertsKpis({ callmebot: { configured: true, enabled: true, recipients_count: 0 } })[2].sub, "0 recipients");

// storage: memory only warns in words; a missing flag is not "memory only"
const mem = alertsKpis({ persisted: false })[3];
assert.equal(mem.value, "Memory only"); assert.equal(mem.tone, "warn"); assert.match(mem.sub, /CACHE_DATABASE_URL/);
assert.equal(alertsKpis({})[3].value, "—");

// never leaks secrets even if the config carries them
const leak = JSON.stringify(alertsKpis({ telegram: { configured: true, enabled: true, masked: "123:SECRET", chat_id: "999" } }));
assert.ok(!leak.includes("SECRET") && !leak.includes("999"));
for (const k of ok) assert.equal(typeof k.value, "string");
console.log("alertsView ok");
