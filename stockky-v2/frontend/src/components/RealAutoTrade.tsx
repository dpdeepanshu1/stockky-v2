import { useCallback, useEffect, useRef, useState } from "react";
import {
  realTradeApi, getRealTradeApiUrl, setRealTradeApiUrl,
  getSessionToken, setSessionToken, setSessionExpiredHandler,
  type GateStatus, type AuditLogRow, type Position, type OrderRow, type CycleResult, type DhanStatus,
  type PipelineStatus, type CandidateRow,
} from "../realTradeApi";
import ManualTradeTicket from "./trading/ManualTradeTicket";

type Mode = "DEMO" | "REAL";
type Tab = "overview" | "live" | "positions" | "orders" | "watchlist" | "pipeline" | "log";

// ── Formatting helpers ───────────────────────────────────────────────────────
function fmtInr(n: number | null | undefined, decimals = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_00_00_000) return `${sign}₹${(abs / 1_00_00_000).toFixed(1)}Cr`;
  if (abs >= 1_00_000) return `${sign}₹${(abs / 1_00_000).toFixed(1)}L`;
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: decimals })}`;
}

function fmtHms(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function fmtTime(iso: string): string {
  try { return new Date(iso).toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit" }); } catch { return iso; }
}

function fmtDate(iso: string): string {
  try { return new Date(iso).toLocaleDateString("en-IN", { day: "2-digit", month: "short" }); } catch { return iso; }
}

// Safe number extraction from Dhan SDK fund object (handles typos and casing)
function pickNum(obj: any, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = obj?.[k];
    if (typeof v === "number") return v;
    if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  }
  return null;
}

function pnlColor(n: number | null | undefined): string {
  if (n == null) return "text-zinc-500";
  return n >= 0 ? "text-emerald-400" : "text-rose-400";
}

// ── Status dot ──────────────────────────────────────────────────────────────
function Dot({ on, pulse }: { on: boolean; pulse?: boolean }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full ${on ? "bg-emerald-400" : "bg-red-500"} ${pulse && on ? "animate-pulse" : ""}`} />
  );
}

// ── Tiny stat card ──────────────────────────────────────────────────────────
function StatCard({ label, value, sub, color }: { label: string; value: React.ReactNode; sub?: string; color?: string }) {
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-3 flex flex-col gap-0.5">
      <p className="text-[10px] font-mono uppercase tracking-widest text-zinc-500">{label}</p>
      <p className={`text-lg font-bold font-mono ${color ?? "text-zinc-100"}`}>{value}</p>
      {sub && <p className="text-[10px] font-mono text-zinc-600">{sub}</p>}
    </div>
  );
}

// ── Section header ───────────────────────────────────────────────────────────
function SectionHdr({ children }: { children: React.ReactNode }) {
  return <p className="text-[10px] font-mono uppercase tracking-widest text-zinc-500 mb-2">{children}</p>;
}

// ── Live pipeline status (2026-08-27) — what the cycle is doing RIGHT NOW,
// whether triggered by the Run Cycle button or by Auto-Pilot in the
// background, plus recent-cycle history with per-stage timing. Purely a
// display of pipeline_status.py's in-memory snapshot — never triggers
// anything itself. ───────────────────────────────────────────────────────────
const STAGE_LABELS: Record<string, string> = {
  starting: "Starting…",
  candidates: "Fetching candidates",
  entry: "Evaluating entries",
  fills: "Checking fills",
  expire: "Expiring stale orders",
  exit: "Evaluating exits",
  reconcile: "Reconciling with Dhan",
};

const SOURCE_LABELS: Record<string, string> = {
  hot_picks: "Hot Picks",
  ipo: "IPO watchlist",
};

function msFmt(ms: number | null | undefined): string {
  if (ms == null) return "—";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`;
}

function PipelineLiveStatus({ pipeline, mode }: { pipeline: PipelineStatus | null; mode: Mode }) {
  if (!pipeline) {
    return (
      <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
        <SectionHdr>Live cycle status — {mode}</SectionHdr>
        <p className="font-mono text-[11px] text-zinc-600">Loading…</p>
      </div>
    );
  }

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <SectionHdr>Live cycle status — {mode}</SectionHdr>
        {pipeline.running ? (
          <span className="font-mono text-[10px] text-emerald-400 flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
            {pipeline.trigger === "autopilot" ? "Auto-Pilot cycle running" : "Cycle running"}
          </span>
        ) : (
          <span className="font-mono text-[10px] text-zinc-600">Idle — no cycle running</span>
        )}
      </div>

      {pipeline.running && (
        <div className="bg-zinc-950 border border-zinc-800 rounded-lg p-3 space-y-2">
          <div className="flex items-center justify-between">
            <p className="font-mono text-xs text-zinc-200">
              {STAGE_LABELS[pipeline.stage || ""] || pipeline.stage}
            </p>
            <p className="font-mono text-[10px] text-zinc-500">
              stage {msFmt(pipeline.stage_elapsed_ms)} · total {msFmt(pipeline.total_elapsed_ms)}
            </p>
          </div>
          {/* stage progress dots */}
          <div className="flex items-center gap-1">
            {(pipeline.stages || []).map(s => (
              <div key={s} className={`h-1.5 flex-1 rounded-full ${
                s === pipeline.stage ? "bg-emerald-500 animate-pulse"
                : (pipeline.stage_timings_ms && s in pipeline.stage_timings_ms) ? "bg-emerald-800"
                : "bg-zinc-800"
              }`} title={STAGE_LABELS[s] || s} />
            ))}
          </div>
          {pipeline.current_source && (
            <p className="font-mono text-[10px] text-sky-400">
              Source: {SOURCE_LABELS[pipeline.current_source] || pipeline.current_source}
            </p>
          )}
          {pipeline.current_symbol && (
            <p className="font-mono text-[10px] text-amber-300">
              Symbol: {pipeline.current_symbol}
              {!!pipeline.symbols_total && (
                <span className="text-zinc-600"> ({(pipeline.symbols_done ?? 0) + 1}/{pipeline.symbols_total})</span>
              )}
            </p>
          )}
          {pipeline.warning && (
            <p className="font-mono text-[10px] text-amber-400">⚠ {pipeline.warning}</p>
          )}
        </div>
      )}

      {pipeline.last_cycle && (
        <div>
          <p className="font-mono text-[10px] text-zinc-500 mb-1">
            Last cycle — {pipeline.last_cycle.trigger === "autopilot" ? "🤖 Auto-Pilot" : "▶ Manual"} ·{" "}
            {fmtTime(pipeline.last_cycle.ended_at)} · took {msFmt(pipeline.last_cycle.duration_ms)}
          </p>
          {pipeline.last_cycle.warning && (
            <p className="font-mono text-[10px] text-amber-400 mb-1">⚠ {pipeline.last_cycle.warning}</p>
          )}
          {pipeline.last_cycle.error ? (
            <p className="font-mono text-[10px] text-rose-400">Error: {pipeline.last_cycle.error}</p>
          ) : pipeline.last_cycle.auto_disarmed ? (
            <p className="font-mono text-[10px] text-rose-400">Auto-disarmed: {pipeline.last_cycle.auto_disarmed}</p>
          ) : (
            <>
              <p className="font-mono text-[10px] text-zinc-400">
                {pipeline.last_cycle.new_candidates ?? 0} candidates · {pipeline.last_cycle.entered ?? 0} entered ·{" "}
                {pipeline.last_cycle.waited ?? 0} waited · {pipeline.last_cycle.rejected ?? 0} rejected ·{" "}
                {pipeline.last_cycle.full_exits ?? 0} closed
              </p>
              {!!pipeline.last_cycle.entry_details?.length && (
                <div className="mt-2 space-y-1 border-t border-zinc-800 pt-2">
                  {pipeline.last_cycle.entry_details.map((row, i) => {
                    const color = row.action === "ENTER" ? "text-emerald-400"
                      : row.risk_verdict === "REJECTED" || row.risk_verdict === "BLOCKED_GLOBAL" ? "text-rose-400"
                      : "text-amber-400";
                    return (
                      <p key={i} className="font-mono text-[10px] text-zinc-500">
                        <span className={`font-bold ${color}`}>{row.action}</span>{" "}
                        <span className="text-zinc-300">{row.symbol}</span>
                        {row.reasoning && <span className="text-zinc-600"> — {row.reasoning}</span>}
                      </p>
                    );
                  })}
                </div>
              )}
            </>
          )}
        </div>
      )}

      {pipeline.history && pipeline.history.length > 1 && (
        <details className="group">
          <summary className="font-mono text-[10px] text-zinc-500 cursor-pointer select-none">
            Recent cycles ({pipeline.history.length}) — includes Auto-Pilot ticks even when this tab was closed
          </summary>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full font-mono text-[10px]">
              <thead>
                <tr className="text-zinc-600 text-left">
                  <th className="py-1 pr-3">Time</th>
                  <th className="py-1 pr-3">Trigger</th>
                  <th className="py-1 pr-3">Duration</th>
                  <th className="py-1 pr-3">Candidates</th>
                  <th className="py-1 pr-3">Entered</th>
                  <th className="py-1 pr-3">Rejected</th>
                  <th className="py-1 pr-3">Closed</th>
                  <th className="py-1">Note</th>
                </tr>
              </thead>
              <tbody>
                {pipeline.history.map((c, i) => (
                  <tr key={i} className="border-t border-zinc-900 text-zinc-400">
                    <td className="py-1 pr-3">{fmtTime(c.ended_at)}</td>
                    <td className="py-1 pr-3">{c.trigger === "autopilot" ? "🤖 auto" : "▶ manual"}</td>
                    <td className="py-1 pr-3">{msFmt(c.duration_ms)}</td>
                    <td className="py-1 pr-3">{c.new_candidates ?? "—"}</td>
                    <td className="py-1 pr-3 text-emerald-400">{c.entered ?? "—"}</td>
                    <td className="py-1 pr-3 text-rose-400">{c.rejected ?? "—"}</td>
                    <td className="py-1 pr-3">{c.full_exits ?? "—"}</td>
                    <td className="py-1 text-rose-400">{c.error || c.auto_disarmed || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}

// ── Order flow diagram (how a trade executes) ────────────────────────────────
function OrderFlowDiagram({ mode }: { mode: Mode }) {
  const steps = [
    { id: "candidate", label: "Candidate", desc: "Hot Picks / IPO / Scan" },
    { id: "risk", label: "Risk Check", desc: "9 engine checks" },
    { id: "order", label: "Place Order", desc: mode === "REAL" ? "Dhan API (LIMIT)" : "Paper trade" },
    { id: "fill", label: "Fill", desc: mode === "REAL" ? "Reconcile w/ Dhan" : "Simulated price" },
    { id: "position", label: "Position Open", desc: "Stop + Target set" },
    { id: "exit", label: "Exit", desc: "Stop / Target / Trail / Time" },
  ];
  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4 mb-4">
      <SectionHdr>How a trade executes — {mode} mode</SectionHdr>
      <div className="flex items-center gap-0 overflow-x-auto pb-1">
        {steps.map((s, i) => (
          <div key={s.id} className="flex items-center flex-shrink-0">
            <div className="flex flex-col items-center gap-1 min-w-[80px]">
              <div className={`w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold border ${
                mode === "REAL"
                  ? "bg-amber-500/10 border-amber-500/40 text-amber-400"
                  : "bg-emerald-500/10 border-emerald-500/40 text-emerald-400"
              }`}>{i + 1}</div>
              <p className="text-[10px] font-mono font-bold text-zinc-200 text-center leading-tight">{s.label}</p>
              <p className="text-[9px] font-mono text-zinc-600 text-center leading-tight">{s.desc}</p>
            </div>
            {i < steps.length - 1 && (
              <div className="w-6 flex-shrink-0 flex items-center justify-center mb-6">
                <div className="h-px w-full bg-zinc-700" />
                <span className="text-zinc-600 text-[9px] -ml-1">›</span>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Balance allocation bar ───────────────────────────────────────────────────
function BalanceAllocation({ funds, positions }: { funds: any; positions: any[] }) {
  const available = pickNum(funds, "availabelBalance", "availableBalance", "availableCash") ?? 0;
  const utilized = pickNum(funds, "utilizedAmount", "utilisedAmount") ?? 0;
  const collateral = pickNum(funds, "collateralAmount") ?? 0;
  const total = available + utilized + (collateral > 0 ? collateral : 0);
  const pct = (v: number) => total > 0 ? Math.round((v / total) * 100) : 0;

  // Per-position allocation from broker positions array
  const positionsValue = positions.reduce((sum, p) => {
    const qty = Number(p.buyQty || p.netQty || 0);
    const avg = Number(p.averageBuyPrice || p.costPrice || 0);
    return sum + qty * avg;
  }, 0);

  return (
    <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4 mb-4">
      <SectionHdr>Balance allocation</SectionHdr>
      <div className="space-y-3">
        {/* Bar */}
        <div className="h-3 rounded-full bg-zinc-800 flex overflow-hidden">
          <div className="bg-emerald-500/70 transition-all" style={{ width: `${pct(available)}%` }} />
          <div className="bg-amber-500/70 transition-all" style={{ width: `${pct(utilized)}%` }} />
          {collateral > 0 && <div className="bg-sky-500/70 transition-all" style={{ width: `${pct(collateral)}%` }} />}
        </div>
        <div className="grid grid-cols-3 gap-2 text-[10px] font-mono">
          <div><span className="inline-block w-2 h-2 rounded-sm bg-emerald-500/70 mr-1" />Available<br/><span className="text-zinc-200 font-bold">{fmtInr(available, 0)}</span></div>
          <div><span className="inline-block w-2 h-2 rounded-sm bg-amber-500/70 mr-1" />Utilized<br/><span className="text-zinc-200 font-bold">{fmtInr(utilized, 0)}</span></div>
          {collateral > 0 && <div><span className="inline-block w-2 h-2 rounded-sm bg-sky-500/70 mr-1" />Collateral<br/><span className="text-zinc-200 font-bold">{fmtInr(collateral, 0)}</span></div>}
        </div>
        {positions.length > 0 && (
          <div className="border-t border-zinc-800 pt-2">
            <p className="text-[9px] font-mono text-zinc-600 mb-1">OPEN BROKER POSITIONS</p>
            <div className="space-y-1">
              {positions.slice(0, 6).map((p, i) => {
                const sym = p.tradingSymbol || p.symbol || "—";
                const qty = Number(p.buyQty || p.netQty || 0);
                const avg = Number(p.averageBuyPrice || p.costPrice || 0);
                const val = qty * avg;
                const pnl = Number(p.unrealizedProfit || p.dayBuyValue || 0);
                const valPct = total > 0 ? Math.round((val / total) * 100) : 0;
                return (
                  <div key={i} className="flex items-center gap-2">
                    <div className="w-24 font-mono text-[10px] text-zinc-300 truncate">{sym}</div>
                    <div className="flex-1 h-1.5 bg-zinc-800 rounded-full overflow-hidden">
                      <div className="h-full bg-amber-500/60 rounded-full" style={{ width: `${valPct}%` }} />
                    </div>
                    <div className="w-16 text-right font-mono text-[10px] text-zinc-400">{fmtInr(val, 0)}</div>
                    <div className={`w-14 text-right font-mono text-[10px] ${pnlColor(pnl)}`}>{pnl >= 0 ? "+" : ""}{fmtInr(pnl, 0)}</div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Gate sequence steps ──────────────────────────────────────────────────────
function GateStep({ n, label, done, active }: { n: number; label: string; done: boolean; active: boolean }) {
  return (
    <div className={`flex items-center gap-2 px-3 py-2 rounded-lg border transition-colors ${
      done ? "bg-emerald-500/5 border-emerald-500/20" :
      active ? "bg-amber-500/10 border-amber-500/30 animate-pulse" :
      "bg-zinc-900 border-zinc-800"
    }`}>
      <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0 ${
        done ? "bg-emerald-500/20 text-emerald-400" :
        active ? "bg-amber-500/20 text-amber-400" :
        "bg-zinc-800 text-zinc-600"
      }`}>{done ? "✓" : n}</div>
      <span className={`text-[11px] font-mono ${done ? "text-emerald-300" : active ? "text-amber-300" : "text-zinc-600"}`}>{label}</span>
    </div>
  );
}

