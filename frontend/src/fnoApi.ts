// frontend/src/fnoApi.ts  (F&O options page + the F&O part of Real Money Calibration)
//
// Client for fno-options-service (nginx: /fno-api/). Same admin session as Auto Trade / Position Stocks: the service only VERIFIES the real-trade or
// position-stocks token, it never issues one. Nothing here places an order except fnoApi.squareoff / arm / disarm, which are explicit buttons.
import { getRealTradeApiUrl, getSessionToken as getRtToken } from "./realTradeApi";
import { getPositionStocksApiUrl, getSessionToken as getPsToken } from "./positionStocksApi";

const URL_KEY = "stockky:fno_api_url";

export function getFnoApiUrl(): string {
  try { const s = localStorage.getItem(URL_KEY); if (s) return s; } catch { /* storage blocked */ }
  const env = ((import.meta as any).env?.VITE_FNO_URL || "").replace(/\/$/, "");
  if (env) return env;
  const other = getRealTradeApiUrl() || getPositionStocksApiUrl();
  const m = /\/(realtrade|positionstocks)$/.exec(other || "");
  return m ? other.replace(/\/(realtrade|positionstocks)$/, "/fno-api") : "/fno-api";
}
export function setFnoApiUrl(url: string) {
  const clean = url.trim().replace(/\/$/, "");
  try { if (clean) localStorage.setItem(URL_KEY, clean); else localStorage.removeItem(URL_KEY); } catch { /* ignore */ }
}
export function fnoToken(): string | null { return getRtToken() || getPsToken(); }

export class FnoError extends Error {
  status: number;
  constructor(status: number, msg: string) { super(msg); this.status = status; }
}

