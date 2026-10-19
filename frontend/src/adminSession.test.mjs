// Run: node --experimental-strip-types src/adminSession.test.mjs  (Node >= 22.6)
import assert from "node:assert/strict";
import { classifyLoginFailure, loginFailureText, jwtSecondsLeft, formatCountdown, validateNewPassword, passwordStrength, nextGatePhase, initialGatePhase } from "./adminSession.ts";

// error wording: wrong password vs a service that is down
assert.equal(classifyLoginFailure("real-trade: Invalid admin credentials | position-stocks: Invalid username or password"), "bad_password");
assert.equal(classifyLoginFailure("real-trade: Could not reach /auth/login (Failed to fetch)"), "unreachable");
assert.equal(classifyLoginFailure("real-trade: Real Trade service URL isn't set. Open Real Automatic Trade"), "unreachable");
assert.equal(classifyLoginFailure("502 Bad Gateway: <html>"), "unreachable");
assert.equal(classifyLoginFailure("ADMIN_PASSWORD_HASH not configured on this deploy."), "not_configured");
assert.equal(classifyLoginFailure("something odd"), "other");
assert.match(loginFailureText("bad_password"), /Wrong username or password/);
assert.match(loginFailureText("unreachable"), /Could not reach/);
assert.match(loginFailureText("other", "x".repeat(500)), /^Login failed: x{160}$/);

// countdown
const tok = (exp) => "h." + Buffer.from(JSON.stringify({ exp })).toString("base64url") + ".s";
assert.equal(jwtSecondsLeft(tok(1000 + 90), 1000 * 1000), 90);
assert.equal(jwtSecondsLeft(tok(5), 1000 * 1000), 0);
assert.equal(jwtSecondsLeft("not-a-jwt"), null);
assert.equal(jwtSecondsLeft(null), null);
assert.equal(formatCountdown(8 * 3600 - 60), "7h 59m");
assert.equal(formatCountdown(581), "9:41");
assert.equal(formatCountdown(5), "0:05");
assert.equal(formatCountdown(-3), "0:00");

// new-password rules mirror the server
assert.match(validateNewPassword("old-password-1", "short", "short"), /at least 10/);
assert.match(validateNewPassword("old-password-1", "x".repeat(129), "x".repeat(129)), /at most 128/);
assert.match(validateNewPassword("old-password-1", " leading-space-1", " leading-space-1"), /space/);
assert.match(validateNewPassword("same-password-1", "same-password-1", "same-password-1"), /different/);
assert.match(validateNewPassword("old-password-1", "brand-new-pass-99", "brand-new-pass-98"), /do not match/);
assert.equal(validateNewPassword("old-password-1", "brand-new-pass-99", "brand-new-pass-99"), null);
assert.equal(passwordStrength(""), 0);
assert.ok(passwordStrength("Abcdefghij1!xyz") >= 3);
assert.ok(passwordStrength("abcdefghij") < passwordStrength("Abcdefghij1!xyz"));

// gate: login page never mounts the page; lock keeps it; a success animation holds the switch
assert.equal(initialGatePhase(false), "login");
assert.equal(initialGatePhase(true), "app");
assert.equal(nextGatePhase("login", false, false), "login");
assert.equal(nextGatePhase("login", true, true), "login");
assert.equal(nextGatePhase("login", true, false), "app");
assert.equal(nextGatePhase("app", true, false), "app");
assert.equal(nextGatePhase("app", false, false), "lock");
assert.equal(nextGatePhase("lock", false, false), "lock");
assert.equal(nextGatePhase("lock", true, true), "lock");
assert.equal(nextGatePhase("lock", true, false), "app");
console.log("adminSession tests passed");