// ── Main component ───────────────────────────────────────────────────────────
export default function RealAutoTrade() {
  const [mode, setMode] = useState<Mode>("DEMO");
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [apiUrlInput, setApiUrlInput] = useState(getRealTradeApiUrl());

  const [status, setStatus] = useState<GateStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const [loggedIn, setLoggedIn] = useState(!!getSessionToken());
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");

  const [dhanClientId, setDhanClientId] = useState("");
  const [dhanToken, setDhanToken] = useState("");
  const [dhanAccount, setDhanAccount] = useState<(DhanStatus & { funds: any; funds_error: string | null }) | null>(null);
  const [networkCheck, setNetworkCheck] = useState<{ outbound_ip: string | null; note: string } | null>(null);
  const [networkCheckBusy, setNetworkCheckBusy] = useState(false);
  const [showDhanForm, setShowDhanForm] = useState(false);
  const [nowTick, setNowTick] = useState(() => Date.now());

  // Live Dhan data
  const [livePositions, setLivePositions] = useState<any[]>([]);
  const [liveHoldings, setLiveHoldings] = useState<any[]>([]);
  const [liveDhanOrders, setLiveDhanOrders] = useState<any[]>([]);
  const [liveLoading, setLiveLoading] = useState(false);
  const [liveError, setLiveError] = useState<string | null>(null);

  // Service positions/orders
  const [positions, setPositions] = useState<Position[]>([]);
  const [orders, setOrders] = useState<OrderRow[]>([]);
  const [orderRange, setOrderRange] = useState<"today" | "all">("today");

  const [auditRows, setAuditRows] = useState<AuditLogRow[]>([]);
  const [cycleBusy, setCycleBusy] = useState(false);
  const [autoPilotBusy, setAutoPilotBusy] = useState(false);
  const [featureBusy, setFeatureBusy] = useState<string | null>(null);
  const [cycleResult, setCycleResult] = useState<CycleResult | null>(null);
  const [pipeline, setPipeline] = useState<PipelineStatus | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [candidates, setCandidates] = useState<CandidateRow[]>([]);
  const [candidatesLoading, setCandidatesLoading] = useState(false);
  const [expandedCandidateId, setExpandedCandidateId] = useState<number | null>(null);

  // Editable risk configuration form — separate from `status.risk_config`
  // (the last-saved server value) so typing doesn't fight a background
  // status poll; only synced FROM the server on load/mode-change/save, and
  // only ever sent back on an explicit Save click.
  const [riskForm, setRiskForm] = useState<{
    risk_per_trade_pct: string; max_daily_loss_pct: string; max_concurrent_positions: string;
    max_portfolio_risk_pct: string; stale_data_seconds: string; max_tick_volatility_mult: string;
    allow_pyramiding: boolean;
  } | null>(null);
  const [riskSaving, setRiskSaving] = useState(false);
  const [riskMsg, setRiskMsg] = useState<{ ok: boolean; text: string } | null>(null);

  // Tick for countdown
  useEffect(() => {
    const id = window.setInterval(() => setNowTick(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const secondsRemaining = dhanAccount?.token_expires_at
    ? Math.max(0, Math.floor((new Date(dhanAccount.token_expires_at).getTime() - nowTick) / 1000))
    : null;

  const loadStatus = useCallback(async (m: Mode) => {
    try {
      const s = await realTradeApi.gateStatus(m);
      setStatus(s);
      // A "not armed" error (from a Run Cycle / manual-BUY attempt made
      // before arming) is a snapshot of that moment, not a live-bound
      // banner — it doesn't re-check itself against current state, so it
      // can keep showing after the user goes on to arm successfully. Every
      // successful status poll is a fresher truth than that old toast, so
      // once the gate now says armed, clear it rather than leaving a
      // contradictory "not armed" message next to a green ARMED badge.
      setError(prev => (prev && s.armed && /not armed/i.test(prev)) ? null : prev);
      if (s.risk_config) {
        setRiskForm({
          risk_per_trade_pct: String(s.risk_config.risk_per_trade_pct ?? ""),
          max_daily_loss_pct: String(s.risk_config.max_daily_loss_pct ?? ""),
          max_concurrent_positions: String(s.risk_config.max_concurrent_positions ?? ""),
          max_portfolio_risk_pct: String(s.risk_config.max_portfolio_risk_pct ?? ""),
          stale_data_seconds: String(s.risk_config.stale_data_seconds ?? ""),
          max_tick_volatility_mult: String(s.risk_config.max_tick_volatility_mult ?? ""),
          allow_pyramiding: !!s.risk_config.allow_pyramiding,
        });
      }
      if (m === "REAL" && getSessionToken()) {
        try {
          const d = await realTradeApi.dhanAccount();
          setDhanAccount(d.connected ? d : null);
        } catch { setDhanAccount(null); }
      } else {
        setDhanAccount(null);
      }
    } catch (e: any) {
      setError(e?.message || "Failed to load status");
    }
  }, []);

  const loadPositionsAndOrders = useCallback(async (m: Mode) => {
    try {
      // 100 (not 20) so the "All" orders sub-tab has real history to show —
      // "Today" is just a client-side filter over this same fetch, not a
      // separate call.
      const [p, o] = await Promise.all([realTradeApi.positions(m), realTradeApi.orders(m, 100)]);
      setPositions(p);
      setOrders(o);
    } catch { /* best-effort */ }
  }, []);

  const isSameLocalDay = (iso: string, ref: Date): boolean => {
    const d = new Date(iso);
    return d.getFullYear() === ref.getFullYear()
      && d.getMonth() === ref.getMonth()
      && d.getDate() === ref.getDate();
  };

  // Watchlist tab — what the engine has fetched/evaluated recently, with a
  // live price alongside whatever it's waiting on. The tab itself polls
  // every 5s while open (see effect below); this is the one-shot loader
  // also used just to populate the tab count badge on mode change.
  const loadCandidates = useCallback(async (m: Mode) => {
    setCandidatesLoading(true);
    try {
      setCandidates(await realTradeApi.candidates(m, 40));
    } catch { /* best-effort */ }
    finally { setCandidatesLoading(false); }
  }, []);

  const loadLiveDhanData = async () => {
    if (!getSessionToken() || mode !== "REAL") return;
    setLiveLoading(true);
    setLiveError(null);
    try {
      const base = getRealTradeApiUrl();
      const token = getSessionToken();
      const h = { "Content-Type": "application/json", "Authorization": `Bearer ${token}` };
      const [posRes, holdRes, ordRes] = await Promise.all([
        fetch(`${base}/dhan/positions`, { headers: h }).then(r => r.json()),
        fetch(`${base}/dhan/holdings`, { headers: h }).then(r => r.json()),
        fetch(`${base}/dhan/orders`, { headers: h }).then(r => r.json()),
      ]);
      setLivePositions(posRes.positions || []);
      setLiveHoldings(holdRes.holdings || []);
      setLiveDhanOrders(ordRes.orders || []);
    } catch (e: any) {
      setLiveError(e?.message || "Failed to load live Dhan data");
    } finally {
      setLiveLoading(false);
    }
  };

  useEffect(() => {
    if (getRealTradeApiUrl()) void loadStatus(mode);
  }, [mode, loadStatus]);

  // Registers once: the moment ANY request (including a background poll the
  // user never directly triggered) discovers the session has expired, flip
  // `loggedIn` immediately instead of leaving it stuck true from the last
  // explicit login. See the long comment on setSessionExpiredHandler in
  // realTradeApi.ts for why this used to go stale.
  useEffect(() => {
    setSessionExpiredHandler(() => {
      setLoggedIn(false);
      setError("Session expired — log in again to arm, run a cycle, or change settings. Auto-Pilot (if it was on) keeps running.");
    });
    return () => setSessionExpiredHandler(null);
  }, []);

  useEffect(() => {
    // Load read-only data when armed even without an active admin session —
    // auto-pilot runs through market hours with dashboard closed/logged-out.
    if (getRealTradeApiUrl() && (mode === "DEMO" || loggedIn || status?.armed)) {
      void loadPositionsAndOrders(mode);
      void loadCandidates(mode); // one-shot, just for the tab count badge — the watchlist tab itself polls
    }
  }, [mode, loggedIn, status?.armed, loadPositionsAndOrders, loadCandidates]);

  useEffect(() => {
    if (activeTab === "live" && mode === "REAL" && loggedIn) {
      void loadLiveDhanData();
    }
    if (activeTab === "log") void loadAudit();
  }, [activeTab, mode, loggedIn]);

  // Live pipeline polling — shows what the cycle (manual OR Auto-Pilot,
  // even if triggered while this tab wasn't open) is doing right now: which
  // stage, which symbol, how long each stage took, plus recent history.
  // Polls every 2s while the Pipeline tab is open; stops the moment it
  // isn't, so this never runs up requests in the background.
  useEffect(() => {
    // Pipeline tab is readable when armed even without login session
    if (activeTab !== "pipeline" || !getRealTradeApiUrl() || (mode === "REAL" && !loggedIn && !status?.armed)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const p = await realTradeApi.pipelineStatus(mode);
        if (!cancelled) setPipeline(p);
      } catch {
        // best-effort — a failed poll just leaves the last known status showing
      }
    };
    void poll();
    const id = setInterval(poll, 2000);
    return () => { cancelled = true; clearInterval(id); };
  }, [activeTab, mode, loggedIn]);

  useEffect(() => {
    if (activeTab !== "watchlist" || !getRealTradeApiUrl() || (mode === "REAL" && !loggedIn && !status?.armed)) return;
    let cancelled = false;
    const poll = async () => {
      try {
        const c = await realTradeApi.candidates(mode, 40);
        if (!cancelled) setCandidates(c);
      } catch { /* best-effort */ }
    };
    void poll();
    const id = setInterval(poll, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [activeTab, mode, loggedIn]);

  const saveApiUrl = () => { setRealTradeApiUrl(apiUrlInput); void loadStatus(mode); };

  const doLogin = async () => {
    setLoading(true); setError(null);
    try {
      const res = await realTradeApi.login(username, password);
      setSessionToken(res.token); setLoggedIn(true); setPassword("");
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Login failed"); }
    finally { setLoading(false); }
  };

  const doLogout = async () => {
    try { await realTradeApi.logout(); } catch { }
    setSessionToken(null); setLoggedIn(false); void loadStatus(mode);
  };

  const doConnectDhan = async () => {
    setLoading(true); setError(null);
    try {
      await realTradeApi.connectDhan(dhanClientId, dhanToken);
      setDhanToken(""); setDhanClientId(""); setShowDhanForm(false);
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Failed to connect Dhan"); }
    finally { setLoading(false); }
  };

  // Separate loading flag from the shared `loading` above — regenerating a
  // token shouldn't visually disable/spin every other button on the panel
  // (arm/disarm, save-token, refresh) while its own request is in flight.
  const [regenLoading, setRegenLoading] = useState(false);
  const [regenNote, setRegenNote] = useState<string | null>(null);

  const doRegenerateToken = async () => {
    setRegenLoading(true); setError(null); setRegenNote(null);
    try {
      await realTradeApi.dhanRegenerateToken();
      setRegenNote("Token regenerated ✓");
      await loadStatus(mode);
    } catch (e: any) {
      // 409 = TOTP not configured on this deploy — surface that distinct
      // message rather than a generic failure so it's obvious "Rotate
      // token" (manual paste) is the right move instead of retrying this.
      setError(e?.message || "Failed to regenerate token");
    } finally {
      setRegenLoading(false);
      setTimeout(() => setRegenNote(null), 4000);
    }
  };

  const doArm = async () => {
    setLoading(true); setError(null);
    try { await realTradeApi.arm(mode); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to arm"); }
    finally { setLoading(false); }
  };

  const doDisarm = async () => {
    try { await realTradeApi.disarm(mode); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to disarm"); }
  };

  const doEmergencyPause = async () => {
    if (!window.confirm("Pause ALL trading (DEMO + REAL)? Disarms both modes immediately.")) return;
    try { await realTradeApi.emergencyPause(); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to pause"); }
  };

  const doConfirmRisk = async () => {
    try { await realTradeApi.confirmRiskConfig(mode); await loadStatus(mode); }
    catch (e: any) { setError(e?.message || "Failed to confirm risk config"); }
  };

  const doNetworkCheck = async () => {
    setNetworkCheckBusy(true);
    try { setNetworkCheck(await realTradeApi.dhanNetworkCheck()); }
    catch (e: any) { setNetworkCheck({ outbound_ip: null, note: e?.message || "Lookup failed" }); }
    finally { setNetworkCheckBusy(false); }
  };

  const doSaveRiskConfig = async () => {
    if (!riskForm) return;
    setRiskSaving(true); setRiskMsg(null);
    try {
      await realTradeApi.updateRiskConfig(mode, {
        risk_per_trade_pct: Number(riskForm.risk_per_trade_pct),
        max_daily_loss_pct: Number(riskForm.max_daily_loss_pct),
        max_concurrent_positions: Number(riskForm.max_concurrent_positions),
        max_portfolio_risk_pct: Number(riskForm.max_portfolio_risk_pct),
        stale_data_seconds: Number(riskForm.stale_data_seconds),
        max_tick_volatility_mult: Number(riskForm.max_tick_volatility_mult),
        allow_pyramiding: riskForm.allow_pyramiding,
      });
      setRiskMsg({ ok: true, text: "Risk configuration saved." });
      await loadStatus(mode);
    } catch (e: any) {
      setRiskMsg({ ok: false, text: e?.message || "Failed to save risk configuration" });
    } finally { setRiskSaving(false); }
  };

  const loadAudit = async () => {
    try { setAuditRows(await realTradeApi.auditLog(mode, 30)); } catch { }
  };

  const doClosePosition = async (p: Position) => {
    const key = `close:${p.id}`;
    setActionBusy(key); setActionMsg(null);
    try {
      const res = await realTradeApi.closePosition(mode, p.id);
      setActionMsg({ ok: true, text: res.status === "pending_broker_confirmation"
        ? `${p.symbol} close sent to Dhan.`
        : `${p.symbol} closed (P&L ${res.pnl?.toFixed(2)}).` });
      await loadPositionsAndOrders(mode);
    } catch (e: any) { setActionMsg({ ok: false, text: e?.message || `Failed to close ${p.symbol}` }); }
    finally { setActionBusy(null); }
  };

  const doCancelOrder = async (o: OrderRow) => {
    const key = `cancel:${o.id}`;
    setActionBusy(key); setActionMsg(null);
    try {
      await realTradeApi.cancelOrder(mode, o.id);
      setActionMsg({ ok: true, text: `${o.symbol} order cancelled.` });
      await loadPositionsAndOrders(mode);
    } catch (e: any) { setActionMsg({ ok: false, text: e?.message || `Failed to cancel` }); }
    finally { setActionBusy(null); }
  };

  const doReconcile = async () => {
    setActionBusy("reconcile"); setActionMsg(null);
    try {
      const res = await realTradeApi.reconcile(mode);
      setActionMsg({ ok: true, text: res.note || `Checked ${res.checked ?? 0} · filled ${res.entries_filled ?? 0} · exits ${res.exits_confirmed ?? 0}` });
      await loadPositionsAndOrders(mode);
    } catch (e: any) { setActionMsg({ ok: false, text: e?.message || "Reconcile failed" }); }
    finally { setActionBusy(null); }
  };

  const doRunCycle = async () => {
    setCycleBusy(true); setError(null);
    try {
      const res = await realTradeApi.runCycle(mode);
      setCycleResult(res);
      await Promise.all([loadStatus(mode), loadPositionsAndOrders(mode)]);
    } catch (e: any) { setError(e?.message || "Cycle failed"); }
    finally { setCycleBusy(false); }
  };

  const doToggleAutoPilot = async () => {
    setAutoPilotBusy(true); setError(null);
    try {
      if (status?.auto_pilot_enabled) {
        await realTradeApi.disableAutoPilot(mode);
      } else {
        await realTradeApi.enableAutoPilot(mode);
      }
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Auto-Pilot toggle failed"); }
    finally { setAutoPilotBusy(false); }
  };

  const doToggleFeature = async (
    feature: "prepick" | "enter_at_open" | "eod_squareoff",
    currentlyEnabled: boolean,
  ) => {
    setFeatureBusy(feature); setError(null);
    try {
      await realTradeApi.setFeature(mode, feature, !currentlyEnabled);
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Automation toggle failed"); }
    finally { setFeatureBusy(null); }
  };

  // ── No URL configured ────────────────────────────────────────────────────
  if (!getRealTradeApiUrl()) {
    return (
      <div className="p-4 max-w-lg mx-auto">
        <p className="font-mono text-xs text-zinc-400 mb-1 uppercase tracking-widest">Real Trade Service URL</p>
        <p className="font-mono text-[11px] text-zinc-600 mb-3">Paste your real-trade-service URL (separate Render deploy from api-gateway).</p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-zinc-900 border border-zinc-700 rounded-lg px-3 py-2 font-mono text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
            placeholder="https://stockky-real-trade.onrender.com"
            value={apiUrlInput}
            onChange={e => setApiUrlInput(e.target.value)}
          />
          <button onClick={saveApiUrl} className="px-4 py-2 rounded-lg bg-emerald-600/20 border border-emerald-500/40 font-mono text-xs text-emerald-300">Save</button>
        </div>
      </div>
    );
  }

  // ── Gate computation ─────────────────────────────────────────────────────
  const gate1 = status?.admin_authenticated ?? false;
  const gate2 = mode === "DEMO" ? true : (status?.dhan_connected ?? false);
  const gate3 = status?.risk_config_confirmed ?? false;
  const armed = status?.armed ?? false;

  const nextGateNeeded = !gate1 ? 1 : !gate2 ? 2 : !gate3 ? 3 : armed ? null : 4;

  const tabs: { id: Tab; label: string }[] = [
    { id: "overview", label: "Overview" },
    { id: "live", label: "Live Dhan" },
    { id: "positions", label: `Positions (${positions.length})` },
    { id: "orders", label: `Orders (${orders.length})` },
    { id: "watchlist", label: `Watchlist (${candidates.length})` },
    { id: "pipeline", label: "Pipeline" },
    { id: "log", label: "Activity" },
  ];

  return (
    <div className="max-w-3xl mx-auto pb-8">
      {/* ── Header bar ───────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between mb-1 px-1">
        <div className="flex items-center gap-2">
          <span className="text-base">🤖</span>
          <span className="font-mono text-sm font-bold text-zinc-100">Real Auto Trade</span>
          {armed && <span className="text-[10px] font-mono px-2 py-0.5 rounded-full bg-emerald-500/15 border border-emerald-500/30 text-emerald-400 animate-pulse">ARMED</span>}
        </div>
        <div className="flex gap-1.5">
          {(["DEMO", "REAL"] as Mode[]).map(m => (
            <button key={m} onClick={() => setMode(m)} className={`px-3 py-1 rounded-lg font-mono text-[11px] border transition-colors ${
              mode === m
                ? m === "DEMO" ? "bg-emerald-500/15 border-emerald-500/30 text-emerald-300" : "bg-rose-500/15 border-rose-500/30 text-rose-300"
                : "bg-zinc-900 border-zinc-800 text-zinc-600"
            }`}>
              <Dot on={mode === m && armed} pulse /> {m}
            </button>
          ))}
        </div>
      </div>

      {/* Phase notice */}
      <p className="font-mono text-[10px] text-amber-400/70 mb-3 px-1">
        Phase 2 active — DEMO fully wired (candidates→risk→fill→exit). REAL places live Dhan orders & reconciles fills.
      </p>

      {/* Error banner */}
      {error && (
        <div className="mb-3 px-3 py-2 rounded-lg bg-rose-950/40 border border-rose-500/30 font-mono text-xs text-rose-300 flex items-start justify-between gap-2">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="text-rose-500 hover:text-rose-300 flex-shrink-0">✕</button>
        </div>
      )}

      {/* ── Auth / session bar (REAL only) ──────────────────────────────── */}
      {mode === "REAL" && !loggedIn && !status?.armed ? (
        <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4 mb-4">
          <SectionHdr>Admin login required for REAL mode</SectionHdr>
          <div className="space-y-2">
            <input className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-3 py-2 font-mono text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
              placeholder="Admin username" value={username} onChange={e => setUsername(e.target.value)} />
            <input type="password" className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-3 py-2 font-mono text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
              placeholder="Password" value={password} onChange={e => setPassword(e.target.value)}
              onKeyDown={e => e.key === "Enter" && void doLogin()} />
            <button onClick={() => void doLogin()} disabled={loading || !password}
              className="w-full py-2 rounded-lg bg-sky-500/15 border border-sky-500/30 font-mono text-xs text-sky-300 disabled:opacity-40">
              {loading ? "Authenticating…" : "Authenticate"}
            </button>
          </div>
        </div>
      ) : (
        <>
          {mode === "REAL" && (
            loggedIn ? (
              <div className="flex items-center justify-between mb-3 px-3 py-1.5 rounded-lg bg-emerald-500/5 border border-emerald-500/20">
                <span className="font-mono text-[11px] text-emerald-400 flex items-center gap-1.5">
                  <Dot on={true} /> Admin session active ({username})
                </span>
                <button onClick={() => void doLogout()} className="font-mono text-[10px] px-2 py-1 rounded bg-rose-500/10 border border-rose-500/20 text-rose-400">
                  Log out
                </button>
              </div>
            ) : (
              <div className="flex items-center justify-between mb-3 px-3 py-1.5 rounded-lg bg-amber-500/5 border border-amber-500/20">
                <span className="font-mono text-[11px] text-amber-400">
                  Session expired — auto-pilot still running. Log in to make changes.
                </span>
                <button onClick={() => setActiveTab("overview")} className="font-mono text-[10px] px-2 py-1 rounded bg-sky-500/10 border border-sky-500/20 text-sky-400">
                  Log in
                </button>
              </div>
            )
          )}

          {/* ── Tab bar ─────────────────────────────────────────────────── */}
          <div className="flex gap-1 mb-4 overflow-x-auto pb-1">
            {tabs.map(t => (
              <button key={t.id} onClick={() => setActiveTab(t.id)}
                className={`px-3 py-1.5 rounded-lg font-mono text-[11px] whitespace-nowrap border transition-colors flex-shrink-0 ${
                  activeTab === t.id
                    ? "bg-zinc-700 border-zinc-600 text-zinc-100"
                    : "bg-zinc-900 border-zinc-800 text-zinc-500 hover:text-zinc-300"
                }`}>
                {t.label}
              </button>
            ))}
          </div>

          {/* ═══════════════════════════════════════════════════════════════
              TAB: OVERVIEW
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "overview" && (
            <div className="space-y-4">
              {/* Gate sequence */}
              <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                <SectionHdr>Arming sequence — {mode} mode</SectionHdr>
                <div className="grid grid-cols-2 gap-2 mb-4">
                  <GateStep n={1} label="Admin authenticated" done={gate1} active={nextGateNeeded === 1} />
                  <GateStep n={2} label={mode === "DEMO" ? "Dhan (not required)" : "Dhan connected"} done={gate2} active={nextGateNeeded === 2} />
                  <GateStep n={3} label="Risk config confirmed" done={gate3} active={nextGateNeeded === 3} />
                  <GateStep n={4} label="Armed" done={armed} active={nextGateNeeded === 4} />
                </div>

                {/* Inline login form when armed but session expired —
                    don't block the full page, just gate the mutating actions */}
                {mode === "REAL" && !loggedIn && armed && (
                  <div className="mb-3 p-3 rounded-lg border border-amber-500/20 bg-amber-500/5 space-y-2">
                    <p className="font-mono text-[10px] text-amber-400">Session expired — log in to disarm or change config. Auto-pilot keeps running.</p>
                    <div className="flex gap-2">
                      <input className="flex-1 bg-zinc-950 border border-zinc-700 rounded px-2 py-1 font-mono text-xs text-zinc-200 focus:outline-none"
                        placeholder="Username" value={username} onChange={e => setUsername(e.target.value)} />
                      <input type="password" className="flex-1 bg-zinc-950 border border-zinc-700 rounded px-2 py-1 font-mono text-xs text-zinc-200 focus:outline-none"
                        placeholder="Password" value={password} onChange={e => setPassword(e.target.value)}
                        onKeyDown={e => e.key === "Enter" && void doLogin()} />
                      <button onClick={() => void doLogin()} disabled={loading || !password}
                        className="px-3 py-1 rounded bg-sky-500/15 border border-sky-500/30 font-mono text-xs text-sky-300 disabled:opacity-40">
                        {loading ? "…" : "Login"}
                      </button>
                    </div>
                  </div>
                )}

                {/* Login form already shown above when not logged in; here only if
                    logged in but gate not met */}
                {status && !gate3 && mode === "REAL" && gate1 && gate2 && (
                  <button onClick={() => void doConfirmRisk()}
                    className="w-full py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 font-mono text-xs text-amber-300">
                    Confirm risk configuration
                  </button>
                )}
                {status && !gate3 && mode === "DEMO" && (
                  <button onClick={() => void doConfirmRisk()}
                    className="w-full py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 font-mono text-xs text-amber-300">
                    Confirm risk configuration
                  </button>
                )}

                <div className="flex gap-2 mt-2">
                  {armed ? (
                    <button onClick={() => void doDisarm()} className="flex-1 py-2.5 rounded-lg bg-rose-500/10 border border-rose-500/30 font-mono text-xs text-rose-300">
                      🛑 DISARM
                    </button>
                  ) : (
                    <button onClick={() => void doArm()} disabled={loading || !gate1 || !gate2 || !gate3}
                      className="flex-1 py-2.5 rounded-lg bg-emerald-500/10 border border-emerald-500/30 font-mono text-xs text-emerald-300 disabled:opacity-30">
                      {loading ? "Arming…" : "⚡ ARM"}
                    </button>
                  )}
                  <button onClick={() => void doEmergencyPause()} className="px-4 py-2.5 rounded-lg bg-red-900/30 border border-red-500/40 font-mono text-xs text-red-300">
                    🚨 PAUSE ALL
                  </button>
                </div>
                {status?.disarmed_reason && (
                  /outbound IP|invalid ip|not whitelisted/i.test(status.disarmed_reason) ? (
                    <div className="mt-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-3 py-2">
                      <p className="font-mono text-[10px] text-amber-300">
                        🚨 Auto-paused: Dhan rejected an order because this service's outbound IP isn't whitelisted.
                      </p>
                      <p className="font-mono text-[9px] text-zinc-500 mt-1">
                        Reads (funds/positions) still work — only order placement is IP-gated by Dhan, which is
                        why the account shows connected while this happens.
                      </p>
                      <div className="flex items-center gap-2 mt-2">
                        <button onClick={() => void doNetworkCheck()} disabled={networkCheckBusy}
                          className="font-mono text-[10px] px-3 py-1 rounded-lg bg-amber-500/10 border border-amber-500/30 text-amber-200 disabled:opacity-40">
                          {networkCheckBusy ? "Checking…" : "Check outbound IP"}
                        </button>
                        {networkCheck?.outbound_ip && (
                          <code className="font-mono text-[10px] text-zinc-200 bg-zinc-950 px-2 py-1 rounded">{networkCheck.outbound_ip}</code>
                        )}
                      </div>
                      {networkCheck && (
                        <p className="font-mono text-[9px] text-zinc-600 mt-1">{networkCheck.note}</p>
                      )}
                      <p className="font-mono text-[9px] text-zinc-600 mt-1">
                        Add that IP under Dhan Web → My Profile → API Access → IP Whitelisting, then re-arm.
                        A non-static host IP can change on redeploy — re-check if this recurs.
                      </p>
                    </div>
                  ) : (
                    <p className="font-mono text-[10px] text-amber-400/60 mt-2">Last disarm: {status.disarmed_reason}</p>
                  )
                )}
              </div>

              {/* REAL: Dhan account card */}
              {mode === "REAL" && (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                  <div className="flex items-center justify-between mb-3">
                    <SectionHdr>Dhan account</SectionHdr>
                    <span className={`font-mono text-[10px] px-2 py-0.5 rounded-full border ${
                      dhanAccount?.connected ? "bg-emerald-500/10 border-emerald-500/30 text-emerald-400" : "bg-rose-500/10 border-rose-500/30 text-rose-400"
                    }`}>
                      {dhanAccount?.connected ? "🟢 Connected" : "🔴 Disconnected"}
                    </span>
                  </div>

                  {dhanAccount?.connected ? (
                    <div className="space-y-3">
                      <div className="grid grid-cols-2 gap-2 text-[11px] font-mono text-zinc-500">
                        <div>Client ID <span className="text-zinc-200 ml-1">{dhanAccount.client_id_masked}</span></div>
                        <div>Token <span className={`ml-1 ${(secondsRemaining ?? 0) < 7200 ? "text-rose-400" : "text-emerald-400"}`}>
                          {secondsRemaining != null ? (secondsRemaining <= 0 ? "expired" : fmtHms(secondsRemaining) + " left") : "—"}
                        </span></div>
                      </div>
                      <p className="font-mono text-[9px] text-zinc-700 -mt-2">
                        {dhanAccount.token_issued_at && `Issued ${fmtDate(dhanAccount.token_issued_at)} ${fmtTime(dhanAccount.token_issued_at)} · `}
                        Dhan hard-caps every token at {dhanAccount.token_hard_cap_hours ?? 24}h regardless of when it was generated.
                      </p>

                      {dhanAccount.funds_error ? (
                        <p className="font-mono text-[11px] text-rose-300 bg-rose-500/5 rounded-lg px-3 py-2 border border-rose-500/20">
                          ⚠ Live funds check failed: {dhanAccount.funds_error}
                        </p>
                      ) : dhanAccount.funds ? (
                        <div className="grid grid-cols-3 gap-2">
                          {[
                            { label: "Available", keys: ["availabelBalance", "availableBalance", "availableCash"] },
                            { label: "Utilized", keys: ["utilizedAmount", "utilisedAmount"] },
                            { label: "Withdrawable", keys: ["withdrawableBalance"] },
                            { label: "SOD Limit", keys: ["sodLimit"] },
                            { label: "Collateral", keys: ["collateralAmount"] },
                            { label: "Blocked", keys: ["blockedPayoutAmount"] },
                          ].map(f => {
                            const v = pickNum(dhanAccount.funds, ...f.keys);
                            return (
                              <div key={f.label} className="bg-zinc-950 border border-zinc-800 rounded-lg p-2">
                                <p className="font-mono text-[9px] uppercase tracking-widest text-zinc-600">{f.label}</p>
                                <p className="font-mono text-sm font-bold text-zinc-100 mt-0.5">{fmtInr(v, 0)}</p>
                              </div>
                            );
                          })}
                        </div>
                      ) : null}

                      <div className="flex items-center gap-2 flex-wrap">
                        <button onClick={() => void loadStatus(mode)} className="font-mono text-[10px] text-sky-400 hover:text-sky-300">
                          ↻ Refresh
                        </button>
                        <button onClick={() => setShowDhanForm(!showDhanForm)} className="font-mono text-[10px] text-zinc-500 hover:text-zinc-300">
                          Rotate token
                        </button>
                        {/* Manual trigger for the same TOTP auto-regenerate path the
                            background loop uses — for when it expired and the scheduled
                            refresh didn't fire in time. Safe to click anytime; a 409
                            (TOTP not configured on this deploy) surfaces in the error
                            banner and points at "Rotate token" instead. */}
                        <button
                          onClick={() => void doRegenerateToken()}
                          disabled={regenLoading}
                          title="Manually trigger TOTP-based token regeneration (requires DHAN_TOTP_ENABLED)"
                          className="font-mono text-[10px] text-amber-400 hover:text-amber-300 disabled:opacity-40"
                        >
                          {regenLoading ? "Regenerating…" : "⟳ Regenerate token"}
                        </button>
                        {regenNote && (
                          <span className="font-mono text-[10px] text-emerald-400">{regenNote}</span>
                        )}
                      </div>
                    </div>
                  ) : (
                    <p className="font-mono text-[11px] text-zinc-600">Connect Dhan below to enable REAL trading and see live balance.</p>
                  )}

                  {/* Dhan connect form */}
                  {(!dhanAccount?.connected || showDhanForm) && (
                    <div className="mt-3 space-y-2 border-t border-zinc-800 pt-3">
                      <p className="font-mono text-[10px] text-zinc-500">{dhanAccount?.connected ? "Paste a fresh access token to rotate" : "Connect your Dhan account"}</p>
                      <input className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-3 py-2 font-mono text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
                        placeholder="Dhan Client ID" value={dhanClientId} onChange={e => setDhanClientId(e.target.value)} />
                      <input type="password" className="w-full bg-zinc-950 border border-zinc-700 rounded-lg px-3 py-2 font-mono text-xs text-zinc-200 focus:outline-none focus:border-zinc-500"
                        placeholder="Access Token (generate at web.dhan.co → DhanHQ APIs)" value={dhanToken} onChange={e => setDhanToken(e.target.value)} />
                      <div className="flex gap-2">
                        <button onClick={() => void doConnectDhan()} disabled={loading || !dhanClientId || !dhanToken}
                          className="flex-1 py-2 rounded-lg bg-sky-500/10 border border-sky-500/30 font-mono text-xs text-sky-300 disabled:opacity-40">
                          {loading ? "Connecting…" : dhanAccount?.connected ? "Save new token" : "Connect Dhan"}
                        </button>
                        {dhanAccount?.connected && (
                          <button onClick={() => setShowDhanForm(false)}
                            className="px-3 py-2 rounded-lg border border-zinc-700 font-mono text-xs text-zinc-500">
                            Cancel
                          </button>
                        )}
                      </div>
                    </div>
                  )}
                </div>
              )}

              {/* Risk config — editable while disarmed, read-only snapshot while armed
                  (backend enforces the same rule and 409s otherwise, so the UI just
                  mirrors it instead of letting someone submit a doomed request). */}
              {status?.risk_config && (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                  <div className="flex items-center justify-between mb-1">
                    <SectionHdr>Risk configuration — {mode}</SectionHdr>
                    {armed && <span className="font-mono text-[9px] text-amber-400 uppercase">locked while armed</span>}
                  </div>
                  {riskForm && (
                    <div className="grid grid-cols-2 gap-3">
                      {([
                        ["risk_per_trade_pct", "Risk / trade (%)"],
                        ["max_daily_loss_pct", "Max daily loss (%)"],
                        ["max_concurrent_positions", "Max positions"],
                        ["max_portfolio_risk_pct", "Portfolio risk cap (%)"],
                        ["stale_data_seconds", "Stale data cutoff (s)"],
                        ["max_tick_volatility_mult", "Max tick volatility ×"],
                      ] as const).map(([key, label]) => (
                        <div key={key} className="bg-zinc-950 border border-zinc-800 rounded-lg p-2">
                          <p className="font-mono text-[9px] uppercase tracking-widest text-zinc-600">{label}</p>
                          <input
                            type="number" inputMode="decimal" disabled={armed}
                            value={riskForm[key]}
                            onChange={e => setRiskForm(f => f && { ...f, [key]: e.target.value })}
                            className="w-full bg-transparent font-mono text-sm font-bold text-zinc-100 mt-0.5 focus:outline-none disabled:opacity-60"
                          />
                        </div>
                      ))}
                      <label className="col-span-2 flex items-center gap-2 bg-zinc-950 border border-zinc-800 rounded-lg p-2 cursor-pointer">
                        <input type="checkbox" disabled={armed} checked={riskForm.allow_pyramiding}
                          onChange={e => setRiskForm(f => f && { ...f, allow_pyramiding: e.target.checked })}
                          className="accent-sky-500" />
                        <span className="font-mono text-[10px] text-zinc-400">Allow pyramiding (add to an existing open position)</span>
                      </label>
                    </div>
                  )}
                  {riskMsg && (
                    <p className={`font-mono text-[10px] mt-2 ${riskMsg.ok ? "text-emerald-400" : "text-rose-400"}`}>{riskMsg.text}</p>
                  )}
                  {status.risk_config.updated_at && (
                    <p className="font-mono text-[9px] text-zinc-700 mt-2">
                      Last saved {fmtDate(status.risk_config.updated_at)} {fmtTime(status.risk_config.updated_at)}
                      {status.risk_config.updated_by ? ` by ${status.risk_config.updated_by}` : ""}
                    </p>
                  )}
                  {!armed && (
                    <button onClick={() => void doSaveRiskConfig()} disabled={riskSaving}
                      className="mt-3 w-full py-2 rounded-lg bg-sky-500/10 border border-sky-500/30 font-mono text-xs text-sky-300 disabled:opacity-40">
                      {riskSaving ? "Saving…" : "Save changes"}
                    </button>
                  )}
                  {!gate3 && (
                    <button onClick={() => void doConfirmRisk()} className="mt-2 w-full py-2 rounded-lg bg-amber-500/10 border border-amber-500/30 font-mono text-xs text-amber-300">
                      Confirm this risk configuration
                    </button>
                  )}
                </div>
              )}

              {/* Stockky account snapshot */}
              {status?.account && (
                <div className="grid grid-cols-2 gap-2">
                  <StatCard label="Starting capital" value={fmtInr(status.account.starting_capital)} />
                  <StatCard label="Current equity" value={fmtInr(status.account.current_equity)} />
                  <StatCard label="Cash available" value={fmtInr(status.account.cash_available)} />
                  <StatCard
                    label="P&L today"
                    value={fmtInr(status.account.realized_pnl_today, 0)}
                    color={pnlColor(status.account.realized_pnl_today)}
                  />
                </div>
              )}

              {/* Order flow diagram */}
              <OrderFlowDiagram mode={mode} />

              {/* Manual trade ticket */}
              {(mode === "DEMO" || gate1) && (
                <ManualTradeTicket
                  mode={mode}
                  armed={armed}
                  onOrderComplete={() => { void loadPositionsAndOrders(mode); void loadStatus(mode); }}
                />
              )}
            </div>
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: LIVE DHAN DATA
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "live" && (
            <div className="space-y-4">
              {mode !== "REAL" ? (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-6 text-center">
                  <p className="font-mono text-sm text-zinc-500">Switch to REAL mode and log in to see live Dhan account data.</p>
                </div>
              ) : !loggedIn ? (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-6 text-center">
                  <p className="font-mono text-sm text-zinc-500">Log in to view live Dhan data.</p>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between">
                    <SectionHdr>Live data from Dhan broker</SectionHdr>
                    <button onClick={() => void loadLiveDhanData()} disabled={liveLoading}
                      className="font-mono text-[10px] px-3 py-1 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-400 disabled:opacity-40">
                      {liveLoading ? "Loading…" : "↻ Refresh all"}
                    </button>
                  </div>

                  {liveError && (
                    <div className="px-3 py-2 rounded-lg bg-rose-950/40 border border-rose-500/30 font-mono text-xs text-rose-300">{liveError}</div>
                  )}

                  {/* Balance allocation */}
                  {dhanAccount?.funds && (
                    <BalanceAllocation funds={dhanAccount.funds} positions={livePositions} />
                  )}

                  {/* Live positions */}
                  <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                    <SectionHdr>Open positions at Dhan ({livePositions.length})</SectionHdr>
                    {livePositions.length === 0 ? (
                      <p className="font-mono text-[11px] text-zinc-600">No open positions at broker.</p>
                    ) : (
                      <div className="space-y-2">
                        {livePositions.map((p, i) => {
                          const sym = p.tradingSymbol || p.symbol || "—";
                          const qty = Number(p.netQty || p.buyQty || 0);
                          const avg = Number(p.averageBuyPrice || p.costPrice || 0);
                          const ltp = Number(p.lastTradedPrice || p.ltp || 0);
                          const pnl = Number(p.unrealizedProfit || p.dayBuyValue || 0);
                          const product = p.positionType || p.productType || "";
                          return (
                            <div key={i} className="bg-zinc-950 rounded-lg px-3 py-2 border border-zinc-800">
                              <div className="flex items-center justify-between">
                                <div>
                                  <span className="font-mono text-sm font-bold text-zinc-100">{sym}</span>
                                  <span className="font-mono text-[10px] text-zinc-600 ml-2">{product}</span>
                                </div>
                                <span className={`font-mono text-sm font-bold ${pnlColor(pnl)}`}>
                                  {pnl >= 0 ? "+" : ""}{fmtInr(pnl, 2)}
                                </span>
                              </div>
                              <div className="flex gap-4 mt-1 font-mono text-[10px] text-zinc-500">
                                <span>Qty <span className="text-zinc-300">{qty}</span></span>
                                <span>Avg <span className="text-zinc-300">₹{avg.toFixed(2)}</span></span>
                                <span>LTP <span className="text-zinc-300">₹{ltp.toFixed(2)}</span></span>
                                <span>Val <span className="text-zinc-300">{fmtInr(qty * avg, 0)}</span></span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {/* Live holdings */}
                  <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                    <SectionHdr>Demat holdings ({liveHoldings.length})</SectionHdr>
                    {liveHoldings.length === 0 ? (
                      <p className="font-mono text-[11px] text-zinc-600">No holdings in demat.</p>
                    ) : (
                      <div className="overflow-x-auto">
                        <table className="w-full font-mono text-[11px]">
                          <thead>
                            <tr className="text-zinc-600 border-b border-zinc-800">
                              <th className="text-left py-1 pr-3">Symbol</th>
                              <th className="text-right pr-3">Qty</th>
                              <th className="text-right pr-3">Avg Cost</th>
                              <th className="text-right pr-3">LTP</th>
                              <th className="text-right">P&L</th>
                            </tr>
                          </thead>
                          <tbody>
                            {liveHoldings.map((h, i) => {
                              const sym = h.tradingSymbol || h.symbol || "—";
                              const qty = Number(h.totalQty || h.quantity || 0);
                              const avg = Number(h.avgCostPrice || h.averageBuyPrice || 0);
                              const ltp = Number(h.lastTradedPrice || h.ltp || 0);
                              const pnl = (ltp - avg) * qty;
                              return (
                                <tr key={i} className="border-b border-zinc-900 hover:bg-zinc-950">
                                  <td className="py-1.5 pr-3 text-zinc-200 font-bold">{sym}</td>
                                  <td className="text-right pr-3 text-zinc-400">{qty}</td>
                                  <td className="text-right pr-3 text-zinc-400">₹{avg.toFixed(2)}</td>
                                  <td className="text-right pr-3 text-zinc-400">₹{ltp.toFixed(2)}</td>
                                  <td className={`text-right ${pnlColor(pnl)}`}>{pnl >= 0 ? "+" : ""}{fmtInr(pnl, 0)}</td>
                                </tr>
                              );
                            })}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>

                  {/* Live orders from Dhan */}
                  <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                    <SectionHdr>Today's orders at Dhan ({liveDhanOrders.length})</SectionHdr>
                    {liveDhanOrders.length === 0 ? (
                      <p className="font-mono text-[11px] text-zinc-600">No orders placed today.</p>
                    ) : (
                      <div className="space-y-1.5 max-h-60 overflow-y-auto">
                        {liveDhanOrders.map((o, i) => {
                          const sym = o.tradingSymbol || o.symbol || "—";
                          const side = o.transactionType || o.side || "";
                          const qty = Number(o.quantity || 0);
                          const price = Number(o.price || o.averageTradedPrice || 0);
                          const status = (o.orderStatus || o.status || "").toUpperCase();
                          const statusColor = status === "TRADED" || status === "FILLED" ? "text-emerald-400"
                            : status === "REJECTED" || status === "CANCELLED" ? "text-rose-400"
                            : "text-amber-400";
                          return (
                            <div key={i} className="flex items-center justify-between bg-zinc-950 rounded-lg px-3 py-1.5 border border-zinc-800">
                              <div className="flex items-center gap-2 font-mono text-[11px]">
                                <span className={`font-bold ${side === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>{side}</span>
                                <span className="text-zinc-200">{sym}</span>
                                <span className="text-zinc-500">×{qty}</span>
                                {price > 0 && <span className="text-zinc-500">@ ₹{price.toFixed(2)}</span>}
                              </div>
                              <span className={`font-mono text-[10px] ${statusColor}`}>{status}</span>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: POSITIONS (Stockky service DB)
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "positions" && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <SectionHdr>Open positions — {mode} ({positions.length})</SectionHdr>
                <div className="flex gap-2">
                  {armed && (
                    <button onClick={() => void doReconcile()} disabled={actionBusy === "reconcile"}
                      className="font-mono text-[10px] px-3 py-1 rounded-lg bg-sky-500/10 border border-sky-500/30 text-sky-400 disabled:opacity-40">
                      {actionBusy === "reconcile" ? "Checking…" : "🔄 Reconcile"}
                    </button>
                  )}
                  <button onClick={() => void loadPositionsAndOrders(mode)}
                    className="font-mono text-[10px] px-3 py-1 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-400">
                    ↻
                  </button>
                </div>
              </div>

              {actionMsg && (
                <div className={`px-3 py-2 rounded-lg border font-mono text-xs ${actionMsg.ok ? "bg-emerald-500/5 border-emerald-500/20 text-emerald-300" : "bg-rose-500/5 border-rose-500/20 text-rose-300"}`}>
                  {actionMsg.text}
                </div>
              )}

              {positions.length === 0 ? (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-6 text-center">
                  <p className="font-mono text-sm text-zinc-600">No open positions.</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {positions.map(p => (
                    <div key={p.id} className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                      <div className="flex items-start justify-between mb-2">
                        <div>
                          <span className="font-mono text-base font-bold text-zinc-100">{p.symbol}</span>
                          <span className="font-mono text-[10px] text-zinc-600 ml-2">{p.status}</span>
                        </div>
                        <div className="text-right">
                          <span className={`font-mono text-base font-bold ${pnlColor(p.unrealized_pnl)}`}>
                            {p.unrealized_pnl >= 0 ? "+" : ""}{fmtInr(p.unrealized_pnl, 2)}
                          </span>
                          {p.pnl_pct != null && (
                            <span className={`font-mono text-[10px] ml-1 ${pnlColor(p.pnl_pct)}`}>
                              ({p.pnl_pct >= 0 ? "+" : ""}{p.pnl_pct}%)
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="grid grid-cols-5 gap-2 font-mono text-[10px] text-zinc-500 mb-3">
                        <div>Qty<br/><span className="text-zinc-200 font-bold">{p.qty_open}</span></div>
                        <div>Entry<br/><span className="text-zinc-200 font-bold">₹{p.avg_entry_price}</span></div>
                        <div>Current<br/><span className="text-sky-400 font-bold">{p.current_price != null ? `₹${p.current_price.toFixed(2)}` : "—"}</span></div>
                        <div>
                          Stop
                          {p.stop_distance_pct != null && <span className="text-zinc-700"> ({p.stop_distance_pct}%)</span>}
                          <br/><span className="text-rose-400 font-bold">{p.current_stop ? `₹${p.current_stop}` : "—"}</span>
                        </div>
                        <div>
                          Target
                          {p.target_distance_pct != null && <span className="text-zinc-700"> ({p.target_distance_pct}%)</span>}
                          <br/><span className="text-emerald-400 font-bold">{p.current_target ? `₹${p.current_target}` : "—"}</span>
                        </div>
                      </div>
                      {/* Risk:reward bar with a live marker for where current price sits between stop and target */}
                      {p.current_stop && p.current_target && (
                        <div className="relative h-1.5 rounded-full bg-zinc-800 flex overflow-hidden mb-2">
                          <div className="bg-rose-500/50" style={{ width: "50%" }} />
                          <div className="bg-emerald-500/50" style={{ width: "50%" }} />
                          {p.current_price != null && p.current_target > p.current_stop && (
                            <div
                              className="absolute top-[-2px] w-[3px] h-[9px] bg-white rounded-full"
                              style={{
                                left: `${Math.min(100, Math.max(0, ((p.current_price - p.current_stop) / (p.current_target - p.current_stop)) * 100))}%`,
                              }}
                            />
                          )}
                        </div>
                      )}
                      <div className="flex items-center justify-between">
                        <span className="font-mono text-[10px] text-zinc-600">{fmtDate(p.opened_at)} {fmtTime(p.opened_at)}</span>
                        {p.status === "PENDING_EXIT" ? (
                          <span className="font-mono text-[10px] text-amber-400">pending exit…</span>
                        ) : (
                          <button onClick={() => void doClosePosition(p)} disabled={actionBusy === `close:${p.id}`}
                            className="font-mono text-[10px] px-2 py-1 rounded bg-rose-500/10 border border-rose-500/20 text-rose-400 disabled:opacity-40">
                            {actionBusy === `close:${p.id}` ? "Closing…" : "Close position"}
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: ORDERS
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "orders" && (() => {
            const now = new Date();
            const todayOrders = orders.filter(o => isSameLocalDay(o.created_at, now));
            const visibleOrders = orderRange === "today" ? todayOrders : orders;
            return (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <SectionHdr>Recent orders — {mode}</SectionHdr>
                <button onClick={() => void loadPositionsAndOrders(mode)}
                  className="font-mono text-[10px] px-3 py-1 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-400">
                  ↻
                </button>
              </div>
              <div className="flex gap-1.5">
                <button onClick={() => setOrderRange("today")}
                  className={`font-mono text-[10px] px-3 py-1.5 rounded-lg border ${orderRange === "today"
                    ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-400"
                    : "bg-zinc-900 border-zinc-800 text-zinc-500"}`}>
                  Today ({todayOrders.length})
                </button>
                <button onClick={() => setOrderRange("all")}
                  className={`font-mono text-[10px] px-3 py-1.5 rounded-lg border ${orderRange === "all"
                    ? "bg-emerald-500/10 border-emerald-500/40 text-emerald-400"
                    : "bg-zinc-900 border-zinc-800 text-zinc-500"}`}>
                  All ({orders.length})
                </button>
              </div>
              {visibleOrders.length === 0 ? (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-6 text-center">
                  <p className="font-mono text-sm text-zinc-600">
                    {orderRange === "today" ? "No orders today." : "No orders yet."}
                  </p>
                </div>
              ) : (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
                  <table className="w-full font-mono text-[11px]">
                    <thead className="border-b border-zinc-800">
                      <tr className="text-zinc-600">
                        <th className="text-left p-3">Symbol</th>
                        <th className="text-left p-3">Side</th>
                        <th className="text-right p-3">Qty</th>
                        <th className="text-right p-3">Limit</th>
                        <th className="text-right p-3">Current</th>
                        <th className="text-center p-3">Status</th>
                        <th className="text-right p-3">{orderRange === "today" ? "Time" : "Date · Time"}</th>
                        <th className="p-3" />
                      </tr>
                    </thead>
                    <tbody>
                      {visibleOrders.map(o => {
                        const sc = o.status === "FILLED" ? "text-emerald-400"
                          : o.status === "CANCELLED" || o.status === "REJECTED" ? "text-rose-400"
                          : o.status === "PLACED" ? "text-amber-400"
                          : "text-zinc-500";
                        const waiting = o.status === "PENDING" || o.status === "PLACED";
                        return (
                          <tr key={o.id} className="border-b border-zinc-900 hover:bg-zinc-950">
                            <td className="p-3 font-bold text-zinc-100">
                              {o.symbol}
                              {o.execution_source === "MANUAL" && (
                                <span className="ml-1.5 font-mono text-[9px] text-sky-400 align-middle">MANUAL</span>
                              )}
                            </td>
                            <td className={`p-3 font-bold ${o.side === "BUY" ? "text-emerald-400" : "text-rose-400"}`}>{o.side}</td>
                            <td className="p-3 text-right text-zinc-400">{o.qty}</td>
                            <td className="p-3 text-right text-zinc-400">{o.limit_price ? `₹${o.limit_price}` : "MKT"}</td>
                            <td className="p-3 text-right">
                              {waiting && o.current_price != null ? (
                                <span className={o.limit_distance_pct != null && o.limit_distance_pct <= 0 ? "text-emerald-400" : "text-zinc-400"}>
                                  ₹{o.current_price.toFixed(2)}
                                  {o.limit_distance_pct != null && (
                                    <span className="text-[9px] text-zinc-600 ml-1">
                                      ({o.limit_distance_pct > 0 ? "+" : ""}{o.limit_distance_pct}%)
                                    </span>
                                  )}
                                </span>
                              ) : <span className="text-zinc-700">—</span>}
                            </td>
                            <td className={`p-3 text-center ${sc}`}>{o.status}</td>
                            <td className="p-3 text-right text-zinc-600">
                              {orderRange === "today" ? fmtTime(o.created_at) : `${fmtDate(o.created_at)} ${fmtTime(o.created_at)}`}
                            </td>
                            <td className="p-3 text-right">
                              {o.status === "PLACED" && (
                                <button onClick={() => void doCancelOrder(o)} disabled={actionBusy === `cancel:${o.id}`}
                                  className="text-rose-400 hover:text-rose-300 disabled:opacity-40">
                                  {actionBusy === `cancel:${o.id}` ? "…" : "✕"}
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
            );
          })()}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: WATCHLIST — every candidate the engine has recently
              fetched/evaluated, its latest WAIT/ENTER verdict, the limit
              price it's waiting on (if any), and a live price alongside it.
              This is the piece that used to be invisible between a cycle
              running and an order showing up in Orders/Positions.
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "watchlist" && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <SectionHdr>Candidate watchlist — {mode}</SectionHdr>
                <button onClick={() => void loadCandidates(mode)} disabled={candidatesLoading}
                  className="font-mono text-[10px] px-3 py-1 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-400 disabled:opacity-40">
                  {candidatesLoading ? "…" : "↻"}
                </button>
              </div>
              <p className="font-mono text-[10px] text-zinc-600 -mt-2">
                Stocks pulled from Hot Picks / IPO / market scan, evaluated by the risk engine each cycle.
              </p>

              {candidates.length === 0 ? (
                <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-6 text-center">
                  <p className="font-mono text-sm text-zinc-600">
                    {candidatesLoading ? "Loading…" : "No candidates fetched yet — run a cycle or wait for Auto-Pilot."}
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  {candidates.map(c => {
                    const d = c.latest_decision;
                    const actionColor = d?.action === "ENTER" ? "text-emerald-400"
                      : d?.action === "WAIT" ? "text-amber-400"
                      : d?.risk_verdict === "REJECTED" || d?.risk_verdict === "BLOCKED_GLOBAL" ? "text-rose-400"
                      : "text-zinc-500";
                    const expanded = expandedCandidateId === c.id;
                    return (
                      <div key={c.id} className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                        <button
                          onClick={() => setExpandedCandidateId(expanded ? null : c.id)}
                          className="flex items-start justify-between mb-2 w-full text-left"
                        >
                          <div>
                            <span className="font-mono text-base font-bold text-zinc-100">{c.symbol}</span>
                            {c.fetch_count > 1 && (
                              <span className="font-mono text-[9px] text-zinc-500 ml-2 px-1.5 py-0.5 rounded bg-zinc-800 border border-zinc-700" title={`Seen ${c.fetch_count} times in this window`}>
                                ×{c.fetch_count}
                              </span>
                            )}
                            {c.source_tab && (
                              <span className="font-mono text-[9px] text-zinc-600 ml-2 uppercase">{SOURCE_LABELS[c.source_tab] || c.source_tab}</span>
                            )}
                            {c.decision_label && (
                              <span className="font-mono text-[9px] text-sky-400 ml-2">{c.decision_label}</span>
                            )}
                          </div>
                          <span className="flex items-center gap-2">
                            <span className={`font-mono text-[11px] font-bold ${actionColor}`}>
                              {d ? d.action : "not yet evaluated"}
                            </span>
                            <span className="font-mono text-[9px] text-zinc-600">{expanded ? "▲" : "▼"}</span>
                          </span>
                        </button>

                        <div className="grid grid-cols-4 gap-2 font-mono text-[10px] text-zinc-500 mb-2">
                          <div>Signal<br/><span className="text-zinc-300 font-bold">{c.signal_price ? `₹${c.signal_price}` : "—"}</span></div>
                          <div>
                            {d?.action === "WAIT" ? "Waiting at" : "Entry limit"}
                            <br/>
                            <span className="text-amber-400 font-bold">{d?.proposed_price ? `₹${d.proposed_price}` : "—"}</span>
                          </div>
                          <div>
                            Current
                            <br/>
                            <span className="text-zinc-100 font-bold">
                              {c.current_price != null ? `₹${c.current_price.toFixed(2)}` : "—"}
                              {d?.limit_distance_pct != null && (
                                <span className={`ml-1 text-[9px] ${d.limit_distance_pct <= 0 ? "text-emerald-400" : "text-zinc-600"}`}>
                                  ({d.limit_distance_pct > 0 ? "+" : ""}{d.limit_distance_pct}%)
                                </span>
                              )}
                            </span>
                          </div>
                          <div>Stop loss<br/><span className="text-rose-400 font-bold">{d?.proposed_stop ? `₹${d.proposed_stop}` : "—"}</span></div>
                        </div>

                        {d?.reasoning && (
                          <p className="font-mono text-[10px] text-zinc-600 border-t border-zinc-800 pt-2 mt-1">
                            {d.risk_verdict && <span className={`font-bold mr-1 ${d.risk_verdict === "APPROVED" ? "text-emerald-500" : "text-rose-500"}`}>[{d.risk_verdict}]</span>}
                            {d.reasoning}
                          </p>
                        )}

                        {expanded && (
                          <div className="border-t border-zinc-800 pt-2 mt-2 space-y-2">
                            <div className="grid grid-cols-3 gap-2 font-mono text-[10px] text-zinc-500">
                              <div>Conviction<br/><span className="text-zinc-200 font-bold">{c.conviction_score != null ? c.conviction_score.toFixed(1) : "—"}</span></div>
                              <div>Proposed qty<br/><span className="text-zinc-200 font-bold">{d?.proposed_qty ?? "—"}</span></div>
                              <div>Target<br/><span className="text-emerald-400 font-bold">{d?.proposed_target ? `₹${d.proposed_target}` : "—"}</span></div>
                            </div>
                            {d?.risk_verdict_reason && d.risk_verdict_reason !== d.reasoning && (
                              <p className="font-mono text-[10px] text-zinc-500">
                                <span className="text-zinc-600">Risk engine verdict — </span>{d.risk_verdict_reason}
                              </p>
                            )}
                            <p className="font-mono text-[9px] text-zinc-700">Candidate #{c.id}{d ? " · has been evaluated" : " · awaiting first evaluation"}</p>
                          </div>
                        )}

                        <div className="flex items-center justify-between mt-2">
                          <span className="font-mono text-[9px] text-zinc-700">
                            Fetched {fmtDate(c.received_at)} {fmtTime(c.received_at)}
                            {c.consumed ? "" : " · not yet evaluated"}
                          </span>
                          {d && <span className="font-mono text-[9px] text-zinc-700">Evaluated {fmtTime(d.evaluated_at)}</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: PIPELINE (Run Cycle)
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "pipeline" && (
            <div className="space-y-4">
              <OrderFlowDiagram mode={mode} />

              <PipelineLiveStatus pipeline={pipeline} mode={mode} />

              <div className="bg-zinc-900 border border-zinc-800 rounded-xl p-4">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <SectionHdr>AI Trade pipeline — {mode}</SectionHdr>
                    <p className="font-mono text-[10px] text-zinc-600">Candidate → Risk → {mode === "REAL" ? "Dhan Order" : "Simulated Fill"} → Position → Exit</p>
                  </div>
                  {armed ? (
                    <button onClick={() => void doRunCycle()} disabled={cycleBusy}
                      className="px-4 py-2 rounded-lg bg-emerald-500/10 border border-emerald-500/30 font-mono text-xs text-emerald-300 disabled:opacity-40">
                      {cycleBusy ? "Running…" : "▶ Run Cycle"}
                    </button>
                  ) : (
                    <span className="font-mono text-[10px] text-zinc-600">Arm first to run cycles</span>
                  )}
                </div>

                {mode === "REAL" && (
                  <p className="font-mono text-[10px] text-amber-400/70 mb-3 bg-amber-500/5 rounded-lg px-3 py-2 border border-amber-500/20">
                    REAL mode: orders placed live at Dhan. Reconcile runs automatically at end of each cycle.
                  </p>
                )}

                {/* Auto-Pilot — runs this same cycle on a server-side timer
                    (market hours only) so it keeps working with this page
                    closed. Telegram must be configured on the backend
                    (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) to get notified. */}
                <div className={`flex items-center justify-between mb-3 px-3 py-2 rounded-lg border ${
                  status?.auto_pilot_enabled ? "bg-emerald-500/5 border-emerald-500/20" : "bg-zinc-950 border-zinc-800"
                }`}>
                  <div>
                    <p className="font-mono text-[11px] text-zinc-300">
                      🤖 Auto-Pilot {status?.auto_pilot_enabled ? <span className="text-emerald-400">ON</span> : <span className="text-zinc-600">OFF</span>}
                    </p>
                    <p className="font-mono text-[9px] text-zinc-600">
                      {status?.auto_pilot_enabled
                        ? "Running this cycle automatically during market hours — Telegram notifies you of every action."
                        : "Off — Run Cycle only fires when you click it here."}
                    </p>
                  </div>
                  <button
                    onClick={() => void doToggleAutoPilot()}
                    disabled={!armed || autoPilotBusy}
                    className={`px-4 py-2 rounded-lg font-mono text-xs disabled:opacity-40 ${
                      status?.auto_pilot_enabled
                        ? "bg-rose-500/10 border border-rose-500/30 text-rose-300"
                        : "bg-emerald-500/10 border border-emerald-500/30 text-emerald-300"
                    }`}
                  >
                    {autoPilotBusy ? "…" : status?.auto_pilot_enabled ? "Turn Off" : "Turn On"}
                  </button>
                </div>
                {!armed && (
                  <p className="font-mono text-[10px] text-zinc-600 mb-3">Arm {mode} first to enable Auto-Pilot.</p>
                )}

                {/* Scheduled Automation (2026-08-31) — three optional time-of-day
                    features layered on top of Auto-Pilot. This per-mode switch is
                    the SOLE on/off authority (the server-side env kill-switch was
                    removed 2026-09-01), plus the mode being armed, and fires at
                    most once per trading day. All default OFF — turn on in DEMO
                    first to prove the flow before enabling for REAL money. */}
                {status?.scheduled_automation && (
                  <div className="mb-3 rounded-lg border border-zinc-800 bg-zinc-950/60 overflow-hidden">
                    <div className="px-3 py-2 border-b border-zinc-800 bg-zinc-900/40">
                      <p className="font-mono text-[11px] text-zinc-300">⏰ Scheduled Automation</p>
                      <p className="font-mono text-[9px] text-zinc-600 mt-0.5">
                        Pre-pick the best stocks before the open, auto-enter at market open, and square off before close.
                        {" "}Fires once per trading day. Test in DEMO before enabling for REAL.
                      </p>
                    </div>
                    <div className="divide-y divide-zinc-800/70">
                      {([
                        { key: "prepick" as const, icon: "🌅", title: "Pre-pick (pre-open)",
                          desc: "Warm the candidate queue so the strongest names are ready the moment the market opens. No order is placed." },
                        { key: "enter_at_open" as const, icon: "🚀", title: "Enter at open",
                          desc: "Run one full entry cycle just after the open so pre-picked names get entered at the early price." },
                        { key: "eod_squareoff" as const, icon: "🌆", title: "EOD square-off",
                          desc: "Close every open position before the close so nothing is carried overnight (intraday square-off)." },
                      ]).map(f => {
                        const st = status.scheduled_automation![f.key];
                        const on = st.enabled;
                        const busy = featureBusy === f.key;
                        return (
                          <div key={f.key} className={`flex items-start justify-between gap-3 px-3 py-2.5 ${on ? "bg-emerald-500/[0.04]" : ""}`}>
                            <div className="min-w-0">
                              <p className="font-mono text-[11px] text-zinc-300">
                                {f.icon} {f.title}{" "}
                                <span className="text-zinc-600">· {st.time_ist} IST</span>{" "}
                                {on
                                  ? <span className="text-emerald-400">ON</span>
                                  : <span className="text-zinc-600">OFF</span>}
                              </p>
                              <p className="font-mono text-[9px] text-zinc-600 mt-0.5">{f.desc}</p>
                              {st.last_run && (
                                <p className="font-mono text-[9px] text-zinc-700 mt-0.5">Last ran: {st.last_run}</p>
                              )}
                            </div>
                            <button
                              onClick={() => void doToggleFeature(f.key, on)}
                              disabled={!armed || busy}
                              className={`shrink-0 px-3 py-1.5 rounded-lg font-mono text-[11px] disabled:opacity-40 ${
                                on
                                  ? "bg-rose-500/10 border border-rose-500/30 text-rose-300"
                                  : "bg-emerald-500/10 border border-emerald-500/30 text-emerald-300"
                              }`}
                            >
                              {busy ? "…" : on ? "Turn Off" : "Turn On"}
                            </button>
                          </div>
                        );
                      })}
                    </div>
                    {!armed && (
                      <p className="font-mono text-[9px] text-zinc-600 px-3 py-2 border-t border-zinc-800">
                        Arm {mode} first to enable scheduled automation.
                      </p>
                    )}
                  </div>
                )}

                {cycleBusy && (
                  <div className="h-1 w-full bg-zinc-800 rounded-full overflow-hidden mb-3">
                    <div className="h-full w-1/3 bg-emerald-500/60 animate-pulse rounded-full" />
                  </div>
                )}

                {(cycleResult || cycleBusy) && (
                  <div className="grid grid-cols-3 gap-2">
                    {[
                      { label: "Candidates", value: cycleResult?.new_candidates, color: "text-sky-400" },
                      { label: "Entered", value: cycleResult?.entry.entered, color: "text-emerald-400" },
                      { label: "Waited", value: cycleResult?.entry.waited, color: "text-amber-400" },
                      { label: "Rejected", value: cycleResult?.entry.rejected, color: "text-rose-400" },
                      { label: "Fills", value: cycleResult?.fills, color: "text-emerald-400" },
                      { label: "Expired", value: cycleResult?.expired_orders, color: "text-zinc-500" },
                      { label: "Held", value: cycleResult?.exit.held, color: "text-zinc-400" },
                      { label: "Trailed", value: cycleResult?.exit.trailed, color: "text-sky-400" },
                      { label: "Part. exit", value: cycleResult?.exit.partial_exits, color: "text-amber-400" },
                      { label: "Closed", value: cycleResult?.exit.full_exits, color: "text-emerald-400" },
                      { label: "Time-stop", value: cycleResult?.exit.time_stops, color: "text-rose-400" },
                    ].map(s => (
                      <StatCard key={s.label} label={s.label}
                        value={cycleBusy && s.value == null ? "…" : (s.value ?? 0)}
                        color={cycleBusy && s.value == null ? "text-zinc-700 animate-pulse" : s.color} />
                    ))}
                  </div>
                )}
              </div>

              {/* Manual trade ticket */}
              {(mode === "DEMO" || gate1) && (
                <ManualTradeTicket mode={mode} armed={armed}
                  onOrderComplete={() => { void loadPositionsAndOrders(mode); void loadStatus(mode); }} />
              )}
            </div>
          )}

          {/* ═══════════════════════════════════════════════════════════════
              TAB: ACTIVITY LOG
          ═══════════════════════════════════════════════════════════════ */}
          {activeTab === "log" && (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <SectionHdr>Recent activity — {mode}</SectionHdr>
                <button onClick={() => void loadAudit()}
                  className="font-mono text-[10px] px-3 py-1 rounded-lg bg-zinc-800 border border-zinc-700 text-zinc-400">
                  ↻ Refresh
                </button>
              </div>
              <div className="bg-zinc-900 border border-zinc-800 rounded-xl overflow-hidden">
                {auditRows.length === 0 ? (
                  <p className="font-mono text-[11px] text-zinc-600 p-4">No activity yet — click Refresh.</p>
                ) : (
                  <div className="divide-y divide-zinc-900">
                    {auditRows.map((r, i) => {
                      const isPositive = ["ARMED", "ADMIN_LOGIN", "DHAN_CONNECTED", "MANUAL_CLOSE", "RISK_CONFIG_CONFIRMED"].includes(r.action);
                      const isNegative = ["DISARMED", "ADMIN_LOGOUT", "MANUAL_CANCEL"].includes(r.action);
                      return (
                        <div key={i} className="flex items-start justify-between gap-3 px-4 py-2 hover:bg-zinc-950">
                          <div className="flex items-start gap-2 min-w-0">
                            <span className={`flex-shrink-0 mt-0.5 ${isPositive ? "text-emerald-500" : isNegative ? "text-rose-500" : "text-zinc-600"}`}>●</span>
                            <div className="min-w-0">
                              <span className="font-mono text-[11px] text-zinc-200 font-bold">{r.action}</span>
                              {r.detail && <span className="font-mono text-[10px] text-zinc-500 ml-2 truncate">{r.detail}</span>}
                            </div>
                          </div>
                          <span className="font-mono text-[10px] text-zinc-700 whitespace-nowrap flex-shrink-0">{fmtTime(r.occurred_at)}</span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            </div>
          )}
        </>
      )}
    </div>
  );
}
