// frontend/src/calibrationApi.ts  (group 442 - Real Money Calibration page)
//
// Three clients in one file, each with its own base URL, because they are three separate services (same reasoning as realTradeApi.ts / positionStocksApi.ts):
//   * runner   - calibration-runner-service: runs the test / calibration / evidence scripts and keeps their txt output   (nginx: /calibration/)
//   * control  - GET/POST /control/* of real-trade-service ("rt") and of position-stocks-service ("ps"): the ON / OFF switches
// All three accept the same admin session (one SESSION_SECRET), so the page reuses the tokens the Auto Trade and Position Stocks pages already stored.
// Pure helpers at the bottom (trade rows, day totals) have no network and are covered by calibration.test.mjs.

import { getRealTradeApiUrl, setRealTradeApiUrl, getSessionToken as getRtToken, setSessionToken as setRtToken, realTradeApi } from "./realTradeApi";
import { getPositionStocksApiUrl, setPositionStocksApiUrl, getSessionToken as getPsToken, setSessionToken as setPsToken, positionStocksApi } from "./positionStocksApi";
import { getApiUrl } from "./api";

import { tradesFromPositionStocks, tradesFromRealTrade } from "./calibrationTrades";
export * from "./calibrationTrades";

export type Svc = "rt" | "ps";

const RUNNER_URL_KEY = "stockky:calibration_api_url";

export function getRunnerUrl(): string {
  try {
    const stored = localStorage.getItem(RUNNER_URL_KEY);
    if (stored) return stored;
  } catch { /* storage blocked: fall through */ }
  const env = ((import.meta as any).env?.VITE_CALIBRATION_URL || "").replace(/\/$/, "");
  if (env) return env;
  // The normal deployment serves every service under one host: .../realtrade -> .../calibration
  const rt = getRealTradeApiUrl();
  if (rt && /\/realtrade$/.test(rt)) return rt.replace(/\/realtrade$/, "/calibration");
  return "/calibration";
}

export function setRunnerUrl(url: string) {
  const clean = url.trim().replace(/\/$/, "");
  try {
    if (clean) localStorage.setItem(RUNNER_URL_KEY, clean);
    else localStorage.removeItem(RUNNER_URL_KEY);
  } catch { /* ignore */ }
}

/** group 444: a stored token whose JWT `exp` is already past is as good as none (the server answers 401 ExpiredSignatureError), so the page shows the login card instead of a wall of red errors. */
function live(t: string | null): string | null {
  if (!t) return null;
  try {
    const part = t.split(".")[1];
    if (part) {
      const exp = JSON.parse(atob(part.replace(/-/g, "+").replace(/_/g, "/"))).exp;
      if (typeof exp === "number" && exp * 1000 <= Date.now()) return null;
    }
  } catch { /* not a JWT we can read: let the server decide */ }
  return t;
}

export const AUTH_LOST_EVENT = "stockky:calibration-auth-lost";

/** Any valid admin session works for the runner; prefer the real-trade one. */
export function anyToken(): string | null {
  return live(getRtToken()) || live(getPsToken());
}

/**
 * group 445: the two trading services each have their own URL, stored by the Auto Trade / Position Stocks pages. When one was never set it is "", every
 * call went to the page's own origin (index.html, or the OTHER service's route) and the two tabs showed the same thing or the same error. In the normal
 * deployment everything sits under one host (/api, /realtrade, /positionstocks, /calibration), so a missing URL is derived from the other service's URL
 * or from the gateway URL and saved, exactly as if it had been typed in the page. A URL that is already set is never changed.
 */
export function deriveServiceUrls(): { rt: string; ps: string } {
  let rt = getRealTradeApiUrl();
  let ps = getPositionStocksApiUrl();
  const swap = (u: string, from: string, to: string) => (new RegExp(`/${from}$`).test(u) ? u.replace(new RegExp(`/${from}$`), `/${to}`) : "");
  let base = "";
  try {
    const g = (getApiUrl() || "").replace(/\/$/, "");
    base = g.replace(/\/api$/, "");
  } catch { /* storage blocked */ }
  if (!rt) { rt = swap(ps, "positionstocks", "realtrade") || `${base}/realtrade`; try { setRealTradeApiUrl(rt); } catch { /* ignore */ } }
  if (!ps) { ps = swap(rt, "realtrade", "positionstocks") || `${base}/positionstocks`; try { setPositionStocksApiUrl(ps); } catch { /* ignore */ } }
  return { rt, ps };
}

export function serviceUrl(svc: Svc): string {
  return svc === "rt" ? getRealTradeApiUrl() : getPositionStocksApiUrl();
}

export function serviceToken(svc: Svc): string | null {
  // the two services share SESSION_SECRET, so either token is accepted by either one; prefer the service's own
  return svc === "rt" ? live(getRtToken()) || live(getPsToken()) : live(getPsToken()) || live(getRtToken());
}

