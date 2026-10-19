// Run: node --experimental-strip-types src/popupLayout.test.mjs  (Node >= 22.6)
// group 456 guard: popups must never be positioned against the animated page wrapper (that put the Run window out of sight and dimmed the whole page).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const css = readFileSync(new URL("./design-v3.css", import.meta.url), "utf8");
for (const sel of [".terminal-main > *", ".animate-fadeIn"]) {
  const line = css.split("\n").find((l) => l.startsWith(sel + " {") && l.includes("animation:"));
  assert.ok(line, `${sel} animation rule not found`);
  assert.ok(!/\b(both|forwards)\b/.test(line), `${sel} must not use fill-mode both/forwards (keeps a transform containing block): ${line}`);
}

const modal = readFileSync(new URL("./components/calibration/SwitchesPanel.tsx", import.meta.url), "utf8");
assert.match(modal, /createPortal\(/, "Modal must be drawn into document.body with createPortal");
assert.match(modal, /document\.body,\n?\s*\);/, "Modal portal target must be document.body");

const job = readFileSync(new URL("./components/calibration/JobCard.tsx", import.meta.url), "utf8");
assert.match(job, /Watch/, "a running job needs a Watch button");
assert.match(job, /Cancel/, "a running / queued job needs a Cancel button");
assert.match(job, /Close options/, "the options panel needs a close button");
const tab = readFileSync(new URL("./components/CalibrationControlTab.tsx", import.meta.url), "utf8");
assert.match(tab, /<SessionNotice \/>/, "the page must warn before the fixed-length admin session ends");
assert.ok(!/30 idle minutes/.test(tab), "sessions are a fixed window from login, not idle-based");
console.log("popupLayout ok");
