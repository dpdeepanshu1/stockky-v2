// Run: node --experimental-strip-types src/settingsView.test.mjs
import assert from "node:assert/strict";
import { settingsKpis, apiHost, checkApiUrl, powerOffReport } from "./components/settingsView.ts";
import { readFileSync as readSrc } from "node:fs";

assert.deepEqual(settingsKpis(null), []);
assert.deepEqual(settingsKpis(undefined), []);

// host only, never path / query; empty is same-origin; junk is invalid
assert.equal(apiHost("https://api.example.com/x?y=1#z"), "api.example.com");
assert.equal(apiHost("http://localhost:8000"), "localhost:8000");
assert.equal(apiHost("  "), ""); assert.equal(apiHost(null), "");
assert.equal(apiHost("api.example.com"), null);
assert.equal(apiHost("ftp://api.example.com"), null);

const base = { backendUp: "up", url: "https://gw.example.com", savedUrl: "https://gw.example.com", theme: "dark", dbWake: "idle" };
const ok = settingsKpis(base);
assert.equal(ok.length, 4);
assert.equal(ok[0].value, "Connected"); assert.equal(ok[0].sub, "gw.example.com"); assert.equal(ok[0].tone, "info");
assert.equal(ok[1].value, "gw.example.com"); assert.equal(ok[1].sub, "saved on this device"); assert.equal(ok[1].tone, "info");
assert.equal(ok[2].value, "Dark");
assert.equal(ok[3].value, "Not checked"); assert.equal(ok[3].tone, "flat");

// backend states, spelled in words
const down = settingsKpis({ ...base, backendUp: "down" })[0];
assert.equal(down.value, "Unreachable"); assert.equal(down.tone, "warn"); assert.match(down.sub, /gw\.example\.com did not answer/);
assert.equal(settingsKpis({ ...base, backendUp: "checking" })[0].value, "Checking");
assert.equal(settingsKpis({ ...base, backendUp: "down", url: "", savedUrl: "" })[0].sub, "no answer from this origin");

// an unsaved edit warns; the saved host is still what the backend check used
const edit = settingsKpis({ ...base, url: "https://new.example.com" });
assert.equal(edit[1].value, "new.example.com"); assert.equal(edit[1].tone, "warn"); assert.match(edit[1].sub, /not saved/);
assert.equal(edit[0].sub, "gw.example.com");
// whitespace-only difference is not an edit
assert.equal(settingsKpis({ ...base, url: " https://gw.example.com " })[1].tone, "info");
// bad address
const bad = settingsKpis({ ...base, url: "gw.example.com" })[1];
assert.equal(bad.value, "Not valid"); assert.equal(bad.tone, "warn");
// empty = same origin, not an error
const empty = settingsKpis({ ...base, url: "", savedUrl: "" });
assert.equal(empty[1].value, "Same origin"); assert.equal(empty[1].sub, "no API URL set"); assert.equal(empty[0].sub, "same origin");

assert.equal(settingsKpis({ ...base, theme: "light" })[2].value, "Light");

// database wake states
assert.equal(settingsKpis({ ...base, dbWake: "waking" })[3].value, "Waking");
const dbOk = settingsKpis({ ...base, dbWake: "ok", dbDetail: "neon: up · cache: up" })[3];
assert.equal(dbOk.value, "Awake"); assert.equal(dbOk.sub, "neon: up · cache: up");
assert.equal(settingsKpis({ ...base, dbWake: "ok" })[3].sub, "all targets answered");
const dbBad = settingsKpis({ ...base, dbWake: "error", dbDetail: "x".repeat(200) })[3];
assert.equal(dbBad.value, "Wake failed"); assert.equal(dbBad.tone, "warn"); assert.equal(dbBad.sub.length, 80);
assert.equal(settingsKpis({ ...base, dbWake: "error" })[3].sub, "no detail from the backend");

// never prints a path, query or token from the address
const leak = JSON.stringify(settingsKpis({ ...base, url: "https://gw.example.com/secret?token=abc", savedUrl: "https://gw.example.com/secret?token=abc" }));
assert.ok(!leak.includes("secret") && !leak.includes("token"));
for (const k of ok) assert.equal(typeof k.value, "string");

// E168: Save URL and Power Off
for (const good of ["https://gw.example.com", "http://localhost:8000", "https://my-vm.duckdns.org:8443", ""]) assert.deepEqual(checkApiUrl(good), { ok: true }, `accept ${good}`);
for (const bad of ["hello world", "https://hello world", "ftp://x.example.com", "javascript:alert(1)", "https://", "://x"]) { const r = checkApiUrl(bad); assert.equal(r.ok, false, `refuse ${bad}`); assert.ok(r.message.length > 20); }
assert.deepEqual(checkApiUrl(null), { ok: true }); assert.deepEqual(checkApiUrl(undefined), { ok: true });
const okRep = powerOffReport({ ok: true, message: "All off.", phases: [{ phase: "scan", detail: "stopped" }, { phase: "feed" }, null, { detail: "x" }] });
assert.equal(okRep.ok, true); assert.equal(okRep.text, "All off.\n\nscan: stopped\nfeed: —\n\nReady for fresh start. Refreshing…");
assert.equal(powerOffReport({ ok: true }).text, "Processes stopped.\n\nReady for fresh start. Refreshing…");
const failRep = powerOffReport({ ok: false, error: "403: admin login required" });
assert.equal(failRep.ok, false); assert.ok(/403: admin login required/.test(failRep.text) && /Nothing was confirmed stopped/.test(failRep.text) && !/Ready for fresh start/.test(failRep.text));
assert.ok(/no answer from the backend/.test(powerOffReport({ ok: false }).text)); assert.ok(/no answer from the backend/.test(powerOffReport({ ok: false, error: "  " }).text));
const sp = readSrc(new URL("./components/app/SettingsPage.tsx", import.meta.url), "utf8");
assert.ok(/\n  if \(!window\.confirm\(/.test(sp) && /api\.powerOff\(\)/.test(sp) && !/fetch\(`\$\{base\}\/ops\/power-off`/.test(sp) && !/import\.meta\.env\.VITE_API_URL \|\| ""/.test(sp), "Power Off asks first and uses the api wrappers (saved address)");
assert.ok(/if \(!report\.ok\) return;/.test(sp) && /checkApiUrl\(normalizeGatewayUrl\(url\)\)/.test(sp) && /url: normalizeGatewayUrl\(url\)/.test(sp), "failed power-off keeps the page; Save URL checks; the strip reads the normalised address");
assert.ok(/Open Service Manager/.test(sp) && /Wake DB/.test(sp) && /Deploy checklist/.test(sp), "sections kept");
console.log("settingsView ok");
