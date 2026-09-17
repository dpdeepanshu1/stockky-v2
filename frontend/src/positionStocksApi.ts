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
  orders_placed_today_budget: number;
  daily_loss_kill_switch: boolean;
  eod_squareoff_fired_date: string | null;
  // AUDIT ADD (this session): non-zero only after today's EOD squareoff has
  // already run AND at least one position is still OPEN — i.e. the forced
  // 3pm flatten failed for it (no notification channel exists in this
  // service to alert on that any other way — see main.py's GET /status).
  eod_squareoff_stragglers: number;
  // AUDIT ADD (this session): backend has always returned this (capital/
  // shared_order_budget.py's status()) but no frontend field ever typed or
  // rendered it — see PositionStocksTab.tsx's System Health grid.
  shared_order_budget: { used_today: number; budget: number; remaining: number };
  circuit_breaker: { state: "closed" | "open" | "half_open"; consecutive_failures: number; failure_threshold: number; cooldown_s: number; seconds_until_retry: number | null };
  ws: { connected: boolean; subscribed_symbols?: number; last_tick_at?: string | null; reconnect_attempts?: number };
  market_open: boolean;
  risk_per_trade_pct: number;
  risk_confirmed: boolean;
  max_daily_loss_pct_of_pool: number;
  max_concurrent_scalp_positions: number;
  scalp_pool_capital_share_pct: number;
  // AUDIT ADD (this session): live scan/quality-gate config for the
  // Pipeline dashboard — see backend main.py's /status docstring.
  pipeline_config?: {
    scan_interval_s: number;
    windows: { "1m": number; "5m": number; "15m": number; "60m": number };
    min_avg_volume: number;
    quality_gate_enabled: boolean;
    quality_gate_top_n: number;
    min_fundamental_score: number;
    min_technical_score: number;
    min_market_cap_cr: number;
    scalp_product_type: string;
  };
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
  status: "OPEN" | "TARGET_HIT" | "STOP_HIT" | "EOD_SQUAREOFF" | "MANUAL_EXIT" | "EXIT_LEGS_REJECTED" | "ERROR";
  window_source: "1m" | "5m" | "15m" | "60m" | "MANUAL";
  entry_price: number;
  exit_price: number | null;
  quantity: number;
  target_price: number;
  stop_price: number;
  adaptive_target_pct: number;
  adaptive_stop_pct: number;
  realized_pnl: number | null;
  realized_pnl_pct: number | null;
  // AUDIT FIX (session60): live fields, OPEN positions only — None/absent
  // for closed rows or a symbol this service's feed hasn't ticked yet.
  current_price: number | null;
  current_amount: number | null;
  unrealized_pnl: number | null;
  unrealized_pnl_pct: number | null;
  target_distance_pct: number | null;
  stop_distance_pct: number | null;
  opened_at: string;
  closed_at: string | null;
  is_first_live_order: boolean;
  dhan_super_order_id: string | null;
  dhan_entry_order_id: string | null;
  dhan_exit_order_id: string | null;
}

export interface ScalpCandidateRow {
  symbol: string;
  window: "1m" | "5m" | "15m" | "60m";
  pct_change: number;
  current_ltp: number;
  composite_score: number;
  tick_activity: number;
}

// AUDIT FIX (this session): GET /candidates was changed in an earlier
// session to wrap the candidate list in an object ({market_open, count,
// candidates: [...]}) so callers can tell "no candidates because market
// is closed" apart from "no candidates because nothing is moving" — but
// this client's candidates() method (and every caller of it) was never
// updated to match, and kept typing/treating the response as a bare
// ScalpCandidateRow[]. At runtime the response is this object, not an
// array, so any caller doing array methods (e.g. .filter()) directly on
// it throws. This is the real shape now.
export interface ScalpCandidatesResponse {
  market_open: boolean;
  count: number;
  candidates: ScalpCandidateRow[];
}

