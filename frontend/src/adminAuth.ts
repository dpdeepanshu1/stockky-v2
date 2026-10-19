// frontend/src/adminAuth.ts  (group 460) - the ONE front door for the three admin pages (Real Trade, Position Stocks, Real Money Calibration).
// Same storage keys and endpoints as before (realTradeApi / positionStocksApi / calibrationApi), so existing sessions and every backend route keep working.
import { AUTH_LOST_EVENT, anyToken, deriveServiceUrls, loginBoth, logoutAll } from "./calibrationApi";
import { getSessionToken as getRtToken, realTradeApi } from "./realTradeApi";
import { getSessionToken as getPsToken, positionStocksApi } from "./positionStocksApi";
import { classifyLoginFailure, jwtSecondsLeft, loginFailureText, type LoginFailure } from "./adminSession";

export const AUTH_CHANGED_EVENT = "stockky:admin-auth-changed";
export const AUTH_LOGOUT_EVENT = "stockky:admin-logout"; // an explicit log-out: the gate goes back to the login page (an expiry shows the re-login overlay instead)

export interface SessionInfo {
  has: boolean;
  secondsLeft: number | null;
  rt: boolean;
  ps: boolean;
}

function live(t: string | null): boolean {
  if (!t) return false;
  const left = jwtSecondsLeft(t);
  return left === null || left > 0;
}

export function readSession(): SessionInfo {
  let t: string | null = null;
  let rt = false, ps = false;
  try { t = anyToken(); rt = live(getRtToken()); ps = live(getPsToken()); } catch { /* storage blocked */ }
  return { has: !!t, secondsLeft: t ? jwtSecondsLeft(t) : null, rt, ps };
}

function announce() {
  try { window.dispatchEvent(new Event(AUTH_CHANGED_EVENT)); } catch { /* no window */ }
}

export interface LoginOutcome {
  ok: boolean;
  rt: boolean;
  ps: boolean;
  failure?: LoginFailure;
  message?: string;
  /** set when one service accepted the login and the other did not (you are still in) */
  partial?: string;
}

export async function loginAll(username: string, password: string): Promise<LoginOutcome> {
  try { deriveServiceUrls(); } catch { /* storage blocked */ }
  const r = await loginBoth(username.trim(), password);
  if (!r.rt && !r.ps) {
    const kind = classifyLoginFailure(r.error || "");
    return { ok: false, rt: false, ps: false, failure: kind, message: loginFailureText(kind, r.error || "") };
  }
  announce();
  const partial = r.rt && r.ps ? undefined : `${r.rt ? "Position Stocks" : "Real Trade"} could not be reached, so only ${r.rt ? "Real Trade" : "Position Stocks"} is logged in. Its page will ask again.`;
  return { ok: true, rt: r.rt, ps: r.ps, partial };
}

export function logoutEverywhere() {
  // best effort: tell the servers, then forget the tokens (a stateless JWT stays valid until it expires either way)
  try { void realTradeApi.logout().catch(() => {}); } catch { /* ignore */ }
  try { void positionStocksApi.logout().catch(() => {}); } catch { /* ignore */ }
  logoutAll();
  try { window.dispatchEvent(new Event(AUTH_LOGOUT_EVENT)); } catch { /* no window */ }
  announce();
}

export interface ChangeOutcome { ok: boolean; message: string }

/**
 * Log in with the CURRENT password, ask a service to store the new one (both services read the same stored row), then log in again with the new one
 * so both tokens are fresh. A wrong current password is reported as such; "switched off" and "service down" are worded as what they are.
 */
export async function changePasswordAll(username: string, current: string, next: string): Promise<ChangeOutcome> {
  const first = await loginAll(username, current);
  if (!first.ok) return { ok: false, message: first.message || "Could not log in with the current password." };
  let lastErr = "";
  let changed = false;
  for (const call of [first.rt ? () => realTradeApi.changePassword(current, next) : null, first.ps ? () => positionStocksApi.changePassword(current, next) : null]) {
    if (!call) continue;
    try { await call(); changed = true; break; } catch (e: any) { lastErr = String(e?.message || e); }
  }
  if (!changed) {
    logoutAll(); announce();
    const kind = classifyLoginFailure(lastErr);
    return { ok: false, message: kind === "unreachable" ? loginFailureText("unreachable") : lastErr || "The password was not changed." };
  }
  logoutAll();
  const again = await loginAll(username, next);
  return again.ok
    ? { ok: true, message: "Password changed. You are logged in with the new password." }
    : { ok: true, message: "Password changed. Log in with the new password." };
}

export { AUTH_LOST_EVENT };
