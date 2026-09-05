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

// The dashboard mirrors "is there a session" into its own React `loggedIn`
// state (so it can gate UI without re-reading localStorage on every render).
// That mirror used to only ever get set on an explicit login/logout click —
// but rtRequest can ALSO clear the token itself below, silently, from any
// background poll (pipeline/watchlist/positions) that happens to run after
// the session has expired. Without this hook the two go out of sync: the
// dashboard keeps showing "Admin session active" from stale state while
// every real request is now quietly failing with "Missing admin session
// token", and Run Cycle / Auto-Pilot clicks appear to do nothing. Any
// component that renders a logged-in/out banner should register here so it
// hears about an expiry the moment it happens, not on the next unrelated
// error.
let sessionExpiredHandler: (() => void) | null = null;
export function setSessionExpiredHandler(fn: (() => void) | null) {
  sessionExpiredHandler = fn;
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
    if (resp.status === 401) {
      const hadToken = !!getSessionToken();
      setSessionToken(null); // expired/invalid — drop it so the UI re-shows login
      if (hadToken) sessionExpiredHandler?.(); // only fire once, on the transition — not on every already-logged-out call
    }
    const detail = (data && data.detail) || resp.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data as T;
}

export interface ScheduledFeatureState {
  enabled: boolean;   // per-mode UI toggle (this DB row) — SOLE on/off authority
                       // (the separate server-side env_on kill-switch was
                       // removed 2026-09-01; no server-side flag exists anymore)
  time_ist: string;   // "HH:MM" IST the action is scheduled for
  last_run: string | null;  // "YYYY-MM-DD" it last fired, null if never
}

export interface GateStatus {
  mode: "DEMO" | "REAL";
  admin_authenticated: boolean;
  dhan_connected: boolean | null;
  risk_config_confirmed: boolean;
  armed: boolean;
  disarmed_reason: string | null;
  auto_pilot_enabled: boolean;
  scheduled_automation?: {
    prepick: ScheduledFeatureState;
    enter_at_open: ScheduledFeatureState;
    eod_squareoff: ScheduledFeatureState;
  };
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
    stale_data_seconds: number | null;
    max_tick_volatility_mult: number | null;
    allow_pyramiding: boolean | null;
    updated_at: string | null;
    updated_by: string | null;
  } | null;
}

export interface AuditLogRow {
  actor: string | null;
  action: string;
  detail: string | null;
  mode: string | null;
  occurred_at: string;
}

export interface DhanStatus {
  connected: boolean;
  client_id_masked: string | null;
  token_issued_at: string | null;
  token_expires_at: string | null;
  token_valid: boolean;
  token_hard_cap_hours: number | null;
  days_remaining: number | null;
  hours_remaining: number | null;
  seconds_remaining: number | null;
}

export interface Position {
  id: number; symbol: string; status: string; qty_open: number; avg_entry_price: number;
  current_stop: number | null; current_target: number | null;
  unrealized_pnl: number; realized_pnl: number; opened_at: string;
  current_price: number | null;
  pnl_pct: number | null;
  stop_distance_pct: number | null;
  target_distance_pct: number | null;
}

export interface OrderRow {
  id: number; symbol: string; side: string; qty: number; order_type: string;
  limit_price: number | null; status: string; valid_until: string | null; created_at: string;
  execution_source?: string;
  current_price: number | null;
  limit_distance_pct: number | null;
}

export interface CandidateDecision {
  action: string;
  reasoning: string | null;
  proposed_qty: number | null;
  proposed_price: number | null;
  proposed_stop: number | null;
  proposed_target: number | null;
  risk_verdict: string | null;
  risk_verdict_reason: string | null;
  evaluated_at: string;
  limit_distance_pct: number | null;
}

export interface CandidateRow {
  id: number;
  symbol: string;
  source_tab: string | null;
  decision_label: string | null;
  conviction_score: number | null;
  signal_price: number | null;
  received_at: string;
  consumed: boolean;
  fetch_count: number;
  current_price: number | null;
  latest_decision: CandidateDecision | null;
}

export interface CycleResult {
  mode: string;
  new_candidates: number;
  entry: { evaluated: number; entered: number; waited: number; rejected: number };
  fills: number;
  expired_orders: number;
  exit: { evaluated: number; held: number; trailed: number; partial_exits: number; full_exits: number; time_stops: number };
}

export interface EntryDetailRow {
  symbol: string;
  action: string;
  reasoning: string | null;
  risk_verdict: string | null;
}

export interface PipelineCycleRecord {
  trigger: "manual" | "autopilot";
  warning?: string | null;
  started_at: string;
  ended_at: string;
  duration_ms: number;
  stage_timings_ms: Record<string, number>;
  new_candidates: number | null;
  entered: number | null;
  waited: number | null;
  rejected: number | null;
  entry_details: EntryDetailRow[];
  fills: number | null;
  expired_orders: number | null;
  full_exits: number | null;
  partial_exits: number | null;
  auto_disarmed: string | null;
  error: string | null;
}