export interface ScalpLedgerState {
  total_allocated_capital: number;
  available_capital: number;
  realized_pnl_today: number;
  realized_pnl_total: number;
  last_synced_from_broker_at: string | null;
  daily_loss_kill_switch_tripped: boolean;
}

// AUDIT ADD (this session): backend now returns a full stage-by-stage
// breakdown of what happened during the cycle — how long each stage took
// and which stock(s) it looked at / decided on. See main.py::_run_cycle's
// docstring for why (requested for the "Run Cycle Now" button specifically).
export interface ScalpCycleStageCandidate {
  symbol: string;
  window: "1m" | "5m" | "15m" | "60m";
  pct_change: number;
  current_ltp: number;
  composite_score: number;
}

export interface ScalpCycleStageChecked {
  symbol: string;
  window: "1m" | "5m" | "15m" | "60m";
  passed: boolean;
  reason: string | null;
  fundamental_score: number | null;
  technical_score: number | null;
  market_cap_cr: number | null;
}

export interface ScalpCycleStage {
  name: string;
  label: string;
  duration_ms: number;
  detail?: string;
  symbol?: string;
  candidates?: ScalpCycleStageCandidate[];
  checked?: ScalpCycleStageChecked[];
}

export interface ScalpCycleResult {
  status: string;
  trigger: "AUTO" | "MANUAL";
  started_at: string;
  total_duration_ms: number;
  reconciled: number;
  eod_fired: boolean;
  candidates_seen: number;
  entered_symbol: string | null;
  skipped_reason: string | null;
  stages: ScalpCycleStage[];
}

// ADDED (session48): shape returned by GET /pipeline/status — see
// pipeline_status.py's module docstring on the backend for why this
// didn't exist before.
export interface ScalpPipelineStatus {
  running: boolean;
  trigger: "AUTO" | "MANUAL" | null;
  started_at: string | null;
  stage: string | null;
  stage_label: string | null;
  stage_started_at: string | null;
  candidates: ScalpCycleStageCandidate[];
  last_cycle: ScalpCycleResult | null;
}


export interface ScalpTradeHistorySummary {
  total_trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number | null;
  total_pnl: number;
  // AUDIT FIX (this session): backend's GET /trades/history has included
  // opened_at on best_trade/worst_trade since the "missing opened_at"
  // fix (see main.py's trades_history docstring) so the dashboard could
  // correlate the summary card with a row in the trades table below —
  // but this type never grew the field to match, so it was invisible to
  // any future caller relying on the type instead of reading main.py.
  // Not currently rendered (best/worst only show symbol+pnl today), but
  // the type should reflect what the backend actually sends.
  best_trade: { symbol: string; pnl: number; opened_at: string } | null;
  worst_trade: { symbol: string; pnl: number; opened_at: string } | null;
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
  dhan_entry_order_id: string | null;
  dhan_exit_order_id: string | null;
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

// Backend session 12: GET /candidates/log — the natural follow-up flagged in
// STATUS.md's Next Steps since session 6, now built. Surfaces why a candidate
// was entered/skipped, including the quality-gate's fund/tech/catalyst fields.
export interface ScalpCandidateLogRow {
  id: number;
  symbol: string;
  window_source: "1m" | "5m" | "15m" | "60m" | "MANUAL";
  pct_change: number;
  composite_score: number | null;
  decision: "ENTERED" | "SKIPPED";
  reason: string | null;
  fundamental_score: number | null;
  technical_score: number | null;
  market_cap_cr: number | null;
  has_positive_catalyst: boolean | null;
  created_at: string;
}

// Learned intraday-restricted symbol list — seeded from live Dhan SELL
// rejections this service has actually observed (orders/eod_squareoff.py,
// orders/entry.py), then filtered out of new candidates every cycle before
// capital or a Dhan call is committed to them. See screening/
// intraday_eligibility.py and GET /candidates/restricted for the backend
// side; this is a pure read, no static/known list.
export interface ScalpIntradayRestrictedRow {
  symbol: string;
  first_detected_at: string;
  last_detected_at: string;
  hit_count: number;
  last_detail: string | null;
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
  // ADDED (session48): live "what is the current/last cycle doing right
  // now" poll — see pipeline_status.py's docstring. No auth, matches every
  // other GET read route.
  pipelineStatus: () => psRequest<ScalpPipelineStatus>("/pipeline/status"),

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
  candidates: () => psRequest<ScalpCandidatesResponse>("/candidates"),
  // AUDIT FIX (session62, issue #5): reasonPrefix optionally isolates one
  // class of skip reason (e.g. "QUALITY_GATE") from the full mixed log —
  // see the matching backend docstring on GET /candidates/log.
  candidatesLog: (limit = 100, reasonPrefix?: string) =>
    psRequest<ScalpCandidateLogRow[]>(
      `/candidates/log?limit=${limit}${reasonPrefix ? `&reason_prefix=${encodeURIComponent(reasonPrefix)}` : ""}`
    ),
  candidatesRestricted: () => psRequest<ScalpIntradayRestrictedRow[]>("/candidates/restricted"),

