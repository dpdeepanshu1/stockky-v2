// frontend/src/realTradeApi.ts
//
// Separate client for real-trade-service — intentionally NOT merged into
// api.ts. That file talks to api-gateway (read-only recommendations,
// no auth). This one talks to a different Render service that holds an
// admin session token and (in REAL mode) a live brokerage connection —
// keeping the request path physically separate matches the "different
// trust boundary, different blast radius" principle the whole real-trade
// architecture is built around.

const STORAGE_URL_KEY = "stockky:real_trade_api_url";
const STORAGE_TOKEN_KEY = "stockky:real_trade_session_token";

export function getRealTradeApiUrl(): string {
  const stored = localStorage.getItem(STORAGE_URL_KEY);
  if (stored) return stored;
  return (import.meta.env.VITE_REAL_TRADE_URL || "").replace(/\/$/, "");
}

export function setRealTradeApiUrl(url: string) {
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

async function rtRequest<T>(path: string, init?: RequestInit, requireAuth = true): Promise<T> {
  const base = getRealTradeApiUrl();
  if (!base) {
    throw new Error("Real Trade service URL isn't set. Open Real Automatic Trade → Settings and paste its URL.");
  }
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string> | undefined),
  };
  if (requireAuth) {
    const token = getSessionToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
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
    if (resp.status === 401) setSessionToken(null); // expired/invalid — drop it so the UI re-shows login
    const detail = (data && data.detail) || resp.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export interface GateStatus {
  mode: "DEMO" | "REAL";
  admin_authenticated: boolean;
  dhan_connected: boolean | null;
  risk_config_confirmed: boolean;
  armed: boolean;
  disarmed_reason: string | null;
  account: {
    starting_capital: number | null;
    current_equity: number | null;
    cash_available: number | null;
    realized_pnl_today: number | null;
  };
  risk_config: {
    risk_per_trade_pct: number | null;
    max_daily_loss_pct: number | null;
    max_concurrent_positions: number | null;
    max_portfolio_risk_pct: number | null;
  } | null;
}

export interface AuditLogRow {
  actor: string | null;
  action: string;
  detail: string | null;
  mode: string | null;
  occurred_at: string;
}

export const realTradeApi = {
  health: () => rtRequest<{ ok: boolean; service: string; phase: string }>("/health", {}, false),

  gateStatus: (mode: "DEMO" | "REAL") => rtRequest<GateStatus>(`/status/${mode}`, {}, false),

  login: (username: string, password: string) =>
    rtRequest<{ token: string; expires_at: string }>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }, false),

  logout: () => rtRequest<{ ok: boolean }>("/auth/logout", { method: "POST" }),

  connectDhan: (client_id: string, access_token: string) =>
    rtRequest<{ connected: boolean; client_id_masked: string | null; token_expires_at: string | null; token_valid: boolean }>(
      "/dhan/connect", { method: "POST", body: JSON.stringify({ client_id, access_token }) }
    ),

  dhanStatus: () =>
    rtRequest<{ connected: boolean; client_id_masked: string | null; token_expires_at: string | null; token_valid: boolean }>(
      "/dhan/status"
    ),

  dhanFunds: () => rtRequest<any>("/dhan/funds"),

  updateRiskConfig: (mode: "DEMO" | "REAL", patch: Record<string, number | boolean>) =>
    rtRequest<{ ok: boolean }>("/risk-config", { method: "POST", body: JSON.stringify({ mode, ...patch }) }),

  confirmRiskConfig: (mode: "DEMO" | "REAL") =>
    rtRequest<{ ok: boolean }>(`/risk-config/${mode}/confirm`, { method: "POST" }),

  arm: (mode: "DEMO" | "REAL") => rtRequest<{ ok: boolean; armed: boolean; mode: string }>(`/arm/${mode}`, { method: "POST" }),

  disarm: (mode: "DEMO" | "REAL") => rtRequest<{ ok: boolean; armed: boolean; mode: string }>(`/disarm/${mode}`, { method: "POST" }),

  emergencyPause: () => rtRequest<{ ok: boolean; paused: boolean }>("/emergency-pause", { method: "POST" }),

  riskEngineCheck: (body: { mode: string; symbol: string; side: string; qty: number; entry_price: number; stop_price: number }) =>
    rtRequest<{ verdict: string; check_name: string; reason: string; approved_qty: number | null }>(
      "/risk-engine/check", { method: "POST", body: JSON.stringify(body) }
    ),

  auditLog: (mode?: "DEMO" | "REAL", limit = 50) =>
    rtRequest<AuditLogRow[]>(`/audit-log?limit=${limit}${mode ? `&mode=${mode}` : ""}`),
};