export interface PipelineStatus {
  mode: string;
  running: boolean;
  trigger?: "manual" | "autopilot";
  warning?: string | null;
  started_at?: string;
  stage?: string;
  stages?: string[];
  stage_elapsed_ms?: number;
  total_elapsed_ms?: number;
  current_symbol?: string | null;
  current_source?: string | null;
  symbols_done?: number;
  symbols_total?: number;
  stage_timings_ms?: Record<string, number>;
  last_cycle: PipelineCycleRecord | null;
  history: PipelineCycleRecord[];
}

// 2026-09-03 — catalyst watchlist (WatchlistEntry) + resilience status types.
export interface WatchlistEntry {
  id: number;
  symbol: string;
  catalyst_type: string;
  catalyst_price: number;
  catalyst_ts: string | null;
  horizon_class: string;
  entry_band_pct: number;
  source_tier: number;
  conviction_score: number | null;
  status: "active" | "entered" | "expired" | "missed" | string;
  missed_reason: string | null;
  expires_at: string | null;
}

export interface WatchlistEntriesResponse {
  mode: string;
  count: number;
  entries: WatchlistEntry[];
}

export interface CircuitBreakerStatus {
  name: string;
  state: "closed" | "open" | "half_open";
  consecutive_failures: number;
  failure_threshold: number;
  cooldown_s: number;
  seconds_until_retry: number | null;
}

export interface DynamicUniverseLast {
  mode: string;
  added: string[];
  removed: string[];
  kept: number;
  synced_at: string;
}

export interface ResilienceStatus {
  breakers: {
    api_gateway: CircuitBreakerStatus;
    market_data: CircuitBreakerStatus;
  };
  dynamic_universe_last: DynamicUniverseLast | null;
}

export interface ManualOrderRequest {
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  order_type?: "LIMIT" | "MARKET";
  limit_price?: number | null;
  product_type?: "CNC" | "MIS";
  stop_price?: number | null;
  target_price?: number | null;
  position_id?: number | null;
}

export interface ManualOrderResult {
  ok: boolean;
  mode?: string;
  symbol?: string;
  side?: "BUY" | "SELL";
  order_type?: string;
  product_type?: string;
  entry_price?: number;
  stop_price?: number;
  target_price?: number;
  qty_requested?: number;
  approved_qty?: number;
  risk_amount?: number;
  estimated_value?: number;
  risk_reward?: number | null;
  verdict?: string;
  check_name?: string;
  reason?: string;
  detail?: string;
  status?: string;
  filled?: boolean;
  order_id?: number;
  decision_id?: number;
  dhan_order_id?: string;
  position_id?: number;
  qty_available?: number;
  exit_price_estimate?: number;
  estimated_pnl?: number;
  pnl?: number;
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
    rtRequest<DhanStatus>(
      "/dhan/connect", { method: "POST", body: JSON.stringify({ client_id, access_token }) }
    ),

  dhanRegenerateToken: () =>
    rtRequest<DhanStatus>("/dhan/regenerate-token", { method: "POST" }),

  dhanStatus: () => rtRequest<DhanStatus>("/dhan/status"),

  dhanFunds: () => rtRequest<any>("/dhan/funds"),

  dhanAccount: () => rtRequest<DhanStatus & { funds: any | null; funds_error: string | null }>("/dhan/account"),
  dhanNetworkCheck: () =>
    rtRequest<{ outbound_ip: string | null; checked_at: string; note: string }>("/dhan/network-check"),

  updateRiskConfig: (mode: "DEMO" | "REAL", patch: Record<string, number | boolean>) =>
    rtRequest<{ ok: boolean }>("/risk-config", { method: "POST", body: JSON.stringify({ mode, ...patch }) }),

  confirmRiskConfig: (mode: "DEMO" | "REAL") =>
    rtRequest<{ ok: boolean }>(`/risk-config/${mode}/confirm`, { method: "POST" }),

  arm: (mode: "DEMO" | "REAL") => rtRequest<{ ok: boolean; armed: boolean; mode: string }>(`/arm/${mode}`, { method: "POST" }),

  disarm: (mode: "DEMO" | "REAL") => rtRequest<{ ok: boolean; armed: boolean; mode: string }>(`/disarm/${mode}`, { method: "POST" }),

  enableAutoPilot: (mode: "DEMO" | "REAL") =>
    rtRequest<{ ok: boolean; mode: string; auto_pilot_enabled: boolean }>(`/autopilot/${mode}/enable`, { method: "POST" }),

  disableAutoPilot: (mode: "DEMO" | "REAL") =>
    rtRequest<{ ok: boolean; mode: string; auto_pilot_enabled: boolean }>(`/autopilot/${mode}/disable`, { method: "POST" }),

  // Scheduled automation (2026-08-31) — flip one of the three time-of-day
  // features on/off for a mode. This toggle is the SOLE on/off authority
  // (the separate server-side env_on kill-switch was removed 2026-09-01 at
  // the admin's request) — flipping it here is immediately effective, no
  // Render env var / redeploy required.
  setFeature: (
    mode: "DEMO" | "REAL",
    feature: "prepick" | "enter_at_open" | "eod_squareoff",
    enabled: boolean,
  ) =>
    rtRequest<{ ok: boolean; mode: string; feature: string; enabled: boolean }>(
      `/features/${mode}`,
      { method: "POST", body: JSON.stringify({ feature, enabled }) },
      mode === "REAL",
    ),

