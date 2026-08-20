// frontend/src/api/api.ts

const STORAGE_KEY = "stockky:api_url";

export function getApiUrl(): string {
  const stored = localStorage.getItem(STORAGE_KEY);
  if (stored) return stored;
  return (import.meta.env.VITE_API_URL || "").replace(/\/$/, "");
}


/** Alias for callers that import API_BASE_URL — resolves live base (Settings + VITE_API_URL). */
export function API_BASE_URL(): string {
  return getApiUrl();
}

/** Absolute gateway URL for a path (works across separate Render domains). */
export function apiUrl(path: string): string {
  const base = getApiUrl().replace(/\/$/, "");
  const p = path.startsWith("/") ? path : `/${path}`;
  return `${base}${p}`;
}

export function setApiUrl(url: string) {
  const clean = url.trim().replace(/\/$/, "");
  if (clean) localStorage.setItem(STORAGE_KEY, clean);
  else localStorage.removeItem(STORAGE_KEY);
}

export function clearApiUrlOverride() {
  localStorage.removeItem(STORAGE_KEY);
}

export interface FundamentalMetrics {
  revenue_growth: number | null;
  earnings_growth: number | null;
  roe: number | null;
  debt_to_equity: number | null;
  free_cashflow: number | null;
  profit_margins: number | null;
  institutional_holding: number | null;
  pe_ratio: number | null;
  forward_pe: number | null;
}

export interface Decision {
  symbol: string;
  decision: "BUY NOW" | "PREPARE TO BUY" | "HOLD" | "DO NOT BUY" | "SELL" | "WAIT";
  confidence: "High" | "Medium" | "Low";
  combined_score: number;
  technical_score: number;
  fundamental_score: number;
  news_score: number | null;
  prediction_score: number | null;
  market_score: number;
  market_sentiment_adjustment: number;
  training_score: number;
  event_risk: boolean;
  entry_range: { low: number | null; high: number | null } | null;
  target: number | null;
  stop_loss: number | null;
  holding_period: string;
  close: number | null;
  price?: number | null;
  cmp?: number | null;
  current_price?: number | null;
  ltp?: number | null;
  last_price?: number | null;
  prev_close?: number | null;
  support: number | null;
  resistance: number | null;
  reasons: {
    technical: string[];
    fundamental: string[];
    news?: string[];
    prediction?: string[];
    event?: string[];
    market?: string[];
    training?: string[];
  };
  valuation: string;
  sector: string | null;
  natural_language_summary?: string;
  fundamental_metrics?: FundamentalMetrics;
  data_insufficient?: boolean;
  fundamental_fallback?: boolean;
  data_quality?: { level?: string; flags?: string[]; note?: string; sources_used?: string[] };
  event_score_delta?: number;
  event_data?: Record<string, unknown> | null;
  holding_period_estimate?: {
    min_days: number;
    max_days: number;
    expected_by_earliest: string;
    expected_by_latest: string;
    label: string;
  } | null;
  long_term_hold?: boolean;
  long_term_hold_estimate?: {
    min_months: number;
    max_months: number;
    expected_by_earliest: string;
    expected_by_latest: string;
    label: string;
  } | null;
}

export interface ScanResult {
  scanned: number;
  universe_size: number;
  watchlist_size: number;
  recommendations: Decision[];
  watchlist_candidates: Decision[];
  verdict: string;
  market_mood: string;
  market_stats: {
    buy_signals: number;
    sell_signals: number;
    hold_signals: number;
    cautious: number;
  };
  all_results: Decision[];
  errors: { symbol: string; error: string }[];
}

export interface ScanStatus {
  status: "running" | "done" | "error" | "cancelled";
  total: number;
  processed: number;
  elapsed: number;
  estimated_remaining?: number | null;
  result?: ScanResult;
  error?: string;
  /** True when scan was user-cancelled and result is partial. */
  cancelled?: boolean;
  /** True when rankings are incomplete / partial. */
  partial?: boolean;
}

export interface MarketStock {
  symbol: string;
  price: number;
  change: number;
  change_pct: number;
  volume?: number;
  high?: number;
  low?: number;
}

export interface MarketResponse {
  data: MarketStock[];
  count: number;
}

export interface NotificationChannelStatus {
  configured: boolean;
  enabled: boolean;
  masked: string;
  chat_id?: string;
}

export interface NotificationConfig {
  discord: NotificationChannelStatus;
  slack: NotificationChannelStatus;
  telegram: NotificationChannelStatus;
  callmebot: NotificationChannelStatus & { phone?: string; user?: string; users_preview?: string; users?: string; recipients_count?: number };
  persisted: boolean;
}

export interface SystemServiceStatus {
  ok: boolean;
  required: boolean;
  status: string;
  seconds?: number;
  error?: string;
  url?: string | null;
  latency_ms?: number | null;
  detail?: string | null;
}

export interface SystemHealth {
  required_ok: boolean;
  all_ok: boolean;
  services: Record<string, SystemServiceStatus>;
}

// ───────────────────────────────────────────────
// Training Service types – updated to match actual responses
// ───────────────────────────────────────────────