export async function fnoCall<T>(path: string, init?: RequestInit, base = getFnoApiUrl()): Promise<T> {
  const token = fnoToken();
  if (!token) throw new FnoError(401, "Admin login required (same password as Auto Trade).");
  let resp: Response;
  try {
    resp = await fetch(`${base}${path}`, { ...init, headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}`, ...(init?.headers as Record<string, string> | undefined) } });
  } catch (e: any) { throw new FnoError(0, `Cannot reach ${base || "(no URL)"}: ${e?.message || e}`); }
  const raw = await resp.text();
  let data: any = null;
  if (raw && raw.trim()) { try { data = JSON.parse(raw); } catch { data = raw; } }
  if (!resp.ok) throw new FnoError(resp.status, data && data.detail !== undefined ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : resp.statusText);
  if (typeof data === "string" && /^\s*<(!doctype|html)/i.test(data)) throw new FnoError(502, `${base}${path} answered with a web page: the nginx /fno-api/ route is missing (install deploy/nginx-stockky.conf).`);
  return data as T;
}

// ---- types (mirror services/fno-options-service/main.py) ----
export interface FnoLocks { engine_enabled: boolean; mode: string; real_enabled: boolean; armed_today: boolean; broker_configured: boolean; real_orders_possible: boolean; closed_locks: string[] }
export interface FnoPosition {
  id: number | null; underlying: string; expiry: string; strike: number; opt_type: "CE" | "PE"; lot_size: number; lots: number; entry_ts: number; entry_premium: number;
  sl_price: number; target_price: number; mode: string; status: "OPEN" | "CLOSED"; max_premium: number; trail_armed: boolean; trail_stop: number | null;
  exit_ts: number | null; exit_premium: number | null; exit_reason: string; gross_pnl: number | null; charges: number | null; net_pnl: number | null;
  signal_score: number | null; spot_at_entry: number | null; note: string; label: string; strategy?: string; parent_id?: number | null; premium?: number | null; unrealized?: number | null;
}
export interface FnoDay { trades: number; open: number; realized_net: number; realized_gross: number; charges: number; wins: number; losses: number }
export interface FnoOverview {
  now: string; market_open: boolean; locks: FnoLocks; day: FnoDay; open_positions: FnoPosition[];
  last_tick: { ts?: number; ok?: boolean; error?: string; [k: string]: any }; runner: { alive: boolean; error: string | null; snapshots_today: number }; underlyings: string[]; params: Record<string, any>;
}
export interface FnoStrategyRow {
  key: string; label: string; ad: string | null; sides: string; rule: string; flag: string; enabled: boolean; expiry_day_only?: boolean;
  today: { entries: number; open: number; closed: number; net: number };
  recent: { days: number; trades: number; net: number; win_rate_pct: number; profit_factor: number | null; max_drawdown: number };
}
export interface FnoStrategies {
  mode: string; strategies: FnoStrategyRow[]; not_built: { ad: string; why: string }[]; max_per_day: number; note: string;
  expiry_rules?: { entry_end: string; squareoff_at: string; risk_mult: number; real_allowed: boolean };
}
export interface FnoSide { security_id?: number; ltp?: number; iv?: number; delta?: number; theta?: number; oi?: number; prev_oi?: number; volume?: number; bid?: number; ask?: number }
export interface FnoChainDoc {
  chain: { underlying: string; expiry: string; spot: number; strikes: { strike: number; ce: FnoSide | null; pe: FnoSide | null }[] };
  summary: Record<string, any>; bias: { direction: string; score: number; reasons?: string[]; parts?: Record<string, number> }; expiries: string[]; age_s: number;
}
export interface FnoEvent { id: number; ts: number; kind: string; underlying: string; [k: string]: any }
export interface FnoBacktestRow { name: string; kind: string; underlying: string; from?: string; to?: string; days?: number; trades?: number; net?: number; win_rate_pct?: number; max_drawdown?: number; verdict?: string }
export interface FnoBacktestList { results: FnoBacktestRow[]; jobs: { id: string; kind: string; state: string; file?: string; trades?: number; verdict?: string; error?: string }[]; recorded_days: string[]; snapshot_days: any[] }
export interface FnoStep { step: string; ok: boolean | null; detail: string }

export const fnoApi = {
  overview: () => fnoCall<FnoOverview>("/fno/overview"),
  strategies: () => fnoCall<FnoStrategies>("/fno/strategies"),
  pipeline: () => fnoCall<{ steps: FnoStep[]; locks: FnoLocks }>("/fno/pipeline"),
  chain: (underlying: string, expiry = "") => fnoCall<FnoChainDoc>(`/fno/chain?underlying=${underlying}${expiry ? `&expiry=${encodeURIComponent(expiry)}` : ""}`),
  signals: (limit = 100) => fnoCall<{ events: FnoEvent[] }>(`/fno/signals?limit=${limit}`),
  log: (limit = 200) => fnoCall<{ events: FnoEvent[] }>(`/fno/log?limit=${limit}`),
  positions: (status = "all", mode?: string) => fnoCall<{ positions: FnoPosition[] }>(`/fno/positions?status=${status}${mode ? `&mode=${mode}` : ""}`),
  history: (days = 30, mode = "DEMO") => fnoCall<{ metrics: any; verdict: { status: string; reasons?: string[]; [k: string]: any }; trades: FnoPosition[] }>(`/fno/history?days=${days}&mode=${mode}`),
  charges: (days = 30, mode = "DEMO") => fnoCall<{ totals: Record<string, number>; trades: any[]; rates: Record<string, number>; note: string }>(`/fno/charges?days=${days}&mode=${mode}`),
  reconcile: (refresh = false) => fnoCall<{ ts: number | null; ok: boolean | null; note?: string; differences?: { security_id: string; ours: number; broker: number }[] }>(`/fno/reconcile${refresh ? "?refresh=true" : ""}`),
  squareoff: () => fnoCall<{ closed: any }>("/fno/squareoff", { method: "POST" }),
  arm: () => fnoCall<{ armed_day: string; credentials?: string; locks: FnoLocks }>("/fno/arm", { method: "POST" }),
  disarm: () => fnoCall<{ locks: FnoLocks }>("/fno/disarm", { method: "POST" }),
  backtests: () => fnoCall<FnoBacktestList>("/fno/backtests"),
  backtest: (name: string) => fnoCall<any>(`/fno/backtests/${encodeURIComponent(name)}`),
  runBacktest: (kind: "replay" | "sim", underlyings: string[], overrides: Record<string, any> = {}, days?: string[], strategy = "CORE") =>
    fnoCall<{ job: string }>("/fno/backtests/run", { method: "POST", body: JSON.stringify({ kind, underlyings, overrides, days, strategy }) }),
  controlFlags: () => fnoCall<any>("/control/flags"),
};