  emergencyPause: () => rtRequest<{ ok: boolean; paused: boolean }>("/emergency-pause", { method: "POST" }),

  riskEngineCheck: (body: { mode: string; symbol: string; side: string; qty: number; entry_price: number; stop_price: number }) =>
    rtRequest<{ verdict: string; check_name: string; reason: string; approved_qty: number | null }>(
      "/risk-engine/check", { method: "POST", body: JSON.stringify(body) }
    ),

  auditLog: (mode?: "DEMO" | "REAL", limit = 50) =>
    rtRequest<AuditLogRow[]>(`/audit-log?limit=${limit}${mode ? `&mode=${mode}` : ""}`),

  sendManualCandidate: (
    mode: "DEMO" | "REAL",
    body: { symbol: string; decision_label?: string; conviction_score?: number; signal_price?: number }
  ) =>
    rtRequest<{ ok: boolean; mode: string; symbol: string; queued: boolean }>(
      `/candidates/manual/${mode}`,
      { method: "POST", body: JSON.stringify(body) },
      mode === "REAL"
    ),

  runCycle: (mode: "DEMO" | "REAL") =>
    rtRequest<CycleResult>(`/cycle/run/${mode}`, { method: "POST" }, mode === "REAL"),

  pipelineStatus: (mode: "DEMO" | "REAL") =>
    rtRequest<PipelineStatus>(`/pipeline/status/${mode}`, {}, mode === "REAL"),

  // 2026-09-03 — catalyst watchlist (trade_watchlist / WatchlistEntry) and
  // resilience status (circuit breakers + last dynamic-universe sync).
  // Both read-only/observational, same rtRequest pattern as everything else.
  watchlistEntries: (mode: "DEMO" | "REAL", status?: string) =>
    rtRequest<WatchlistEntriesResponse>(
      `/watchlist-entries/${mode}${status ? `?status=${status}` : ""}`,
      {},
      mode === "REAL"
    ),

  resilienceStatus: () =>
    rtRequest<ResilienceStatus>(`/resilience/status`, {}, true),

  positions: (mode: "DEMO" | "REAL") =>
    rtRequest<Position[]>(`/positions/${mode}`, {}, mode === "REAL"),

  orders: (mode: "DEMO" | "REAL", limit = 50) =>
    rtRequest<OrderRow[]>(`/orders/${mode}?limit=${limit}`, {}, mode === "REAL"),

  candidates: (mode: "DEMO" | "REAL", limit = 40) =>
    rtRequest<CandidateRow[]>(`/candidates/${mode}?limit=${limit}`, {}, mode === "REAL"),

  closePosition: (mode: "DEMO" | "REAL", positionId: number, qty?: number) =>
    rtRequest<{ ok: boolean; symbol: string; qty_closed?: number; qty_sent?: number; pnl?: number; status?: string }>(
      `/positions/${mode}/${positionId}/close`,
      { method: "POST", body: JSON.stringify({ qty: qty ?? null }) },
      mode === "REAL"
    ),

  cancelOrder: (mode: "DEMO" | "REAL", orderId: number) =>
    rtRequest<{ ok: boolean; order_id: number; status: string }>(
      `/orders/${mode}/${orderId}/cancel`, { method: "POST" }, mode === "REAL"
    ),

  reconcile: (mode: "DEMO" | "REAL") =>
    rtRequest<{ ok: boolean; checked?: number; entries_filled?: number; exits_confirmed?: number; dead_orders?: number; errors?: number; positions_unstuck?: number; holdings_imported?: number; note?: string }>(
      `/reconcile/${mode}`, { method: "POST" }, mode === "REAL"
    ),

  // Manual Execution Gateway — see manual_engine.py. previewManualOrder
  // never writes anything server-side (safe to call on every ticket
  // change); confirmManualOrder is the one that actually places/fills
  // the order, so only ever call it from the ticket's Confirm button,
  // after the person has seen the preview.
  previewManualOrder: (mode: "DEMO" | "REAL", body: ManualOrderRequest) =>
    rtRequest<ManualOrderResult>(`/manual-order/${mode}/preview`, { method: "POST", body: JSON.stringify(body) }, mode === "REAL"),

  confirmManualOrder: (mode: "DEMO" | "REAL", body: ManualOrderRequest) =>
    rtRequest<ManualOrderResult>(`/manual-order/${mode}/confirm`, { method: "POST", body: JSON.stringify(body) }, mode === "REAL"),
  // Live Dhan broker data — require admin auth, REAL mode only
  dhanLivePositions: () =>
    rtRequest<{ ok: boolean; positions: any[] }>("/dhan/positions"),

  dhanLiveHoldings: () =>
    rtRequest<{ ok: boolean; holdings: any[] }>("/dhan/holdings"),

  dhanLiveOrders: () =>
    rtRequest<{ ok: boolean; orders: any[] }>("/dhan/orders"),

};