export interface DbStatus {
  ok?: boolean;
  db_backend?: string;
  db_durable?: boolean;
  db_connected?: boolean;
  db_provider?: string | null;
  db_message?: string | null;
  db_error?: string | null;
  status?: string;
  source?: string;
}

export interface TrainingStatusResponse {
  db_backend?: string;
  db_durable?: boolean;
  db_connected?: boolean;
  db_provider?: string | null;
  db_message?: string | null;
  db_error?: string | null;

  service_url: string;
  production_model_exists: boolean;
  production_model_version?: string | null;
  last_training: string | null;
  dataset_size: number;
  num_symbols: number;
  metrics: Record<string, number>;
  fold_details: Array<{
    fold: number;
    train_start: string;
    train_end: string;
    val_start: string;
    val_end: string;
    train_samples: number;
    val_samples: number;
  }>;
  model_version: string | null;
  training_in_progress: boolean;
}

export interface TriggerTrainingResponse {
  status: string;
  service_url?: string;
}

export interface TrainingModelStatus {
  production_model: {
    version: string;
    training_date: string;
    features: string[];
    metrics: {
      accuracy: number;
      precision: number;
      recall: number;
      f1: number;
      roc_auc: number;
      train_size: number;
      val_size: number;
    };
    status: string;
  } | null;
  candidate_model: {
    version: string;
    training_date: string;
    features: string[];
    metrics: {
      accuracy: number;
      precision: number;
      recall: number;
      f1: number;
      roc_auc: number;
      train_size: number;
      val_size: number;
    };
    status: string;
  } | null;
  last_training_date: string | null;
  dataset_size: number;
  performance: {
    'T+1 Success': number;
    'T+5 Success': number;
    'Average T+1': number;
    'Average T+5': number;
  };
}

export interface TrainingScore {
  symbol: string;
  training_score?: number | null;
  t1_success_probability?: number | null;
  t5_success_probability?: number | null;
  model_success_probability?: number | null;
  historical_similarity?: number | null;
  available?: boolean;
  message?: string;
  similar_setups?: Array<{
    symbol: string;
    similarity: number;
    outcome: string;
  }>;
}

export interface PredictionHistoryItem {
  prediction_id: string;
  symbol: string;
  timestamp: string;
  decision: string;
  price: number;
  t1_success: number;
  t5_success: number;
  outcomes: Array<{ period: string; return_pct: number | null; success: number }>;
}

export interface PeriodRollupItem {
  period: string;
  predictions_recorded: number;
  unique_symbols: number;
  buy_now: number;
  prepare_to_buy: number;
  t1_evaluated: number;
  t1_success_rate: number | null;
  t1_avg_return_pct: number | null;
  t5_evaluated: number;
  t5_success_rate: number | null;
  t5_avg_return_pct: number | null;
  t1_pending: number;
  t5_pending: number;
}

export interface PaperTrade {
  trade_id: string;
  prediction_id: string;
  symbol: string;
  capital_allocated: number;
  entry_price: number;
  quantity: number;
  entry_date: string;
  target: number | null;
  stop_loss: number | null;
  status: "OPEN" | "CLOSED";
  current_price: number | null;
  exit_price: number | null;
  exit_date: string | null;
  exit_reason: string | null;
  pnl_amount: number | null;
  pnl_pct: number | null;
  last_marked_at: string | null;
}

export interface PortfolioSummary {
  cash_balance: number;
  total_deposited: number;
  realized_pnl: number;
  open_positions_value: number;
  open_positions_pnl: number;
  total_equity: number;
  open_positions: number;
  closed_positions: number;
  win_rate: number | null;
}

export interface TradeReportBucket {
  period: string;
  trades_opened: number;
  trades_closed: number;
  realized_pnl: number;
  wins: number;
  losses: number;
  capital_deployed: number;
  win_rate: number | null;
}