  ledger: () => psRequest<ScalpLedgerState>("/ledger"),
  syncLedger: () => psRequest<{ status: string; total_allocated_capital: number }>("/ledger/sync", { method: "POST" }, true),
  // AUDIT FIX (this session): the backend route (POST /ledger/reset-daily)
  // has existed for a while — an explicit manual/emergency reset of today's
  // P&L + kill switch, on top of the automatic lazy reset-on-date-change —
  // but no client method or button ever called it. It was only reachable
  // via a raw HTTP request, never from the dashboard. Added here + wired to
  // a confirm-guarded button in PositionStocksTab.tsx, same confirm pattern
  // as Kill Switch.
  resetLedgerDaily: () => psRequest<{ status: string }>("/ledger/reset-daily", { method: "POST" }, true),

  wsStatus: () => psRequest<{ connected: boolean; subscribed_symbols?: number; last_tick_at?: string | null; reconnect_attempts?: number }>("/ws-status"),
  dhanLiveOrders: () => psRequest<DhanLiveOrders>("/dhan/live-orders"),
  dhanAccount: () => psRequest<DhanAccountStatus>("/dhan/account", {}, true),

  reconcile: () => psRequest<{ status: string; positions_closed: number }>("/reconcile", { method: "POST" }, true),

  // ADDED (this session — "manual control to buy... and manual exit if
  // needed"): neither existed before. manualBuy sizes automatically when
  // quantity is omitted (same risk-based sizing an automatic entry uses);
  // closePosition flattens one open position right now regardless of
  // whether its own bracket target/stop would currently trigger. See the
  // backend's main.py POST /positions/manual/buy and
  // POST /positions/{id}/close docstrings for the full mechanism.
  manualBuy: (symbol: string, quantity?: number) =>
    psRequest<{
      ok: boolean; id: number; symbol: string; quantity: number;
      entry_price: number; target_price: number; stop_price: number;
      dhan_super_order_id: string | null;
    }>("/positions/manual/buy", {
      method: "POST",
      body: JSON.stringify({ symbol, quantity: quantity ?? null }),
    }, true),

  closePosition: (id: number) =>
    psRequest<{ ok: boolean; status: string; id: number; symbol: string }>(
      `/positions/${id}/close`, { method: "POST" }, true
    ),

  // 2026-09-17 addition: mirrors real-trade-service's holdings/sellHolding
  // pair — this service had no holdings endpoint at all before that fix.
  holdings: () => psRequest<{ ok: boolean; holdings: any[] }>("/dhan/holdings", {}, true),

  sellHolding: (securityId: string, exchangeSegment: string, symbol: string, quantity: number) =>
    psRequest<{ ok: boolean; symbol: string; qty: number; dhan_order_id?: string | null }>(
      `/dhan/holdings/sell`,
      { method: "POST", body: JSON.stringify({
        security_id: securityId, exchange_segment: exchangeSegment, symbol, quantity,
      }) },
      true
    ),
};
