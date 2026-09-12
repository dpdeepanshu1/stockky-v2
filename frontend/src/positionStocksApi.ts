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

async function psRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const base = getPositionStocksApiUrl();
  if (!base) {
    throw new Error("Position Stocks service URL isn't set. Open Position Stocks → Settings and paste its URL.");
  }
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
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
    const detail = (data && data.detail) || resp.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export interface ScalpStatus {
  armed: boolean;
  armed_at: string | null;
  service_enabled: boolean;
  first_live_order_done: boolean;
  orders_placed_today: number;
  daily_loss_kill_switch: boolean;
  eod_squareoff_fired_date: string | null;
  circuit_breaker: { state: "closed" | "open" | "half_open"; consecutive_failures: number; failure_threshold: number; cooldown_s: number; seconds_until_retry: number | null };
  ws: { connected: boolean; subscribed_symbols?: number; last_tick_at?: string | null; reconnect_attempts?: number };
  market_open: boolean;
  risk_per_trade_pct: number;
  risk_confirmed: boolean;
}

export interface ScalpPositionRow {
  id: number;
  symbol: string;
  status: "OPEN" | "TARGET_HIT" | "STOP_HIT" | "EOD_SQUAREOFF" | "MANUAL_EXIT" | "ERROR";
  window_source: "1m" | "5m" | "15m" | "60m";
  entry_price: number;
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

export const positionStocksApi = {
  health: () => psRequest<{ status: string; service: string }>("/health"),

  status: () => psRequest<ScalpStatus>("/status"),

  arm: () => psRequest<{ status: string }>("/arm", { method: "POST" }),
  disarm: () => psRequest<{ status: string }>("/disarm", { method: "POST" }),
  kill: () => psRequest<{ status: string }>("/kill", { method: "POST" }),

  serviceEnable: () => psRequest<{ status: string }>("/service/enable", { method: "POST" }),
  serviceDisable: () => psRequest<{ status: string }>("/service/disable", { method: "POST" }),

  positions: () => psRequest<ScalpPositionRow[]>("/positions"),
  candidates: () => psRequest<ScalpCandidateRow[]>("/candidates"),

  ledger: () => psRequest<ScalpLedgerState>("/ledger"),
  syncLedger: () => psRequest<{ status: string; total_allocated_capital: number }>("/ledger/sync", { method: "POST" }),

  wsStatus: () => psRequest<{ connected: boolean; subscribed_symbols?: number; last_tick_at?: string | null; reconnect_attempts?: number }>("/ws-status"),

  reconcile: () => psRequest<{ status: string; positions_closed: number }>("/reconcile", { method: "POST" }),
};