export interface StockHistoryPoint {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface StockHistory {
  symbol: string;
  period: string;
  points: StockHistoryPoint[];
  change_pct: number | null;
}

export interface TrainingProgress {
  stage:
    | "idle"
    | "loading_data"
    | "data_loaded"
    | "building_features"
    | "splitting"
    | "walk_forward"
    | "fitting_model"
    | "calibrating"
    | "evaluating"
    | "saving_model"
    | "done"
    | "aborted"
    | "error";
  detail: Record<string, unknown>;
  timestamp: number | null;
  elapsed?: number | null;
  percent?: number | null;
  is_running?: boolean;
  error?: string | null;
}


export interface ActionablePick {
  symbol: string;
  decision: string;
  confidence: string;
  price: number;
  target?: number | null;
  stop_loss?: number | null;
  entry_range_low?: number | null;
  entry_range_high?: number | null;
  combined_score: number;
  technical_score: number;
  fundamental_score: number;
  news_score?: number | null;
  prediction_score?: number | null;
  market_score: number;
  training_score: number;
  event_risk: boolean;
  market_mood?: string | null;
  nifty_change_pct?: number | null;
  sensex_change_pct?: number | null;
  rsi?: number | null;
  macd?: string | null;
  ema?: string | null;
  volume_ratio?: number | null;
  debt_to_equity?: number | null;
  roe?: number | null;
  roce?: number | null;
  holding_period?: string | null;
  support?: number | null;
  resistance?: number | null;
  sector?: string | null;
  valuation?: string | null;
  market_sentiment_adjustment?: number | null;
  feature_snapshot?: Record<string, unknown> | null;
}

export interface ActionableCommitResult {
  symbol: string;
  prediction_id: string;
  record_status: "stored" | "already_recorded" | "updated";
  trade_id: string | null;
  trade_status: string;
}

// ───────────────────────────────────────────────
// Market Indices types
// ───────────────────────────────────────────────

export interface IndexData {
  price: number;
  change: number;
  change_pct: number;
}

export interface MarketIndicesResponse {
  nifty: IndexData;
  sensex: IndexData;
  market_mood: "BULLISH" | "BEARISH" | "NEUTRAL";
  market_score: number;
  fetched_at?: string;
  stale?: boolean;
}

// ───────────────────────────────────────────────
// Event types (from first file)
// ───────────────────────────────────────────────

export interface EventItem {
  type: string;
  date: string | null;
  description: string;
}

export interface InstitutionalHolder {
  holder: string;
  shares: number | null;
  pct_held: number | null;
}

export interface CategorizedEvents {
  symbol: string;
  upcoming: EventItem[];
  recent: EventItem[];
  recent_changes: string[];
  institutional_holders: InstitutionalHolder[];
  checked_at: string | null;
}

// ───────────────────────────────────────────────
// Request helper
// ───────────────────────────────────────────────

/** Soft keep-alive while the UI is open — prevents free-tier sleep without hammering. */
let _keepAliveTimer: ReturnType<typeof setInterval> | null = null;
let _lastKeepAlive = 0;
const KEEP_ALIVE_MIN_GAP_MS = 4 * 60 * 1000; // never more often than every 4 min
const KEEP_ALIVE_INTERVAL_MS = 4.5 * 60 * 1000; // ~4.5 min while tab visible

export function startSessionKeepAlive(): void {
  if (typeof window === "undefined") return;
  if (_keepAliveTimer) return;
  const tick = () => {
    if (document.visibilityState === "hidden") return;
    const now = Date.now();
    if (now - _lastKeepAlive < KEEP_ALIVE_MIN_GAP_MS) return;
    _lastKeepAlive = now;
    const base = getApiUrl();
    if (!base) return;
    // Lightweight only — no deep fan-out (avoids overload during scans)
    fetch(`${base}/ops/keepalive`, { method: "GET", mode: "cors" }).catch(() => {});
  };
  // Immediate soft wake when UI mounts
  tick();
  _keepAliveTimer = setInterval(tick, KEEP_ALIVE_INTERVAL_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") tick();
  });
}

export function stopSessionKeepAlive(): void {
  if (_keepAliveTimer) {
    clearInterval(_keepAliveTimer);
    _keepAliveTimer = null;
  }
}

async function softWakeGateway(): Promise<void> {
  const base = getApiUrl();
  if (!base) return;
  try {
    await fetch(`${base}/health?warm=true`, { method: "GET", mode: "cors" });
  } catch {
    /* ignore — request still wakes dyno */
  }
}

async function request<T>(path: string, init?: RequestInit, retries = 3, timeoutMs = 90000): Promise<T> {
  const base = getApiUrl();
  if (!base) {
    throw new Error(
      "Backend URL isn't set. Open Settings (top right) and paste your API Gateway URL."
    );
  }

  const url = `${base}${path}`;
  const controller = new AbortController();
  // Cold-start budget: first attempt can be longer; retries get progressive wait
  const attemptTimeout = timeoutMs;
  const timeoutId = setTimeout(() => controller.abort(), attemptTimeout);

  try {
    const response = await fetch(url, {
      ...init,
      signal: controller.signal,
    });
    clearTimeout(timeoutId);

    const raw = await response.text();
    let data: any = null;
    if (raw && raw.trim()) {
      try {
        data = JSON.parse(raw);
      } catch {
        const preview = raw.slice(0, 120).replace(/[^\x20-\x7E\n\t]/g, "?");
        throw new Error(
          response.ok
            ? `Invalid JSON from ${path}: ${preview}`
            : `${response.status} ${response.statusText}: ${preview}`
        );
      }
    }

    if (!response.ok) {
      const detail =
        (data && (data.detail || data.message || data.error)) ||
        (typeof data === "string" ? data : "") ||
        response.statusText;
      // 502/503/504 often mean cold start mid-request — retry after soft wake
      if (retries > 0 && [502, 503, 504].includes(response.status)) {
        await softWakeGateway();
        await new Promise((r) => setTimeout(r, 2500 * (4 - retries)));
        return request<T>(path, init, retries - 1, Math.min(timeoutMs + 15000, 180000));
      }
      throw new Error(`${response.status}: ${typeof detail === "string" ? detail.slice(0, 300) : JSON.stringify(detail).slice(0, 300)}`);
    }

    return data as T;
  } catch (error) {
    clearTimeout(timeoutId);

    const isAbort = error instanceof DOMException && error.name === "AbortError";
    const isNetwork = error instanceof TypeError || isAbort;

    if (retries > 0 && isNetwork) {
      // Progressive backoff + soft wake (free-tier cold start)
      await softWakeGateway();
      const waitMs = isAbort ? 4000 * (4 - retries) : 1500 * (4 - retries);
      await new Promise((resolve) => setTimeout(resolve, waitMs));
      // Give cold dynos more time on retry
      const nextTimeout = Math.min(timeoutMs + (isAbort ? 30000 : 15000), 180000);
      return request<T>(path, init, retries - 1, nextTimeout);
    }

    if (isAbort) {
      throw new Error(
        `Request timed out after ${timeoutMs / 1000} seconds. The backend may be waking up (free-tier cold start). ` +
          `We already retried with soft-wake — wait a few seconds and try again, or click Wake Services.`
      );
    }

    throw error;
  }
}

