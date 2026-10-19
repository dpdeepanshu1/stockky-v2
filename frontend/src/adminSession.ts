// frontend/src/adminSession.ts  (group 460 - one login for Real Trade, Position Stocks and Real Money Calibration)
// Pure helpers only (no React, no storage, no network) so they are covered by adminSession.test.mjs.

export type LoginFailure = "bad_password" | "unreachable" | "not_configured" | "other";

/** Sort a login error message into something the login page can word clearly: wrong password vs a service that is down. */
export function classifyLoginFailure(msg: string): LoginFailure {
  const m = (msg || "").toLowerCase();
  if (/could not reach|failed to fetch|networkerror|network error|isn't set|timed? ?out|\b50[234]\b|bad gateway|unavailable/.test(m)) return "unreachable";
  if (/not configured|\b500\b/.test(m)) return "not_configured";
  if (/invalid|\b401\b|wrong|incorrect/.test(m)) return "bad_password";
  return "other";
}

export function loginFailureText(kind: LoginFailure, detail = ""): string {
  switch (kind) {
    case "bad_password": return "Wrong username or password.";
    case "unreachable": return "Could not reach the trading services. Check that they are running, then try again.";
    case "not_configured": return "Admin login is not configured on the server (ADMIN_PASSWORD_HASH / SESSION_SECRET).";
    default: return detail ? `Login failed: ${detail.slice(0, 160)}` : "Login failed.";
  }
}

/** Seconds until a JWT's `exp` (null = not a readable JWT). Never negative. */
export function jwtSecondsLeft(token: string | null, nowMs: number = Date.now()): number | null {
  if (!token) return null;
  try {
    const part = token.split(".")[1];
    if (!part) return null;
    const b64 = part.replace(/-/g, "+").replace(/_/g, "/");
    const exp = JSON.parse(atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4))).exp;
    return typeof exp === "number" ? Math.max(0, Math.floor(exp - nowMs / 1000)) : null;
  } catch { return null; }
}

/** "7h 59m", "9:41" (under 10 min), "0:05". */
export function formatCountdown(sec: number): string {
  const s = Math.max(0, Math.floor(sec));
  if (s >= 3600) return `${Math.floor(s / 3600)}h ${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}m`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export const MIN_NEW_PASSWORD = 10;
export const MAX_NEW_PASSWORD = 128;

/** Same rules the server enforces (auth/admin_override.py), so the form can say so before the request. Null = fine. */
export function validateNewPassword(current: string, next: string, confirm: string): string | null {
  if (next.length < MIN_NEW_PASSWORD) return `New password must be at least ${MIN_NEW_PASSWORD} characters.`;
  if (next.length > MAX_NEW_PASSWORD) return `New password must be at most ${MAX_NEW_PASSWORD} characters.`;
  if (next.trim() !== next) return "New password must not start or end with a space.";
  if (next === current) return "New password must be different from the current one.";
  if (next !== confirm) return "The two new passwords do not match.";
  return null;
}

/** 0..4 - length and variety only; a hint, the server rules above are the real gate. */
export function passwordStrength(pw: string): number {
  if (!pw) return 0;
  let s = 0;
  if (pw.length >= MIN_NEW_PASSWORD) s++;
  if (pw.length >= 14) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw) && /[^A-Za-z0-9]/.test(pw)) s++;
  return Math.min(4, s);
}

export type GatePhase = "login" | "app" | "lock";

/**
 * The state machine behind <AdminGate>.
 *  login: never logged in on this page   -> login page, the page behind it is NOT mounted (no backend calls while locked)
 *  app:   logged in                      -> the page
 *  lock:  was logged in, session ended   -> the page stays mounted (state kept) under a re-login overlay
 * `holding` = the login form is playing its success animation, so a session that just appeared must not switch the screen yet.
 */
export function nextGatePhase(phase: GatePhase, hasSession: boolean, holding: boolean): GatePhase {
  if (phase === "login") return hasSession && !holding ? "app" : "login";
  if (phase === "app") return hasSession ? "app" : "lock";
  return hasSession && !holding ? "app" : "lock";
}

export function initialGatePhase(hasSession: boolean): GatePhase {
  return hasSession ? "app" : "login";
}