export async function loginBoth(username: string, password: string): Promise<{ rt: boolean; ps: boolean; error?: string }> {
  let rt = false, ps = false;
  const errs: string[] = [];
  try { const r = await realTradeApi.login(username, password); setRtToken(r.token); rt = true; } catch (e: any) { errs.push(`real-trade: ${e?.message || e}`); }
  try { const r = await positionStocksApi.login(username, password); setPsToken(r.token); ps = true; } catch (e: any) { errs.push(`position-stocks: ${e?.message || e}`); }
  return { rt, ps, error: rt || ps ? undefined : errs.join(" | ") };
}

export function logoutAll() {
  setRtToken(null);
  setPsToken(null);
}

export class ApiError extends Error {
  status: number;
  detail: any;
  constructor(status: number, detail: any) {
    super(typeof detail === "string" ? detail : detail?.message || JSON.stringify(detail));
    this.status = status;
    this.detail = detail;
  }
  get needsConfirm(): boolean {
    return this.status === 409 && !!this.detail && typeof this.detail === "object" && this.detail.needs_confirm === true;
  }
}

async function call<T>(base: string, token: string | null, path: string, init?: RequestInit): Promise<T> {
  if (!token) throw new ApiError(401, "Admin login required (same password as Auto Trade).");
  let resp: Response;
  try {
    resp = await fetch(`${base}${path}`, { ...init, headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init?.headers as Record<string, string> | undefined) } });
  } catch (e: any) {
    throw new ApiError(0, `Cannot reach ${base || "(no URL set)"}: ${e?.message || e}`);
  }
  const raw = await resp.text();
  let data: any = null;
  if (raw && raw.trim()) {
    try { data = JSON.parse(raw); } catch { data = raw; }
  }
  if (!resp.ok) {
    if (resp.status === 401) {
      // group 444: the session is dead (expired / idle timeout): forget it and tell the page, which then shows the login card
      logoutAll();
      try { window.dispatchEvent(new Event(AUTH_LOST_EVENT)); } catch { /* no window */ }
    }
    throw new ApiError(resp.status, data && data.detail !== undefined ? data.detail : (typeof data === "string" ? data.slice(0, 200) : resp.statusText));
  }
  // group 443: a 200 that is not JSON (index.html from the SPA fallback when the nginx /calibration/ or /control route is missing) used to be handed
  // to the page as if it were data and crashed the render (blank page). Say what is wrong instead.
  if (typeof data === "string" && data.trim()) {
    const html = /^\s*<(!doctype|html)/i.test(data);
    throw new ApiError(502, html
      ? `${base}${path} answered with a web page instead of JSON: the nginx route for it is missing (install deploy/nginx-stockky.conf, or rebuild the frontend container).`
      : `${base}${path} answered with text, not JSON: ${data.slice(0, 120)}`);
  }
  return data as T;
}

// ------------------------------------------------------------------ runner
export interface JobParam { name: string; label: string; kind: "int" | "str" | "bool" | "choice"; default: any; min: number | null; max: number | null; choices: string[]; help: string }
export interface JobDef {
  id: string; title: string; group: string; when: string; what: string; params: JobParam[]; heavy: boolean; window: "any" | "open" | "closed";
  timeout_s: number; needs: string[]; after: string; auto_elsewhere: boolean;
}
export interface PackDef { id: string; title: string; when: string; what: string; jobs: [string, Record<string, any>][]; stop_on_failure: boolean }
export interface ScheduleSlot { id: string; pack: string; days: number[]; time: string; enabled: boolean; first_week_only?: boolean }
export interface Catalog {
  groups: { id: string; title: string }[]; jobs: JobDef[]; packs: PackDef[]; schedule_default: ScheduleSlot[];
  data_present: { history_folders: string[]; exports: string[]; bar_files: number; has_summary: boolean };
}
export type RunStatus = "queued" | "running" | "success" | "attention" | "failed" | "timeout" | "cancelled" | "blocked";
export interface RunStep { job: string; n: number; title: string; status: RunStatus; seconds: number; detail: string }
export interface RunRec {
  id: string; kind: "job" | "pack"; target: string; title: string; params: Record<string, any>; by: string; trigger: string; status: RunStatus;
  queued_at: string | null; started_at: string | null; finished_at: string | null; steps: RunStep[]; jobs: { id: string; title: string; status: RunStatus; seconds: number }[];
  headline: string[]; note: string; exit_summary: string; files?: { name: string; bytes: number }[];
}
export interface RunnerState { current: RunRec | null; queued: RunRec[]; market_open_now: boolean; now_ist: string }
export interface Summary {
  jobs: Record<string, { status: RunStatus; finished_at: string | null; run_id: string; headline: string[] } | null>;
  packs: Record<string, { status: RunStatus; finished_at: string | null; run_id: string } | null>;
  state: RunnerState;
}

const R = <T,>(path: string, init?: RequestInit) => call<T>(getRunnerUrl(), anyToken(), path, init);