export async function wakeService(url: string): Promise<void> {
  if (!url) return;
  try {
    await fetch(url + "/health?warm=true", { mode: "cors" });
  } catch {
    try {
      await fetch(url + "/health", { mode: "no-cors" });
    } catch {
      // Ignore – request still wakes the service
    }
  }
}


/**
 * Step 1 fix — callback-style NDJSON stream consumer for /api/scan/stream
 * (and /scan/stream). Emits each parsed line to onItemReceived as it arrives.
 * Prefer this when you want progressive React setState without an async generator.
 *
 * Also see api.scanStream (async generator) which App.tsx already uses.
 */
export async function streamMarketScan(
  mode: "full" | "lite" = "full",
  onItemReceived: (item: any) => void,
  opts?: { signal?: AbortSignal; forceRefresh?: boolean }
): Promise<void> {
  const base = getApiUrl();
  if (!base) {
    throw new Error("API base URL not set. Configure backend URL first.");
  }
  const lite = mode === "lite";
  const force = opts?.forceRefresh ? "true" : "false";
  // Step 5: both /api/scan/stream and /scan/stream are registered on the gateway
  const url = `${base}/api/scan/stream?force_refresh=${force}&lite=${lite ? "true" : "false"}`;

  const response = await fetch(url, {
    method: "GET",
    signal: opts?.signal,
    headers: { Accept: "application/x-ndjson" },
  });

  if (!response.ok) {
    throw new Error(`Scan failed with status ${response.status}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("ReadableStream not supported in this browser.");
  }

  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    // Keep the last incomplete fragment in the buffer
    buffer = lines.pop() || "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const parsed = JSON.parse(trimmed);
        onItemReceived(parsed);
      } catch (e) {
        console.error("Failed to parse NDJSON line:", trimmed.slice(0, 120), e);
      }
    }
  }

  // Flush any trailing complete object
  const tail = buffer.trim();
  if (tail) {
    try {
      onItemReceived(JSON.parse(tail));
    } catch (e) {
      console.error("Failed to parse trailing NDJSON:", tail.slice(0, 120), e);
    }
  }
}


/**
 * Step 6 — callback-style NDJSON consumer for surprise scan stream.
 * Mirrors streamMarketScan; progressive upserts via onItemReceived.
 */
export async function streamSurpriseScan(
  onItemReceived: (item: any) => void,
  opts?: { signal?: AbortSignal; forceReload?: boolean }
): Promise<void> {
  const base = getApiUrl();
  if (!base) {
    throw new Error("API base URL not set. Configure backend URL first.");
  }
  const force = opts?.forceReload ? "true" : "false";
  const url = `${base}/api/surprise/scan/stream?force_reload=${force}`;

  const response = await fetch(url, {
    method: "GET",
    signal: opts?.signal,
    headers: { Accept: "application/x-ndjson" },
  });
  if (!response.ok) {
    throw new Error(`Surprise scan failed with status ${response.status}`);
  }
  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("ReadableStream not supported in this browser.");
  }

  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        onItemReceived(JSON.parse(trimmed));
      } catch (e) {
        console.error("Failed to parse surprise NDJSON line:", trimmed.slice(0, 120), e);
      }
    }
  }
  const tail = buffer.trim();
  if (tail) {
    try {
      onItemReceived(JSON.parse(tail));
    } catch (e) {
      console.error("Failed to parse trailing surprise NDJSON:", tail.slice(0, 120), e);
    }
  }
}

// ───────────────────────────────────────────────
// API object
// ───────────────────────────────────────────────

export const api = {
  ping: () => request<{ status: string; service: string }>("/health", undefined, 3, 45000),

  wakeAll: () => request<any>("/wake-all", undefined, 1, 45000),
  systemHealth: () => request<SystemHealth>("/system/health", undefined, 3, 90000),

  getStock: (symbol: string, alreadyOwned = false) =>
    request<Decision>(`/stock/${symbol}?already_owned=${alreadyOwned}`, undefined, 3, 120000),

  runScan: () => request<ScanResult>("/scan", undefined, 2, 180000),

  scanStart: (forceRefresh = false, lite: boolean | null = null) =>
    request<{
      task_id: string;
      from_cache?: boolean;
      lite?: boolean;
      universe_size?: number;
      message?: string;
      scanned_at?: string;
    }>(
      `/scan/start?force_refresh=${forceRefresh}${lite === null ? "" : `&lite=${lite ? "true" : "false"}`}`,
      { method: "POST" },
      2,
      120000
    ),

  scanStatus: (taskId: string) =>
    request<ScanStatus>(`/scan/status/${taskId}`, undefined, 2, 10000),

  getLastScan: () => request<any>("/scan/last", undefined, 2, 15000),
  scanCancel: (taskId: string) =>
    request<{ status: string; processed_so_far?: number; total?: number }>(
      `/scan/cancel/${taskId}`, { method: "POST" }, 1, 10000
    ),

  scanWatchlist: () =>
    request<ScanResult>("/scan/watchlist", undefined, 2, 180000),

  /**
   * Buy Sniper — 1–4 high-conviction setups from scan rows.
   * POST /api/scan/find-buys
   */
  findBuys: (payload: {
    stocks?: any[];
    all_results?: any[];
    results?: any[];
    recommendations?: any[];
    target_count?: number;
    min_conviction?: number;
  }) =>
    request<{
      ok?: boolean;
      count: number;
      suggestions: any[];
      scanned_input?: number;
      error?: string;
    }>(
      "/api/scan/find-buys",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      1,
      30000
    ),

  /**
   * Stream full-market scan as NDJSON (one JSON object per line).
   * Prefer this over scanStart+polling when you want progressive UI updates
   * and to avoid Render ~100s gateway timeouts on large universes.
   *
   * Usage:
   *   const ac = new AbortController();
   *   for await (const row of api.scanStream({ lite: true, signal: ac.signal })) {
   *     if (row._meta) { /* progress / done *\/ }
   *     else { /* symbol result *\/ }
   *   }
   */
  scanStream: async function* (opts?: {
    lite?: boolean | null;
    forceRefresh?: boolean;
    signal?: AbortSignal;
  }): AsyncGenerator<Record<string, any>, void, unknown> {
    const lite = opts?.lite;
    const force = opts?.forceRefresh ? "true" : "false";
    const liteQ =
      lite === undefined || lite === null ? "" : `&lite=${lite ? "true" : "false"}`;
    const base = getApiUrl();
    const url = `${base}/api/scan/stream?force_refresh=${force}${liteQ}`;
    const res = await fetch(url, {
      method: "GET",
      signal: opts?.signal,
      headers: { Accept: "application/x-ndjson" },
    });

    if (!res.ok || !res.body) {
      throw new Error(`scanStream failed: HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
          yield JSON.parse(line);
        } catch {
          // skip malformed line
        }
      }
    }
    const tail = buf.trim();
    if (tail) {
      try {
        yield JSON.parse(tail);
      } catch {
        /* ignore */
      }
    }
  },

  neonKeepalive: () =>
    request<{ ok: boolean; neon_connected?: boolean; error?: string }>(
      "/ops/neon-keepalive",
      { method: "POST" },
      1,
      15000
    ),

  getWatchlist: () => request<{ symbols: string[] }>("/watchlist", undefined, 2, 30000),


  setWatchlist: (symbols: string[]) =>
    request<{ symbols: string[] }>(
      "/watchlist",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols }),
      },
      2,
      30000
    ),

  addToWatchlist: (symbol: string) =>
    request<{ symbols: string[] }>(
      "/watchlist/add",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: [symbol] }),
      },
      2,
      30000
    ),

  addManyToWatchlist: (symbols: string[]) =>
    request<{ symbols: string[] }>(
      "/watchlist/add",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols }),
      },
      2,
      30000
    ),

  marketTopGainers: () => request<MarketResponse>("/market/top-gainers", undefined, 2, 30000),
  marketTopLosers: () => request<MarketResponse>("/market/top-losers", undefined, 2, 30000),
  marketMostActive: () => request<MarketResponse>("/market/most-active", undefined, 2, 30000),
  marketTrending: () => request<MarketResponse>("/market/trending", undefined, 2, 30000),

  marketIndices: (forceRefresh = false) =>
    request<MarketIndicesResponse>(
      `/market/indices?force_refresh=${forceRefresh}`,
      undefined,
      2,
      10000
    ),

  getRateLimits: () => request<any>("/api/rate-limits", undefined, 2, 20000),
  getNotificationConfig: () => request<NotificationConfig>("/notifications/config", undefined, 2, 30000),

  saveNotificationConfig: (update: {
    discord_webhook_url?: string;
    slack_webhook_url?: string;
    telegram_bot_token?: string;
    telegram_chat_id?: string;
    callmebot_user?: string;
    callmebot_phone?: string;
    callmebot_apikey?: string;
    callmebot_users?: string;
    enabled?: Partial<Record<"discord" | "slack" | "telegram" | "callmebot", boolean>>;
  }) =>
    request<NotificationConfig>(
      "/notifications/config",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(update),
      },
      2,
      30000
    ),

  clearNotificationChannel: (channel: "discord" | "slack" | "telegram" | "callmebot") =>
    request<NotificationConfig>(`/notifications/config/${channel}`, { method: "DELETE" }, 2, 30000),

  testNotifications: () =>
    request<{ delivered: boolean; note?: string; results?: Record<string, string> }>(
      "/notifications/test",
      { method: "POST" },
      2,
      30000
    ),

  testCallMeBot: (message?: string) =>
    request<{ ok: boolean; result?: string; error?: string }>(
      `/notifications/call/me?message=${encodeURIComponent(message || "Stockky test call alert")}`,
      { method: "POST" },
      1,
      30000
    ),

  sendPicksToTelegram: (payload: { type: string; recommendations: Decision[] }) =>
    request<{ success: boolean; sent: number; message: string }>(
      "/notifications/send-picks",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      },
      2,
      30000
    ),

  // ─── Surprise momentum scanner ───

  surpriseScan: (forceReload = false) =>
    request<{
      count: number;
      stocks: Array<{
        symbol: string;
        score: number;
        price: number;
        change_pct: number;
        rvol: number;
        trigger_type: string;
        trailing_stop: number;
        target_1: number;
        prev_close?: number;
        sector?: string | null;
        dist_52w_pct?: number;
      }>;
      static_loaded?: number;
      quotes_ok?: number;
      universe_scanned?: number;
      elapsed_sec?: number;
      error?: string;
      min_score?: number;
    }>(
      `/api/surprise/scan?force_reload=${forceReload ? "true" : "false"}`,
      undefined,
      2,
      120000
    ),

  surpriseScanStream: async function* (opts?: {
    forceReload?: boolean;
    signal?: AbortSignal;
  }): AsyncGenerator<Record<string, any>, void, unknown> {
    const force = opts?.forceReload ? "true" : "false";
    const base = getApiUrl();
    const url = `${base}/api/surprise/scan/stream?force_reload=${force}`;
    const res = await fetch(url, {
      method: "GET",
      signal: opts?.signal,
      headers: { Accept: "application/x-ndjson" },
    });
    if (!res.ok || !res.body) {
      throw new Error(`surpriseScanStream failed: HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        try {
          yield JSON.parse(line);
        } catch {
          /* skip */
        }
      }
    }
    const tail = buf.trim();
    if (tail) {
      try {
        yield JSON.parse(tail);
      } catch {
        /* ignore */
      }
    }
  },

  surprisePremarket: () =>
    request<{
      ok?: boolean;
      accepted?: boolean;
      already_running?: boolean;
      message?: string;
      symbols?: number;
      progress?: Record<string, unknown>;
    }>("/surprise/premarket?background=true", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }, 1, 30000),

  surprisePremarketStatus: () =>
    request<{
      stage?: string;
      percent?: number;
      processed?: number;
      total?: number;
      computed?: number;
      errors?: number;
      elapsed_sec?: number;
      eta_sec?: number | null;
      is_running?: boolean;
      current_symbol?: string | null;
      message?: string;
      error?: string;
    }>("/surprise/premarket/status", undefined, 1, 15000),

  surpriseStatic: (limit = 50) =>
    request<{ ok: boolean; count?: number; rows?: any[]; error?: string }>(
      `/api/surprise/static?limit=${limit}`,
      undefined,
      1,
      20000
    ),

  // ─── Training Service endpoints ───

  getTrainingStatus: () =>
    request<TrainingStatusResponse>("/training/status", undefined, 2, 30000),
  getDbStatus: () =>
    request<DbStatus>("/ops/db-status", undefined, 1, 12000),


  triggerTraining: (labelSource: "t1_outcome" | "trade_pnl" = "t1_outcome") =>
    request<TriggerTrainingResponse>(
      `/training/api/train?label_source=${labelSource}`,
      { method: "POST" },
      2,
      90000 // only waits for 202 Accepted; training continues in background
    ),

  getTrainingScore: (symbol: string) =>
    request<TrainingScore>(`/training/score/${symbol}`, undefined, 2, 30000),

  clearTrainingLock: async () => {
    // Prefer /api/lock/clear (POST — most reliable through gateway), then DELETE /lock
    try {
      return await request<{ status: string }>(
        "/training/api/lock/clear",
        { method: "POST" },
        1,
        15000
      );
    } catch {
      return request<{ status: string }>("/training/lock", { method: "DELETE" }, 1, 15000);
    }
  },


  getPredictionHistory: (limit = 20) =>
    request<{ predictions: PredictionHistoryItem[]; total: number }>(
      `/training/api/predictions/history?limit=${limit}`, undefined, 2, 30000
    ),

  getDailyRollup: (days = 30) =>
    request<PeriodRollupItem[]>(`/training/api/metrics/daily?days=${days}`, undefined, 2, 30000),

  getWeeklyRollup: (weeks = 12) =>
    request<PeriodRollupItem[]>(`/training/api/metrics/weekly?weeks=${weeks}`, undefined, 2, 30000),

  triggerEvaluation: (period: "t1" | "t5") =>
    request<{
      status?: string;
      ok?: boolean;
      pending?: number;
      due?: number;
      waiting?: number;
      attempted?: number;
      succeeded?: number;
      period?: string;
      reasons?: Record<string, number>;
      pipeline?: { step: string; ok: boolean; detail: string }[];
      message?: string;
      skipped_sample?: { symbol: string; reason: string }[];
    }>(`/training/api/evaluate/${period}`, { method: "POST" }, 1, 120000),

  /** Record picks for training/T+1/T+5 tracking. Does NOT open trades by default. */
  commitActionablePicks: (picks: ActionablePick[], capitalPerTrade = 10000, openTrades = false) =>
    request<{ results: ActionableCommitResult[]; db_backend?: string; db_durable?: boolean; db_connected?: boolean; db_message?: string }>(
      "/training/api/actionable/commit",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ picks, capital_per_trade: capitalPerTrade, open_trades: openTrades }),
      },
      1,
      60000
    ),

  /** Record picks AND open paper trades. Use for "to Trade" actions. */
  commitActionableToTrade: (picks: ActionablePick[], capitalPerTrade = 10000) =>
    request<{ results: ActionableCommitResult[]; db_backend?: string; db_durable?: boolean; db_connected?: boolean; db_message?: string }>(
      "/training/api/actionable/commit",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ picks, capital_per_trade: capitalPerTrade, open_trades: true }),
      },
      1,
      60000
    ),

  getTrades: (status: "open" | "closed" | "all" = "all") =>
    request<PaperTrade[]>(`/training/api/trades?status=${status}`, undefined, 2, 30000),

  getTradesSummary: () =>
    request<PortfolioSummary>("/training/api/trades/summary", undefined, 2, 30000),

  getPortfolioSummary: () =>
    request<PortfolioSummary>("/training/api/portfolio/summary", undefined, 2, 30000),

  depositFunds: (amount: number, note?: string) =>
    request<{ status: string; cash_balance: number; total_deposited: number }>(
      "/training/api/portfolio/deposit",
      { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ amount, note }) },
      1,
      15000
    ),

  getDailyTradeReport: (days = 30) =>
    request<TradeReportBucket[]>(`/training/api/trades/report/daily?days=${days}`, undefined, 2, 30000),

  getWeeklyTradeReport: (weeks = 12) =>
    request<TradeReportBucket[]>(`/training/api/trades/report/weekly?weeks=${weeks}`, undefined, 2, 30000),

  getStockHistory: async (symbol: string, period: string = "1mo") => {
    try {
      return await request<StockHistory>(`/market/history/${symbol}?period=${period}`, undefined, 1, 45000);
    } catch {
      return request<StockHistory>(`/training/api/stock/history/${symbol}?period=${period}`, undefined, 2, 45000);
    }
  },

  // ─── Event endpoint (from first file) ───
  getSymbolEvents: (symbol: string) =>
    request<CategorizedEvents>(`/events/${symbol}`, undefined, 2, 30000),

  getTrainingProgress: () =>
    request<TrainingProgress>("/training/api/train/progress", undefined, 1, 10000),

  /** T+1 / T+5 evaluation queue progress (pending, due, ETA). */
  getEvaluateStatus: () =>
    request<{
      t1: {
        period: string;
        total: number;
        pending: number;
        evaluated: number;
        due_now: number;
        success: number;
        success_rate_pct: number | null;
        progress_pct: number;
        eta_sweep_seconds: number;
        eta_sweep_label: string;
        next_unlock_hours: number | null;
        status: string;
      };
      t5: {
        period: string;
        total: number;
        pending: number;
        evaluated: number;
        due_now: number;
        success: number;
        success_rate_pct: number | null;
        progress_pct: number;
        eta_sweep_seconds: number;
        eta_sweep_label: string;
        next_unlock_hours: number | null;
        status: string;
      };
    }>("/training/api/evaluate/status", undefined, 2, 20000),

  markTradesToMarket: () =>
    request<{ status: string }>("/training/api/trades/mark-to-market", { method: "POST" }, 1, 30000),

  // ─── New endpoints from second file ───
  clearTradesBackup: () =>
    request<any>("/training/api/trades/clear-backup", { method: "POST" }, 1, 30000),

  listTradeBackups: () =>
    request<{ backups: string[] }>("/training/api/trades/backups", undefined, 1, 15000),

  getTradeBackup: (filename: string) =>
    request<{ ok: boolean; filename?: string; backup?: any; error?: string }>(
      `/training/api/trades/backups/${encodeURIComponent(filename)}`,
      undefined,
      1,
      30000
    ),

  addToTrade: (tradeId: string, qty: number, price?: number) =>
    request<any>(`/training/api/trades/${tradeId}/add`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quantity: qty, price }),
    }, 1, 30000),

  closeTrade: (tradeId: string) =>
    request<{ status: string; trade_id: string; exit_price: number; pnl_pct: number }>(
      `/training/api/trades/${tradeId}/close`, { method: "POST" }, 1, 30000
    ),

  openManualTrade: (payload: {
    symbol: string;
    quantity?: number;
    price?: number;
    capital?: number;
    note?: string;
  }) =>
    request<{
      ok: boolean;
      trade_id: string;
      symbol: string;
      quantity: number;
      entry_price: number;
      capital_allocated: number;
      was_new: boolean;
      ai_warning?: string;
    }>("/training/api/trades/manual", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }, 1, 30000),

  getQuote: (symbol: string) =>
    request<{ symbol: string; price?: number; close?: number; as_of?: string; source?: string }>(
      `/quote/${encodeURIComponent(symbol)}`,
      undefined,
      1,
      10000
    ),


  /** Full scan universe → training (v15 INTEGRATION) */
  trainFromUniverse: (body: {
    symbols: string[];
    decisions?: Record<string, string>;
    scores?: Record<string, number>;
    feature_snapshots?: Record<string, Record<string, unknown>>;
    source?: string;
    retention_hours?: number;
    trigger_training?: boolean;
  }) =>
    request<{
      ok: boolean;
      ingested: number;
      message: string;
      source?: string;
      expires_at?: string;
      retention_hours?: number;
      training_triggered?: boolean;
    }>(
      "/training/api/universe/train-from-universe",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      1,
      120000
    ),

  ingestUniverse: (body: {
    symbols: string[];
    decisions?: Record<string, string>;
    scores?: Record<string, number>;
    feature_snapshots?: Record<string, Record<string, unknown>>;
    source?: string;
    retention_hours?: number;
    trigger_training?: boolean;
  }) =>
    request<{
      ok: boolean;
      ingested: number;
      message: string;
      source?: string;
      expires_at?: string;
      retention_hours?: number;
      training_triggered?: boolean;
    }>(
      "/training/api/universe/ingest",
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      },
      1,
      120000
    ),

  getDataFeedStatus: () =>
    request<any>("/data-feed/status", undefined, 2, 30000),
  getDataFeedMeta: () =>
    request<any>("/data-feed/meta", undefined, 2, 30000),
  runDataFeed: (force = false) =>
    request<any>(`/data-feed/run?force=${force}`, { method: "POST" }, 2, 45000),
  /** Nuke stockky_kv + lock constraints before a pristine full feed */
  hardResetDataFeed: () =>
    request<any>("/data-feed/hard-reset", { method: "POST" }, 1, 60000),
  /** Yahoo bulk price feed — bypasses NSE 403 on Render (1-call chunks) */
  startBulkFeed: (useUniverse = true) =>
    request<any>(
      `/data-feed/start-bulk-feed?use_universe=${useUniverse}`,
      { method: "POST" },
      1,
      300000
    ),
  runSurprisePremarketFeed: (force = false) =>
    request<any>(`/api/surprise/run-premarket-feed?force=${force}`, { method: "POST" }, 1, 300000),
  surpriseAudit: () =>
    request<any>("/api/surprise/audit", undefined, 2, 30000),
  surpriseRepairBatch: (limit = 15, symbol?: string) =>
    request<any>(
      `/api/surprise/repair-batch?limit=${limit}${symbol ? `&symbol=${encodeURIComponent(symbol)}` : ""}`,
      { method: "POST" },
      1,
      180000
    ),
  /** Surgical quote refresh for Prepare-to-Buy band only (no 300-stock storm) */
  refreshPrepareToBuy: (minScore = 58, maxScore = 68) =>
    request<any>(
      `/data-feed/refresh-prepare-to-buy?min_score=${minScore}&max_score=${maxScore}`,
      { method: "POST" },
      1,
      120000
    ),
  dataFeedRunNewOnly: () =>
    request<any>(`/data-feed/run?only_new=true&force=false&resume=false`, { method: "POST" }, 2, 45000),
  resumeDataFeed: () =>
    request<any>("/data-feed/resume", { method: "POST" }, 2, 45000),
  stopDataFeed: () =>
    request<any>("/data-feed/stop?force=true", { method: "POST" }, 2, 45000),

  stopAllScans: () =>
    request<any>("/scan/stop-all", { method: "POST" }, 1, 20000),

  powerOff: () =>
    request<any>("/ops/power-off", { method: "POST" }, 1, 45000),

  resumeActivity: () =>
    request<any>("/ops/resume-activity", { method: "POST" }, 1, 15000),
  getDataFeedSymbol: (symbol: string) =>
    request<any>(`/data-feed/${encodeURIComponent(symbol)}`, undefined, 1, 20000),
  auditMissingFeed: () =>
    request<any>("/api/feed/audit-missing", undefined, 2, 60000),
  repairFeedSingle: (symbol: string) =>
    request<any>(`/api/feed/repair-single/${encodeURIComponent(symbol)}`, { method: "POST" }, 2, 90000),
  repairFeedBatch: (limit: number = 15) =>
    request<any>(`/api/feed/repair-batch?limit=${limit}`, { method: "POST" }, 2, 180000),

  getStockkyHotStatus: () =>
    request<any>("/stockky-hot/status", undefined, 1, 15000),
  getStockkyHotResult: () =>
    request<any>("/stockky-hot/result", undefined, 1, 30000),
  runStockkyHot: (force = true) =>
    request<any>(`/stockky-hot/run?force=${force}`, { method: "POST" }, 1, 30000),

  getStockkyHot: (force = false) =>
    request<{
      news_driven: any[];
      results_driven: any[];
      bulk_insider_driven: any[];
      generated_at?: string;
      universe_size?: number;
      cached?: boolean;
    }>(`/stockky-hot${force ? "?force=true" : ""}`, undefined, 1, 90000),
};