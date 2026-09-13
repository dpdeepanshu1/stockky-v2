// frontend/src/positionStocksApi.ts
//
// Client for position-stocks-service — deliberately separate from both
// api.ts (api-gateway) and realTradeApi.ts (real-trade-service). Same
// "different trust boundary, different blast radius" reasoning as
// realTradeApi.ts: position-stocks-service is its own container, its own
// event loop, its own capital pool, and its own kill switch — a bug in one
// service's frontend wiring should never be able to reach another
// service's real-money controls through a shared client.

const STORAGE_URL_KEY = "stockky:position_stocks_api_url";
// Deliberately a SEPARATE localStorage key from real-trade-service's
// "stockky:real_trade_session_token" — same admin password, same
// ADMIN_PASSWORD_HASH/SESSION_SECRET on the backend, but each service's
// session token is issued (and expires) independently, matching the two
// services' separate trust boundaries.
const STORAGE_TOKEN_KEY = "stockky:position_stocks_session_token";

export function getPositionStocksApiUrl(): string {
  const stored = localStorage.getItem(STORAGE_URL_KEY);
  if (stored) return stored;
  return (import.meta.env.VITE_POSITION_STOCKS_URL || "").replace(/\/$/, "");
}

export function setPositionStocksApiUrl(url: string) {
  const clean = url.trim().replace(/\/$/, "");
  if (clean) localStorage.setItem(STORAGE_URL_KEY, clean);
  else localStorage.removeItem(STORAGE_URL_KEY);
}

export function getSessionToken(): string | null {
  return localStorage.getItem(STORAGE_TOKEN_KEY);
}

export function setSessionToken(token: string | null) {
  if (token) localStorage.setItem(STORAGE_TOKEN_KEY, token);
  else localStorage.removeItem(STORAGE_TOKEN_KEY);
}

// Mirrors realTradeApi.ts's sessionExpiredHandler hook — lets the dashboard
// hear about a token expiring mid-poll (not just on an explicit login/logout
// click) so "Admin session active" never goes stale against reality.
let sessionExpiredHandler: (() => void) | null = null;
export function setSessionExpiredHandler(fn: (() => void) | null) {
  sessionExpiredHandler = fn;
}

