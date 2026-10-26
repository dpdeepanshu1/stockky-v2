// Run: node --experimental-strip-types src/alertsView.test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { alertsKpis, checkChannelInput, saveOutcome } from "./components/alertsView.ts";

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

// E167: Save checks
const TOK = "123456789:AAH_abcdefghijklmnopqrstuvwxyz012345";
const noneConn = {}, tgOn = { telegram: true }, dcOn = { discord: true }, slOn = { slack: true };
assert.deepEqual(checkChannelInput("telegram", { telegramToken: TOK, telegramChatId: "-1001234567890" }, noneConn), { ok: true });
assert.deepEqual(checkChannelInput("telegram", { telegramToken: TOK, telegramChatId: "@my_channel" }, noneConn), { ok: true });
assert.deepEqual(checkChannelInput("telegram", { telegramToken: "", telegramChatId: "123456" }, tgOn), { ok: true });
assert.equal(checkChannelInput("telegram", { telegramToken: "", telegramChatId: "123456" }, noneConn).ok, false);
assert.equal(checkChannelInput("telegram", { telegramToken: "not-a-token", telegramChatId: "123456" }, noneConn).ok, false);
assert.equal(checkChannelInput("telegram", { telegramToken: TOK, telegramChatId: "" }, noneConn).ok, false);
assert.equal(checkChannelInput("telegram", { telegramToken: TOK, telegramChatId: "my chat" }, noneConn).ok, false);
const DC = "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghi-JKL";
assert.deepEqual(checkChannelInput("discord", { discordUrl: DC }, noneConn), { ok: true });
assert.deepEqual(checkChannelInput("discord", { discordUrl: "https://ptb.discord.com/api/webhooks/1/x" }, noneConn), { ok: true });
assert.deepEqual(checkChannelInput("discord", { discordUrl: "" }, dcOn), { ok: true });
for (const badUrl of ["", "discord.com/api/webhooks/1/x", "http://discord.com/api/webhooks/1/x", "https://evil.example/api/webhooks/1/x", "https://discord.com.evil.example/api/webhooks/1/x", "https://discord.com/login", "https://user:pw@discord.com/api/webhooks/1/x", "https://hooks.slack.com/services/T0/B0/x"]) assert.equal(checkChannelInput("discord", { discordUrl: badUrl }, noneConn).ok, false, `discord ${badUrl}`);
const SL = "https://hooks.slack.com/services/T000/B000/XXXXXXXX";
assert.deepEqual(checkChannelInput("slack", { slackUrl: SL }, noneConn), { ok: true }); assert.deepEqual(checkChannelInput("slack", { slackUrl: "" }, slOn), { ok: true });
for (const badUrl of ["", "https://hooks.slack.com/other", "https://slack.com/services/T/B/x", "http://hooks.slack.com/services/T/B/x", DC]) assert.equal(checkChannelInput("slack", { slackUrl: badUrl }, noneConn).ok, false, `slack ${badUrl}`);
assert.deepEqual(checkChannelInput("callmebot", {}, noneConn), { ok: true });
// messages never echo the secret
for (const [ch, f] of [["telegram", { telegramToken: "SECRETTOKEN-xyz", telegramChatId: "1" }], ["discord", { discordUrl: "https://evil.example/SECRETPATH" }], ["slack", { slackUrl: "nope-SECRETPATH" }]]) { const r = checkChannelInput(ch, f, noneConn); assert.equal(r.ok, false); assert.ok(!/SECRET/.test(r.message)); }
assert.deepEqual(saveOutcome("telegram", { telegram: { configured: true } }), { text: "Telegram saved and connected.", connected: true });
for (const cfg of [{ telegram: { configured: false } }, {}, null, undefined]) { const o = saveOutcome("telegram", cfg); assert.equal(o.connected, false); assert.ok(/not connected/.test(o.text)); }
assert.equal(saveOutcome("slack", { slack: { configured: true } }).text, "Slack saved and connected.");
const np = readFileSync(new URL("./components/NotificationsPanel.tsx", import.meta.url), "utf8");
assert.ok(/if \(!chk\.ok\) \{ setTestResult\(chk\.message\); return; \}/.test(np) && /checkChannelInput\(channel,/.test(np) && /saveOutcome\(channel, cfg as any\)/.test(np) && !/saved successfully/.test(np), "Save goes through the checks and reports the real result");
assert.ok(/Test CallMeBot/.test(np) && /Connect &amp; start|Connect & start/.test(np), "buttons kept");
console.log("alertsView ok");