export const runnerApi = {
  health: () => fetch(`${getRunnerUrl()}/health`).then((r) => r.json()),
  session: () => R<{ ok: boolean; user: string }>("/auth/session"),
  catalog: () => R<Catalog>("/catalog"),
  summary: () => R<Summary>("/summary"),
  runs: (limit = 60, target = "") => R<{ state: RunnerState; runs: RunRec[] }>(`/runs?limit=${limit}${target ? `&target=${encodeURIComponent(target)}` : ""}`),
  run: (id: string) => R<RunRec>(`/runs/${encodeURIComponent(id)}`),
  tail: (id: string, offset: number) => R<{ status: RunStatus; offset: number; size: number; text: string }>(`/runs/${encodeURIComponent(id)}/tail?offset=${offset}`),
  start: (kind: "job" | "pack", target: string, params: Record<string, any> = {}, confirm = false) =>
    R<RunRec>("/runs", { method: "POST", body: JSON.stringify({ kind, target, params, confirm }) }),
  cancel: (id: string) => R<{ cancelled: boolean }>(`/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  schedule: () => R<{ enabled: boolean; entries: ScheduleSlot[]; last_fired: Record<string, string> }>("/schedule"),
  putSchedule: (body: { enabled?: boolean; entries?: ScheduleSlot[] }) => R<{ enabled: boolean; entries: ScheduleSlot[]; last_fired: Record<string, string> }>("/schedule", { method: "PUT", body: JSON.stringify(body) }),
  calibrationSummary: async (): Promise<string> => {
    const resp = await fetch(`${getRunnerUrl()}/calibration-summary`, { headers: { Authorization: `Bearer ${anyToken() || ""}` } });
    if (!resp.ok) throw new ApiError(resp.status, resp.status === 404 ? "No calibration summary yet: run the full calibration first." : resp.statusText);
    return resp.text();
  },
};

/** Download needs the bearer header, so the file is fetched and saved from a blob (a plain link could not send the token). */
export async function downloadRunFile(id: string, file: string | "zip"): Promise<void> {
  const path = file === "zip" ? `/runs/${encodeURIComponent(id)}/zip` : `/runs/${encodeURIComponent(id)}/download?file=${encodeURIComponent(file)}`;
  const resp = await fetch(`${getRunnerUrl()}${path}`, { headers: { Authorization: `Bearer ${anyToken() || ""}` } });
  if (!resp.ok) throw new ApiError(resp.status, resp.statusText || "download failed");
  const blob = await resp.blob();
  saveBlob(blob, file === "zip" ? `${id}.zip` : `${id}_${file.split("/").pop()}`);
}

export function saveBlob(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
}

// ------------------------------------------------------------------ switches (control kit of each service)
export interface FlagRow {
  key: string; title: string; group: string; kind: "bool" | "enum" | "int" | "float"; help: string; choices: string[]; lo: number | null; hi: number | null; live: boolean;
  shipped: string | null; recommended: string | null; looser: string; evidence: string; running: string | null; desired: string | null; effective: string | null;
  source: "override" | "env" | "default"; override: { value: string; by: string; at: string; note: string } | null; pending_restart: boolean;
  matches_recommended: boolean | null; restart_needed_on_change: boolean;
}
export interface FlagsDoc {
  service: string; persistent: boolean; state_dir: string; store_error: string | null; restart_hint: string;
  summary: { flags: number; overrides: number; pending_restart: number; off_recommended: number }; flags: FlagRow[];
}
export interface SetResult { ok: boolean; key: string; change: string; from?: string; value: string; applied_live: boolean; restart_required: boolean; message?: string }
export interface PresetPlanRow { key: string; title: string; from: string | null; to: string; change: string; live: boolean }

const C = <T,>(svc: Svc, path: string, init?: RequestInit) => call<T>(serviceUrl(svc), serviceToken(svc), `/control${path}`, init);

export const controlApi = {
  flags: (svc: Svc) => C<FlagsDoc>(svc, "/flags"),
  set: (svc: Svc, key: string, value: string | boolean | number, confirm = false, note = "") =>
    C<SetResult>(svc, `/flags/${encodeURIComponent(key)}`, { method: "POST", body: JSON.stringify({ value, confirm, note }) }),
  clear: (svc: Svc, key: string) => C<any>(svc, `/flags/${encodeURIComponent(key)}`, { method: "DELETE" }),
  preset: (svc: Svc, dry_run: boolean, confirm = false) => C<{ ok: boolean; dry_run: boolean; changes: PresetPlanRow[]; loosening?: number; applied?: string[]; restart_required?: string[] }>(
    svc, "/preset/recommended", { method: "POST", body: JSON.stringify({ dry_run, confirm }) }),
  audit: (svc: Svc, limit = 80) => C<{ service: string; rows: any[] }>(svc, `/audit?limit=${limit}`),
};

// ------------------------------------------------------------------ trades (read-only GETs of the two services)
export const tradesApi = {
  realTrade: async (days: number) => tradesFromRealTrade(await call<any>(getRealTradeApiUrl(), serviceToken("rt"), `/positions/REAL/records?days=${days}&limit=1000&include_imported=true`)),
  positionStocks: async () => tradesFromPositionStocks(await call<any>(getPositionStocksApiUrl(), serviceToken("ps"), "/trades/history?limit=1000")),
};

export const ACTIVE: RunStatus[] = ["queued", "running"];