async function psRequest<T>(path: string, init?: RequestInit, requireAuth = false): Promise<T> {
  const base = getPositionStocksApiUrl();
  if (!base) {
    throw new Error("Position Stocks service URL isn't set. Open Position Stocks → Settings and paste its URL.");
  }
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (requireAuth) {
    const token = getSessionToken();
    if (!token) {
      throw new Error("Admin login required. Log in above with the same admin password used for Real Automatic Trade.");
    }
    headers["Authorization"] = `Bearer ${token}`;
  }
  const resp = await fetch(`${base}${path}`, { ...init, headers });
  const raw = await resp.text();
  let data: any = null;
  if (raw && raw.trim()) {
    try {
      data = JSON.parse(raw);
    } catch {
      throw new Error(`${resp.status} ${resp.statusText}: ${raw.slice(0, 150)}`);
    }
  }
  if (!resp.ok) {
    if (resp.status === 401 && requireAuth) {
      setSessionToken(null);
      sessionExpiredHandler?.();
    }
    const detail = (data && data.detail) || resp.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export interface ScalpStatus {
  armed: boolean;
  armed_at: string | null;
  service_enabled: boolean;
  auto_pilot_enabled: boolean;
  last_cycle_run_at: string | null;
  last_cycle_run_trigger: "AUTO" | "MANUAL" | null;
  first_live_order_done: boolean;
  orders_placed_today: number;
  daily_loss_kill_switch: boolean;
  eod_squareoff_fired_date: string | null;
  circuit_breaker: { state: "closed" | "open" | "half_open"; consecutive_failures: number; failure_threshold: number; cooldown_s: number; seconds_until_retry: number | null };
  ws: { connected: boolean; subscribed_symbols?: number; last_tick_at?: string | null; reconnect_attempts?: number };
  market_open: boolean;
  risk_per_trade_pct: number;
  risk_confirmed: boolean;
  max_daily_loss_pct_of_pool: number;
  max_concurrent_scalp_positions: number;
  scalp_pool_capital_share_pct: number;
}

// Same shape as real-trade-service's DhanStatus — both services show a
// Dhan Account card off the one shared account/token, see backend
// auth/dhan_credentials_ro.connection_status docstring.
export interface DhanAccountStatus {
  connected: boolean;
  client_id_masked: string | null;
  token_issued_at: string | null;
  token_expires_at: string | null;
  token_valid: boolean;
  token_hard_cap_hours: number | null;
  days_remaining: number | null;
  hours_remaining: number | null;
  seconds_remaining: number | null;
  funds: Record<string, unknown> | null;
  funds_error: string | null;
}

export interface ScalpPositionRow {
  id: number;
  symbol: string;
  status: "OPEN" | "TARGET_HIT" | "STOP_HIT" | "EOD_SQUAREOFF" | "MANUAL_EXIT" | "ERROR";
  window_source: "1m" | "5m" | "15m" | "60m";
  entry_price: number;
  exit_price: number | null;
  quantity: number;
  target_price: number;
  stop_price: number;
  adaptive_target_pct: number;
  adaptive_stop_pct: number;
  realized_pnl: number | null;
  realized_pnl_pct: number | null;
  opened_at: string;
  closed_at: string | null;
  is_first_live_order: boolean;
  dhan_super_order_id: string | null;
}

export interface ScalpCandidateRow {
  symbol: string;
  window: "1m" | "5m" | "15m" | "60m";
  pct_change: number;
  current_ltp: number;
  composite_score: number;
  tick_activity: number;
}

export interface ScalpLedgerState {
  total_allocated_capital: number;
  available_capital: number;
  realized_pnl_today: number;
  realized_pnl_total: number;
  last_synced_from_broker_at: string | null;
  daily_loss_kill_switch_tripped: boolean;
}

export interface ScalpCycleResult {
  status: string;
  reconciled: number;
  eod_fired: boolean;
  candidates_seen: number;
  entered_symbol: string | null;
  skipped_reason: string | null;
}

export interface ScalpTradeHistorySummary {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  total_pnl: number;
  best_trade: { symbol: string; pnl: number } | null;
  worst_trade: { symbol: string; pnl: number } | null;
}

export interface ScalpTradeRow {
  id: number;
  symbol: string;
  status: string;
  window_source: string;
  entry_price: number;
  exit_price: number | null;
  quantity: number;
  realized_pnl: number | null;
  realized_pnl_pct: number | null;
  opened_at: string;
  closed_at: string | null;
  dhan_super_order_id: string | null;
}

export interface ScalpTradeHistory {
  summary: ScalpTradeHistorySummary;
  trades: ScalpTradeRow[];
}

export interface DhanLiveOrder {
  orderId?: string;
  tradingSymbol?: string;
  transactionType?: string;
  orderStatus?: string;
  legName?: string;
  quantity?: number;
  price?: number;
  triggerPrice?: number;
  [key: string]: unknown; // pass through whatever else Dhan returns, unfiltered
}

export interface DhanLiveOrders {
  count: number;
  orders: DhanLiveOrder[];
}

export const positionStocksApi = {
  health: () => psRequest<{ status: string; service: string }>("/health"),

  // Same admin username/password as Real Automatic Trade — verified against
  // the SAME ADMIN_PASSWORD_HASH/ADMIN_USERNAME env this service now shares
  // with real-trade-service (see backend auth/admin_auth.py).
  login: (username: string, password: string) =>
    psRequest<{ token: string; expires_at: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  logout: () => psRequest<{ status: string }>("/auth/logout", { method: "POST" }, true),

  status: () => psRequest<ScalpStatus>("/status"),

  // Every route below that mutates state now requires admin login
  // (backend session 8: auth/admin_auth.py) — requireAuth=true attaches
  // the Bearer token and throws a clear "log in" error if there isn't one,
  // instead of the request silently 401-ing with no explanation.
  arm: () => psRequest<{ status: string }>("/arm", { method: "POST" }, true),
  disarm: () => psRequest<{ status: string }>("/disarm", { method: "POST" }, true),
  kill: () => psRequest<{ status: string }>("/kill", { method: "POST" }, true),

  serviceEnable: () => psRequest<{ status: string }>("/service/enable", { method: "POST" }, true),
  serviceDisable: () => psRequest<{ status: string }>("/service/disable", { method: "POST" }, true),

  autopilotEnable: () => psRequest<{ status: string }>("/autopilot/enable", { method: "POST" }, true),
  autopilotDisable: () => psRequest<{ status: string }>("/autopilot/disable", { method: "POST" }, true),
  runCycle: () => psRequest<ScalpCycleResult>("/cycle/run", { method: "POST" }, true),

  positions: () => psRequest<ScalpPositionRow[]>("/positions"),
  tradeHistory: (limit = 200) => psRequest<ScalpTradeHistory>(`/trades/history?limit=${limit}`),
  candidates: () => psRequest<ScalpCandidateRow[]>("/candidates"),

  ledger: () => psRequest<ScalpLedgerState>("/ledger"),
  syncLedger: () => psRequest<{ status: string; total_allocated_capital: number }>("/ledger/sync", { method: "POST" }, true),

  wsStatus: () => psRequest<{ connected: boolean; subscribed_symbols?: number; last_tick_at?: string | null; reconnect_attempts?: number }>("/ws-status"),
  dhanLiveOrders: () => psRequest<DhanLiveOrders>("/dhan/live-orders"),
  dhanAccount: () => psRequest<DhanAccountStatus>("/dhan/account", {}, true),

  reconcile: () => psRequest<{ status: string; positions_closed: number }>("/reconcile", { method: "POST" }, true),
};
