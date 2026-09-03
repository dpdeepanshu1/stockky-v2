import { useCallback, useEffect, useRef, useState } from "react";
import {
  realTradeApi, getRealTradeApiUrl, setRealTradeApiUrl,
  getSessionToken, setSessionToken, setSessionExpiredHandler,
  type GateStatus, type AuditLogRow, type Position, type OrderRow, type CycleResult, type DhanStatus,
  type PipelineStatus, type CandidateRow, type WatchlistEntry, type ResilienceStatus,
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
  if (n == null) return "text-mist";
  return n >= 0 ? "text-signal-buy" : "text-signal-sell";
}

// ── Status dot ──────────────────────────────────────────────────────────────
function Dot({ on, pulse }: { on: boolean; pulse?: boolean }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full ${on ? "bg-signal-buy" : "bg-signal-sell"} ${pulse && on ? "animate-pulse" : ""}`} />
  );
}

// ── Tiny stat card ──────────────────────────────────────────────────────────
function StatCard({ label, value, sub, color }: { label: string; value: React.ReactNode; sub?: string; color?: string }) {
  return (
    <div className="bg-graphite border border-slate rounded-2xl p-3 flex flex-col gap-0.5">
      <p className="text-[10px] font-display tabular-nums uppercase tracking-widest text-mist">{label}</p>
      <p className={`text-lg font-bold font-display tabular-nums ${color ?? "text-paper"}`}>{value}</p>
      {sub && <p className="text-[10px] font-display tabular-nums text-mist">{sub}</p>}
    </div>
  );
}

// ── Section header ───────────────────────────────────────────────────────────
function SectionHdr({ children }: { children: React.ReactNode }) {
  return <p className="text-[10px] font-display tabular-nums uppercase tracking-widest text-mist mb-2">{children}</p>;
}

// ── Live pipeline status (2026-08-27) — what the cycle is doing RIGHT NOW,
// whether triggered by the Run Cycle button or by Auto-Pilot in the
// background, plus recent-cycle history with per-stage timing. Purely a
// display of pipeline_status.py's in-memory snapshot — never triggers
// anything itself. ───────────────────────────────────────────────────────────
const STAGE_LABELS: Record<string, string> = {
  starting: "Starting…",
  dynamic_universe: "Widening/pruning watchlist universe",
  watchlist: "Detecting catalysts (watchlist)",
  candidates: "Fetching candidates",
  entry: "Evaluating entries (chase-guard check)",
  fills: "Checking fills",
  expire: "Expiring stale orders",
  exit: "Evaluating exits (horizon-aware)",
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
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <SectionHdr>Live cycle status — {mode}</SectionHdr>
        <p className="font-display tabular-nums text-[11px] text-mist">Loading…</p>
      </div>
    );
  }

  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4 space-y-3">
      <div className="flex items-center justify-between">
        <SectionHdr>Live cycle status — {mode}</SectionHdr>
        {pipeline.running ? (
          <span className="font-display tabular-nums text-[10px] text-signal-buy flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-signal-buy animate-pulse" />
            {pipeline.trigger === "autopilot" ? "Auto-Pilot cycle running" : "Cycle running"}
          </span>
        ) : (
          <span className="font-display tabular-nums text-[10px] text-mist">Idle — no cycle running</span>
        )}
      </div>

      {pipeline.running && (
        <div className="bg-ink border border-slate rounded-xl p-3 space-y-2">
          <div className="flex items-center justify-between">
            <p className="font-display tabular-nums text-xs text-paper">
              {STAGE_LABELS[pipeline.stage || ""] || pipeline.stage}
            </p>
            <p className="font-display tabular-nums text-[10px] text-mist">
              stage {msFmt(pipeline.stage_elapsed_ms)} · total {msFmt(pipeline.total_elapsed_ms)}
            </p>
          </div>
          {/* stage progress dots */}
          <div className="flex items-center gap-1">
            {(pipeline.stages || []).map(s => (
              <div key={s} className={`h-1.5 flex-1 rounded-full ${
                s === pipeline.stage ? "bg-signal-buy animate-pulse"
                : (pipeline.stage_timings_ms && s in pipeline.stage_timings_ms) ? "bg-signal-buy"
                : "bg-ink"
              }`} title={STAGE_LABELS[s] || s} />
            ))}
          </div>
          {pipeline.current_source && (
            <p className="font-display tabular-nums text-[10px] text-signal-prepare">
              Source: {SOURCE_LABELS[pipeline.current_source] || pipeline.current_source}
            </p>
          )}
          {pipeline.current_symbol && (
            <p className="font-display tabular-nums text-[10px] text-signal-hold">
              Symbol: {pipeline.current_symbol}
              {!!pipeline.symbols_total && (
                <span className="text-mist"> ({(pipeline.symbols_done ?? 0) + 1}/{pipeline.symbols_total})</span>
              )}
            </p>
          )}
          {pipeline.warning && (
            <p className="font-display tabular-nums text-[10px] text-signal-hold">⚠ {pipeline.warning}</p>
          )}
        </div>
      )}

      {pipeline.last_cycle && (
        <div>
          <p className="font-display tabular-nums text-[10px] text-mist mb-1">
            Last cycle — {pipeline.last_cycle.trigger === "autopilot" ? "🤖 Auto-Pilot" : "▶ Manual"} ·{" "}
            {fmtTime(pipeline.last_cycle.ended_at)} · took {msFmt(pipeline.last_cycle.duration_ms)}
          </p>
          {pipeline.last_cycle.warning && (
            <p className="font-display tabular-nums text-[10px] text-signal-hold mb-1">⚠ {pipeline.last_cycle.warning}</p>
          )}
          {pipeline.last_cycle.error ? (
            <p className="font-display tabular-nums text-[10px] text-signal-sell">Error: {pipeline.last_cycle.error}</p>
          ) : pipeline.last_cycle.auto_disarmed ? (
            <p className="font-display tabular-nums text-[10px] text-signal-sell">Auto-disarmed: {pipeline.last_cycle.auto_disarmed}</p>
          ) : (
            <>
              <p className="font-display tabular-nums text-[10px] text-mist">
                {pipeline.last_cycle.new_candidates ?? 0} candidates · {pipeline.last_cycle.entered ?? 0} entered ·{" "}
                {pipeline.last_cycle.waited ?? 0} waited · {pipeline.last_cycle.rejected ?? 0} rejected ·{" "}
                {pipeline.last_cycle.full_exits ?? 0} closed
              </p>
              {!!pipeline.last_cycle.entry_details?.length && (
                <div className="mt-2 space-y-1 border-t border-slate pt-2">
                  {pipeline.last_cycle.entry_details.map((row, i) => {
                    const color = row.action === "ENTER" ? "text-signal-buy"
                      : row.risk_verdict === "REJECTED" || row.risk_verdict === "BLOCKED_GLOBAL" ? "text-signal-sell"
                      : "text-signal-hold";
                    return (
                      <p key={i} className="font-display tabular-nums text-[10px] text-mist">
                        <span className={`font-bold ${color}`}>{row.action}</span>{" "}
                        <span className="text-paper">{row.symbol}</span>
                        {row.reasoning && <span className="text-mist"> — {row.reasoning}</span>}
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
          <summary className="font-display tabular-nums text-[10px] text-mist cursor-pointer select-none">
            Recent cycles ({pipeline.history.length}) — includes Auto-Pilot ticks even when this tab was closed
          </summary>
          <div className="mt-2 overflow-x-auto">
            <table className="w-full font-display tabular-nums text-[10px]">
              <thead>
                <tr className="text-mist text-left">
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
                  <tr key={i} className="border-t border-slate text-mist">
                    <td className="py-1 pr-3">{fmtTime(c.ended_at)}</td>
                    <td className="py-1 pr-3">{c.trigger === "autopilot" ? "🤖 auto" : "▶ manual"}</td>
                    <td className="py-1 pr-3">{msFmt(c.duration_ms)}</td>
                    <td className="py-1 pr-3">{c.new_candidates ?? "—"}</td>
                    <td className="py-1 pr-3 text-signal-buy">{c.entered ?? "—"}</td>
                    <td className="py-1 pr-3 text-signal-sell">{c.rejected ?? "—"}</td>
                    <td className="py-1 pr-3">{c.full_exits ?? "—"}</td>
                    <td className="py-1 text-signal-sell">{c.error || c.auto_disarmed || ""}</td>
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

// ── Catalyst watchlist + signal-sourcing health (2026-09-03) ─────────────────
// Separate concept from the "Watchlist" tab (which shows trade_candidates,
// post-risk-engine). This shows the EARLIER stage: what was detected as a
// catalyst before any price-band check ran, incl. rows the chase-guard
// rejected ("missed") — plus whether either upstream circuit breaker is
// currently degraded, and what the last dynamic-universe sync actually did.
// Fetches its own data on an interval, independent of the rest of the tab's
// state, so it works whether or not Pipeline is the active tab's first load.
const SOURCE_TIER_LABELS: Record<number, string> = {
  1: "Tier 1 · full pipeline",
  2: "Tier 2 · raw event feed",
  3: "Tier 3 · volume shock",
};
const WATCHLIST_STATUS_STYLE: Record<string, string> = {
  active: "text-signal-prepare border-signal-prepare/30 bg-signal-prepare/5",
  entered: "text-signal-buy border-signal-buy/30 bg-signal-buy/5",
  missed: "text-signal-sell border-signal-sell/30 bg-signal-sell/5",
  expired: "text-mist border-slate bg-ink",
};

function BreakerDot({ state }: { state: "closed" | "open" | "half_open" }) {
  const color = state === "closed" ? "bg-signal-buy" : state === "half_open" ? "bg-signal-hold" : "bg-signal-sell";
  return <span className={`inline-block w-2 h-2 rounded-full ${color}`} />;
}

function CatalystWatchlistPanel({ mode }: { mode: Mode }) {
  const [entries, setEntries] = useState<WatchlistEntry[] | null>(null);
  const [resilience, setResilience] = useState<ResilienceStatus | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [wl, res] = await Promise.all([
        realTradeApi.watchlistEntries(mode, statusFilter || undefined),
        realTradeApi.resilienceStatus(),
      ]);
      setEntries(wl.entries);
      setResilience(res);
    } catch {
      // Best-effort observability panel — a failure here shouldn't disrupt
      // the rest of the Pipeline tab. Leaves last-known data on screen.
    } finally {
      setLoading(false);
    }
  }, [mode, statusFilter]);

  useEffect(() => {
    void load();
    const t = setInterval(() => void load(), 30_000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="space-y-4">
      {/* Signal sourcing health */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <SectionHdr>Signal sourcing health</SectionHdr>
        <div className="grid grid-cols-2 gap-3 mt-2">
          {resilience && Object.entries(resilience.breakers).map(([key, b]) => (
            <div key={key} className="bg-ink border border-slate rounded-xl p-3">
              <div className="flex items-center gap-2 mb-1">
                <BreakerDot state={b.state} />
                <span className="font-display text-xs font-bold text-paper capitalize">{key.replace("_", " ")}</span>
              </div>
              <p className="font-display tabular-nums text-[10px] text-mist capitalize">{b.state}</p>
              {b.state !== "closed" && (
                <p className="font-display tabular-nums text-[9px] text-signal-sell mt-1">
                  {b.consecutive_failures}/{b.failure_threshold} failures
                  {b.seconds_until_retry != null && ` · retry in ${Math.round(b.seconds_until_retry)}s`}
                </p>
              )}
            </div>
          ))}
        </div>
        <div className="mt-3 pt-3 border-t border-slate">
          <p className="font-display tabular-nums text-[10px] text-mist uppercase tracking-widest mb-1">Last dynamic-universe sync</p>
          {resilience?.dynamic_universe_last ? (
            <p className="font-display tabular-nums text-[11px] text-paper">
              +{resilience.dynamic_universe_last.added.length} added · −{resilience.dynamic_universe_last.removed.length} removed ·{" "}
              {resilience.dynamic_universe_last.kept} kept ·{" "}
              <span className="text-mist">{fmtTime(resilience.dynamic_universe_last.synced_at)}</span>
            </p>
          ) : (
            <p className="font-display tabular-nums text-[11px] text-mist">No sync recorded yet this run (runs every ~20 min, market hours only)</p>
          )}
        </div>
      </div>

      {/* Catalyst watchlist */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-1">
          <SectionHdr>Catalyst watchlist — {mode}</SectionHdr>
          <button onClick={() => void load()} disabled={loading}
            className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist disabled:opacity-40">
            {loading ? "…" : "↻"}
          </button>
        </div>
        <p className="font-display tabular-nums text-[10px] text-mist -mt-1 mb-2">
          Catalyst detected — before any price-band or risk check. Different from the "Watchlist" tab (that's post-evaluation candidates).
        </p>
        <div className="flex gap-1 mb-3">
          {["", "active", "entered", "missed", "expired"].map(s => (
            <button key={s || "all"} onClick={() => setStatusFilter(s)}
              className={`px-2.5 py-1 rounded-full font-display text-[10px] font-semibold transition-colors ${
                statusFilter === s ? "bg-signal-buy text-white" : "bg-ink border border-slate text-mist hover:text-paper"
              }`}>
              {s || "All"}
            </button>
          ))}
        </div>

        {!entries || entries.length === 0 ? (
          <p className="font-display tabular-nums text-[11px] text-mist py-4 text-center">
            {entries === null ? "Loading…" : "No catalyst watchlist entries" + (statusFilter ? ` with status "${statusFilter}"` : "") + " right now."}
          </p>
        ) : (
          <div className="space-y-2">
            {entries.map(e => (
              <div key={e.id} className="bg-ink border border-slate rounded-xl p-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="font-display text-sm font-bold text-paper">{e.symbol}</span>
                    <span className="font-display tabular-nums text-[9px] text-mist">{e.catalyst_type}</span>
                  </div>
                  <span className={`font-display tabular-nums text-[9px] px-2 py-0.5 rounded-full border ${WATCHLIST_STATUS_STYLE[e.status] || "text-mist border-slate"}`}>
                    {e.status}
                  </span>
                </div>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 mt-1.5 font-display tabular-nums text-[10px] text-mist">
                  <span>{SOURCE_TIER_LABELS[e.source_tier] || `Tier ${e.source_tier}`}</span>
                  <span>Horizon: {e.horizon_class}</span>
                  <span>Band: {(e.entry_band_pct * 100).toFixed(1)}%</span>
                  {e.conviction_score != null && <span>Conviction: {e.conviction_score}</span>}
                  {e.catalyst_ts && <span>Detected {fmtTime(e.catalyst_ts)}</span>}
                </div>
                {e.missed_reason && (
                  <p className="font-display tabular-nums text-[10px] text-signal-sell mt-1.5">⚠ {e.missed_reason}</p>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ── Order flow diagram (how a trade executes) ────────────────────────────────
function OrderFlowDiagram({ mode }: { mode: Mode }) {
  const steps = [
    { id: "universe", label: "Dynamic Universe", desc: "Widen/prune auto-tracked symbols (~20min, mkt hrs)" },
    { id: "watchlist", label: "Watchlist", desc: "Catalyst detected — no price gate yet" },
    { id: "candidate", label: "Candidate", desc: "Hot Picks / IPO / Scan / Watchlist trigger" },
    { id: "risk", label: "Risk Check", desc: "9 engine checks + entry-band (chase guard)" },
    { id: "order", label: "Place Order", desc: mode === "REAL" ? "Dhan API (LIMIT)" : "Paper trade" },
    { id: "fill", label: "Fill", desc: mode === "REAL" ? "Reconcile w/ Dhan" : "Simulated price" },
    { id: "position", label: "Position Open", desc: "Stop + Target set" },
    { id: "exit", label: "Exit", desc: "Horizon-aware: Stop / Target / Trail / Time" },
  ];
  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4 mb-4">
      <SectionHdr>How a trade executes — {mode} mode</SectionHdr>
      <div className="flex items-center gap-0 overflow-x-auto pb-1">
        {steps.map((s, i) => (
          <div key={s.id} className="flex items-center flex-shrink-0">
            <div className="flex flex-col items-center gap-1 min-w-[80px]">
              <div className={`w-7 h-7 rounded-full flex items-center justify-center text-[10px] font-bold border ${
                mode === "REAL"
                  ? "bg-signal-hold/10 border-signal-hold/40 text-signal-hold"
                  : "bg-signal-buy/10 border-signal-buy/40 text-signal-buy"
              }`}>{i + 1}</div>
              <p className="text-[10px] font-display tabular-nums font-bold text-paper text-center leading-tight">{s.label}</p>
              <p className="text-[9px] font-display tabular-nums text-mist text-center leading-tight">{s.desc}</p>
            </div>
            {i < steps.length - 1 && (
              <div className="w-6 flex-shrink-0 flex items-center justify-center mb-6">
                <div className="h-px w-full bg-slate" />
                <span className="text-mist text-[9px] -ml-1">›</span>
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
    <div className="bg-graphite border border-slate rounded-2xl p-4 mb-4">
      <SectionHdr>Balance allocation</SectionHdr>
      <div className="space-y-3">
        {/* Bar */}
        <div className="h-3 rounded-full bg-ink flex overflow-hidden">
          <div className="bg-signal-buy/70 transition-all" style={{ width: `${pct(available)}%` }} />
          <div className="bg-signal-hold/70 transition-all" style={{ width: `${pct(utilized)}%` }} />
          {collateral > 0 && <div className="bg-signal-prepare/70 transition-all" style={{ width: `${pct(collateral)}%` }} />}
        </div>
        <div className="grid grid-cols-3 gap-2 text-[10px] font-display tabular-nums">
          <div><span className="inline-block w-2 h-2 rounded-sm bg-signal-buy/70 mr-1" />Available<br/><span className="text-paper font-bold">{fmtInr(available, 0)}</span></div>
          <div><span className="inline-block w-2 h-2 rounded-sm bg-signal-hold/70 mr-1" />Utilized<br/><span className="text-paper font-bold">{fmtInr(utilized, 0)}</span></div>
          {collateral > 0 && <div><span className="inline-block w-2 h-2 rounded-sm bg-signal-prepare/70 mr-1" />Collateral<br/><span className="text-paper font-bold">{fmtInr(collateral, 0)}</span></div>}
        </div>
        {positions.length > 0 && (
          <div className="border-t border-slate pt-2">
            <p className="text-[9px] font-display tabular-nums text-mist mb-1">OPEN BROKER POSITIONS</p>
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
                    <div className="w-24 font-display tabular-nums text-[10px] text-paper truncate">{sym}</div>
                    <div className="flex-1 h-1.5 bg-ink rounded-full overflow-hidden">
                      <div className="h-full bg-signal-hold/60 rounded-full" style={{ width: `${valPct}%` }} />
                    </div>
                    <div className="w-16 text-right font-display tabular-nums text-[10px] text-mist">{fmtInr(val, 0)}</div>
                    <div className={`w-14 text-right font-display tabular-nums text-[10px] ${pnlColor(pnl)}`}>{pnl >= 0 ? "+" : ""}{fmtInr(pnl, 0)}</div>
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
    <div className={`flex items-center gap-2 px-3 py-2 rounded-xl border transition-colors ${
      done ? "bg-signal-buy/5 border-signal-buy/20" :
      active ? "bg-signal-hold/10 border-signal-hold/30 animate-pulse" :
      "bg-graphite border-slate"
    }`}>
      <div className={`w-5 h-5 rounded-full flex items-center justify-center text-[10px] font-bold flex-shrink-0 ${
        done ? "bg-signal-buy/20 text-signal-buy" :
        active ? "bg-signal-hold/20 text-signal-hold" :
        "bg-ink text-mist"
      }`}>{done ? "✓" : n}</div>
      <span className={`text-[11px] font-display tabular-nums ${done ? "text-signal-buy" : active ? "text-signal-hold" : "text-mist"}`}>{label}</span>
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
  const [regenLoading, setRegenLoading] = useState(false);

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

  // Server-side auto-regenerate — only succeeds when DHAN_TOTP_ENABLED=true.
  // Own loading flag so it doesn't freeze the other buttons on this panel;
  // errors (including the expected 409 in manual-paste mode) surface via
  // the existing shared error banner.
  const doRegenerateToken = async () => {
    setRegenLoading(true); setError(null);
    try {
      await realTradeApi.dhanRegenerateToken();
      await loadStatus(mode);
    } catch (e: any) { setError(e?.message || "Failed to regenerate Dhan token"); }
    finally { setRegenLoading(false); }
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
        <p className="font-display tabular-nums text-xs text-mist mb-1 uppercase tracking-widest">Real Trade Service URL</p>
        <p className="font-display tabular-nums text-[11px] text-mist mb-3">Paste your real-trade-service URL (separate Render deploy from api-gateway).</p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-graphite border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
            placeholder="https://stockky-real-trade.onrender.com"
            value={apiUrlInput}
            onChange={e => setApiUrlInput(e.target.value)}
          />
          <button onClick={saveApiUrl} className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy">Save</button>
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
          <span className="font-display tabular-nums text-sm font-bold text-paper">Real Auto Trade</span>
          {armed && <span className="text-[10px] font-display tabular-nums px-2 py-0.5 rounded-full bg-signal-buy/15 border border-signal-buy/30 text-signal-buy animate-pulse">ARMED</span>}
        </div>
        <div className="flex gap-1.5">
          {(["DEMO", "REAL"] as Mode[]).map(m => (
            <button key={m} onClick={() => setMode(m)} className={`px-3 py-1 rounded-xl font-display tabular-nums text-[11px] border transition-colors ${
              mode === m
                ? m === "DEMO" ? "bg-signal-buy/15 border-signal-buy/30 text-signal-buy" : "bg-signal-sell/15 border-signal-sell/30 text-signal-sell"
                : "bg-graphite border-slate text-mist"
            }`}>
              <Dot on={mode === m && armed} pulse /> {m}
            </button>
          ))}
        </div>
      </div>

      {/* Phase notice */}
      <p className="font-display tabular-nums text-[10px] text-signal-hold/70 mb-3 px-1">
        Phase 2 active — DEMO fully wired (candidates→risk→fill→exit). REAL places live Dhan orders & reconciles fills.
      </p>

      {/* Error banner */}
      {error && (
        <div className="mb-3 px-3 py-2 rounded-xl bg-signal-sell/40 border border-signal-sell/30 font-display tabular-nums text-xs text-signal-sell flex items-start justify-between gap-2">
          <span>{error}</span>
          <button onClick={() => setError(null)} className="text-signal-sell hover:text-signal-sell flex-shrink-0">✕</button>
        </div>
      )}

      {/* ── Auth / session bar (REAL only) ──────────────────────────────── */}
      {mode === "REAL" && !loggedIn && !status?.armed ? (
        <div className="bg-graphite border border-slate rounded-2xl p-4 mb-4">
          <SectionHdr>Admin login required for REAL mode</SectionHdr>
          <div className="space-y-2">
            <input className="w-full bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
              placeholder="Admin username" value={username} onChange={e => setUsername(e.target.value)} />
            <input type="password" className="w-full bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
              placeholder="Password" value={password} onChange={e => setPassword(e.target.value)}
              onKeyDown={e => e.key === "Enter" && void doLogin()} />
            <button onClick={() => void doLogin()} disabled={loading || !password}
              className="w-full py-2 rounded-xl bg-signal-prepare/15 border border-signal-prepare/30 font-display tabular-nums text-xs text-signal-prepare disabled:opacity-40">
              {loading ? "Authenticating…" : "Authenticate"}
            </button>
          </div>
        </div>
      ) : (
        <>
          {mode === "REAL" && (
            loggedIn ? (
              <div className="flex items-center justify-between mb-3 px-3 py-1.5 rounded-xl bg-signal-buy/5 border border-signal-buy/20">
                <span className="font-display tabular-nums text-[11px] text-signal-buy flex items-center gap-1.5">
                  <Dot on={true} /> Admin session active ({username})
                </span>
                <button onClick={() => void doLogout()} className="font-display tabular-nums text-[10px] px-2 py-1 rounded bg-signal-sell/10 border border-signal-sell/20 text-signal-sell">
                  Log out
                </button>
              </div>
            ) : (
              <div className="flex items-center justify-between mb-3 px-3 py-1.5 rounded-xl bg-signal-hold/5 border border-signal-hold/20">
                <span className="font-display tabular-nums text-[11px] text-signal-hold">
                  Session expired — auto-pilot still running. Log in to make changes.
                </span>
                <button onClick={() => setActiveTab("overview")} className="font-display tabular-nums text-[10px] px-2 py-1 rounded bg-signal-prepare/10 border border-signal-prepare/20 text-signal-prepare">
                  Log in
                </button>
              </div>
            )
          )}

          {/* ── Tab bar (Groww-style pill segmented control) ───────────── */}
          <div className="flex gap-1 mb-4 overflow-x-auto pb-1 bg-ink border border-slate rounded-full p-1">
            {tabs.map(t => (
              <button key={t.id} onClick={() => setActiveTab(t.id)}
                className={`px-3.5 py-1.5 rounded-full font-display text-[12px] font-semibold whitespace-nowrap transition-colors flex-shrink-0 ${
                  activeTab === t.id
                    ? "bg-signal-buy text-white"
                    : "text-mist hover:text-paper"
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
              <div className="bg-graphite border border-slate rounded-2xl p-4">
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
                  <div className="mb-3 p-3 rounded-xl border border-signal-hold/20 bg-signal-hold/5 space-y-2">
                    <p className="font-display tabular-nums text-[10px] text-signal-hold">Session expired — log in to disarm or change config. Auto-pilot keeps running.</p>
                    <div className="flex gap-2">
                      <input className="flex-1 bg-ink border border-slate rounded px-2 py-1 font-display tabular-nums text-xs text-paper focus:outline-none"
                        placeholder="Username" value={username} onChange={e => setUsername(e.target.value)} />
                      <input type="password" className="flex-1 bg-ink border border-slate rounded px-2 py-1 font-display tabular-nums text-xs text-paper focus:outline-none"
                        placeholder="Password" value={password} onChange={e => setPassword(e.target.value)}
                        onKeyDown={e => e.key === "Enter" && void doLogin()} />
                      <button onClick={() => void doLogin()} disabled={loading || !password}
                        className="px-3 py-1 rounded bg-signal-prepare/15 border border-signal-prepare/30 font-display tabular-nums text-xs text-signal-prepare disabled:opacity-40">
                        {loading ? "…" : "Login"}
                      </button>
                    </div>
                  </div>
                )}

                {/* Login form already shown above when not logged in; here only if
                    logged in but gate not met */}
                {status && !gate3 && mode === "REAL" && gate1 && gate2 && (
                  <button onClick={() => void doConfirmRisk()}
                    className="w-full py-2 rounded-xl bg-signal-hold/10 border border-signal-hold/30 font-display tabular-nums text-xs text-signal-hold">
                    Confirm risk configuration
                  </button>
                )}
                {status && !gate3 && mode === "DEMO" && (
                  <button onClick={() => void doConfirmRisk()}
                    className="w-full py-2 rounded-xl bg-signal-hold/10 border border-signal-hold/30 font-display tabular-nums text-xs text-signal-hold">
                    Confirm risk configuration
                  </button>
                )}

                <div className="flex gap-2 mt-2">
                  {armed ? (
                    <button onClick={() => void doDisarm()} className="flex-1 py-2.5 rounded-xl bg-signal-sell/10 border border-signal-sell/30 font-display tabular-nums text-xs text-signal-sell">
                      🛑 DISARM
                    </button>
                  ) : (
                    <button onClick={() => void doArm()} disabled={loading || !gate1 || !gate2 || !gate3}
                      className="flex-1 py-2.5 rounded-xl bg-signal-buy/10 border border-signal-buy/30 font-display tabular-nums text-xs text-signal-buy disabled:opacity-30">
                      {loading ? "Arming…" : "⚡ ARM"}
                    </button>
                  )}
                  <button onClick={() => void doEmergencyPause()} className="px-4 py-2.5 rounded-xl bg-signal-sell/30 border border-signal-sell/40 font-display tabular-nums text-xs text-signal-sell">
                    🚨 PAUSE ALL
                  </button>
                </div>
                {status?.disarmed_reason && (
                  /outbound IP|invalid ip|not whitelisted/i.test(status.disarmed_reason) ? (
                    <div className="mt-2 rounded-xl border border-signal-hold/30 bg-signal-hold/5 px-3 py-2">
                      <p className="font-display tabular-nums text-[10px] text-signal-hold">
                        🚨 Auto-paused: Dhan rejected an order because this service's outbound IP isn't whitelisted.
                      </p>
                      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
                        Reads (funds/positions) still work — only order placement is IP-gated by Dhan, which is
                        why the account shows connected while this happens.
                      </p>
                      <div className="flex items-center gap-2 mt-2">
                        <button onClick={() => void doNetworkCheck()} disabled={networkCheckBusy}
                          className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-signal-hold/10 border border-signal-hold/30 text-signal-hold disabled:opacity-40">
                          {networkCheckBusy ? "Checking…" : "Check outbound IP"}
                        </button>
                        {networkCheck?.outbound_ip && (
                          <code className="font-display tabular-nums text-[10px] text-paper bg-ink px-2 py-1 rounded">{networkCheck.outbound_ip}</code>
                        )}
                      </div>
                      {networkCheck && (
                        <p className="font-display tabular-nums text-[9px] text-mist mt-1">{networkCheck.note}</p>
                      )}
                      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
                        Add that IP under Dhan Web → My Profile → API Access → IP Whitelisting, then re-arm.
                        A non-static host IP can change on redeploy — re-check if this recurs.
                      </p>
                    </div>
                  ) : (
                    <p className="font-display tabular-nums text-[10px] text-signal-hold/60 mt-2">Last disarm: {status.disarmed_reason}</p>
                  )
                )}
              </div>

              {/* REAL: Dhan account card */}
              {mode === "REAL" && (
                <div className="bg-graphite border border-slate rounded-2xl p-4">
                  <div className="flex items-center justify-between mb-3">
                    <SectionHdr>Dhan account</SectionHdr>
                    <span className={`font-display tabular-nums text-[10px] px-2 py-0.5 rounded-full border ${
                      dhanAccount?.connected ? "bg-signal-buy/10 border-signal-buy/30 text-signal-buy" : "bg-signal-sell/10 border-signal-sell/30 text-signal-sell"
                    }`}>
                      {dhanAccount?.connected ? "🟢 Connected" : "🔴 Disconnected"}
                    </span>
                  </div>

                  {dhanAccount?.connected ? (
                    <div className="space-y-3">
                      <div className="grid grid-cols-2 gap-2 text-[11px] font-display tabular-nums text-mist">
                        <div>Client ID <span className="text-paper ml-1">{dhanAccount.client_id_masked}</span></div>
                        <div>Token <span className={`ml-1 ${(secondsRemaining ?? 0) < 7200 ? "text-signal-sell" : "text-signal-buy"}`}>
                          {secondsRemaining != null ? (secondsRemaining <= 0 ? "expired" : fmtHms(secondsRemaining) + " left") : "—"}
                        </span></div>
                      </div>
                      <p className="font-display tabular-nums text-[9px] text-mist -mt-2">
                        {dhanAccount.token_issued_at && `Issued ${fmtDate(dhanAccount.token_issued_at)} ${fmtTime(dhanAccount.token_issued_at)} · `}
                        Countdown reflects Dhan's own expiry when a regenerated token reports one, otherwise assumes {dhanAccount.token_hard_cap_hours ?? 24}h as a safety ceiling.
                      </p>

                      {dhanAccount.funds_error ? (
                        <p className="font-display tabular-nums text-[11px] text-signal-sell bg-signal-sell/5 rounded-xl px-3 py-2 border border-signal-sell/20">
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
                              <div key={f.label} className="bg-ink border border-slate rounded-xl p-2">
                                <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">{f.label}</p>
                                <p className="font-display tabular-nums text-sm font-bold text-paper mt-0.5">{fmtInr(v, 0)}</p>
                              </div>
                            );
                          })}
                        </div>
                      ) : null}

                      <div className="flex gap-2">
                        <button onClick={() => void loadStatus(mode)} className="font-display tabular-nums text-[10px] text-signal-prepare hover:text-signal-prepare">
                          ↻ Refresh
                        </button>
                        <button onClick={() => setShowDhanForm(!showDhanForm)} className="font-display tabular-nums text-[10px] text-mist hover:text-paper">
                          Rotate token
                        </button>
                        <button onClick={() => void doRegenerateToken()} disabled={regenLoading} className="font-display tabular-nums text-[10px] text-signal-prepare hover:text-signal-prepare disabled:opacity-40">
                          {regenLoading ? "Regenerating…" : "⟳ Regenerate token"}
                        </button>
                      </div>
                    </div>
                  ) : (
                    <p className="font-display tabular-nums text-[11px] text-mist">Connect Dhan below to enable REAL trading and see live balance.</p>
                  )}

                  {/* Dhan connect form */}
                  {(!dhanAccount?.connected || showDhanForm) && (
                    <div className="mt-3 space-y-2 border-t border-slate pt-3">
                      <p className="font-display tabular-nums text-[10px] text-mist">{dhanAccount?.connected ? "Paste a fresh access token to rotate" : "Connect your Dhan account"}</p>
                      <input className="w-full bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
                        placeholder="Dhan Client ID" value={dhanClientId} onChange={e => setDhanClientId(e.target.value)} />
                      <input type="password" className="w-full bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none focus:border-slate"
                        placeholder="Access Token (generate at web.dhan.co → DhanHQ APIs)" value={dhanToken} onChange={e => setDhanToken(e.target.value)} />
                      <div className="flex gap-2">
                        <button onClick={() => void doConnectDhan()} disabled={loading || !dhanClientId || !dhanToken}
                          className="flex-1 py-2 rounded-xl bg-signal-prepare/10 border border-signal-prepare/30 font-display tabular-nums text-xs text-signal-prepare disabled:opacity-40">
                          {loading ? "Connecting…" : dhanAccount?.connected ? "Save new token" : "Connect Dhan"}
                        </button>
                        {dhanAccount?.connected && (
                          <button onClick={() => setShowDhanForm(false)}
                            className="px-3 py-2 rounded-xl border border-slate font-display tabular-nums text-xs text-mist">
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
                <div className="bg-graphite border border-slate rounded-2xl p-4">
                  <div className="flex items-center justify-between mb-1">
                    <SectionHdr>Risk configuration — {mode}</SectionHdr>
                    {armed && <span className="font-display tabular-nums text-[9px] text-signal-hold uppercase">locked while armed</span>}
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
                        <div key={key} className="bg-ink border border-slate rounded-xl p-2">
                          <p className="font-display tabular-nums text-[9px] uppercase tracking-widest text-mist">{label}</p>
                          <input
                            type="number" inputMode="decimal" disabled={armed}
                            value={riskForm[key]}
                            onChange={e => setRiskForm(f => f && { ...f, [key]: e.target.value })}
                            className="w-full bg-transparent font-display tabular-nums text-sm font-bold text-paper mt-0.5 focus:outline-none disabled:opacity-60"
                          />
                        </div>
                      ))}
                      <label className="col-span-2 flex items-center gap-2 bg-ink border border-slate rounded-xl p-2 cursor-pointer">
                        <input type="checkbox" disabled={armed} checked={riskForm.allow_pyramiding}
                          onChange={e => setRiskForm(f => f && { ...f, allow_pyramiding: e.target.checked })}
                          className="accent-signal-prepare" />
                        <span className="font-display tabular-nums text-[10px] text-mist">Allow pyramiding (add to an existing open position)</span>
                      </label>
                    </div>
                  )}
                  {riskMsg && (
                    <p className={`font-display tabular-nums text-[10px] mt-2 ${riskMsg.ok ? "text-signal-buy" : "text-signal-sell"}`}>{riskMsg.text}</p>
                  )}
                  {status.risk_config.updated_at && (
                    <p className="font-display tabular-nums text-[9px] text-mist mt-2">
                      Last saved {fmtDate(status.risk_config.updated_at)} {fmtTime(status.risk_config.updated_at)}
                      {status.risk_config.updated_by ? ` by ${status.risk_config.updated_by}` : ""}
                    </p>
                  )}
                  {!armed && (
                    <button onClick={() => void doSaveRiskConfig()} disabled={riskSaving}
                      className="mt-3 w-full py-2 rounded-xl bg-signal-prepare/10 border border-signal-prepare/30 font-display tabular-nums text-xs text-signal-prepare disabled:opacity-40">
                      {riskSaving ? "Saving…" : "Save changes"}
                    </button>
                  )}
                  {!gate3 && (
                    <button onClick={() => void doConfirmRisk()} className="mt-2 w-full py-2 rounded-xl bg-signal-hold/10 border border-signal-hold/30 font-display tabular-nums text-xs text-signal-hold">
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
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">Switch to REAL mode and log in to see live Dhan account data.</p>
                </div>
              ) : !loggedIn ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">Log in to view live Dhan data.</p>
                </div>
              ) : (
                <>
                  <div className="flex items-center justify-between">
                    <SectionHdr>Live data from Dhan broker</SectionHdr>
                    <button onClick={() => void loadLiveDhanData()} disabled={liveLoading}
                      className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist disabled:opacity-40">
                      {liveLoading ? "Loading…" : "↻ Refresh all"}
                    </button>
                  </div>

                  {liveError && (
                    <div className="px-3 py-2 rounded-xl bg-signal-sell/40 border border-signal-sell/30 font-display tabular-nums text-xs text-signal-sell">{liveError}</div>
                  )}

                  {/* Balance allocation */}
                  {dhanAccount?.funds && (
                    <BalanceAllocation funds={dhanAccount.funds} positions={livePositions} />
                  )}

                  {/* Live positions */}
                  <div className="bg-graphite border border-slate rounded-2xl p-4">
                    <SectionHdr>Open positions at Dhan ({livePositions.length})</SectionHdr>
                    {livePositions.length === 0 ? (
                      <p className="font-display tabular-nums text-[11px] text-mist">No open positions at broker.</p>
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
                            <div key={i} className="bg-ink rounded-xl px-3 py-2 border border-slate">
                              <div className="flex items-center justify-between">
                                <div>
                                  <span className="font-display tabular-nums text-sm font-bold text-paper">{sym}</span>
                                  <span className="font-display tabular-nums text-[10px] text-mist ml-2">{product}</span>
                                </div>
                                <span className={`font-display tabular-nums text-sm font-bold ${pnlColor(pnl)}`}>
                                  {pnl >= 0 ? "+" : ""}{fmtInr(pnl, 2)}
                                </span>
                              </div>
                              <div className="flex gap-4 mt-1 font-display tabular-nums text-[10px] text-mist">
                                <span>Qty <span className="text-paper">{qty}</span></span>
                                <span>Avg <span className="text-paper">₹{avg.toFixed(2)}</span></span>
                                <span>LTP <span className="text-paper">₹{ltp.toFixed(2)}</span></span>
                                <span>Val <span className="text-paper">{fmtInr(qty * avg, 0)}</span></span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {/* Live holdings */}
                  <div className="bg-graphite border border-slate rounded-2xl p-4">
                    <SectionHdr>Demat holdings ({liveHoldings.length})</SectionHdr>
                    {liveHoldings.length === 0 ? (
                      <p className="font-display tabular-nums text-[11px] text-mist">No holdings in demat.</p>
                    ) : (
                      <div className="overflow-x-auto">
                        <table className="w-full font-display tabular-nums text-[11px]">
                          <thead>
                            <tr className="text-mist border-b border-slate">
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
                                <tr key={i} className="border-b border-slate hover:bg-ink">
                                  <td className="py-1.5 pr-3 text-paper font-bold">{sym}</td>
                                  <td className="text-right pr-3 text-mist">{qty}</td>
                                  <td className="text-right pr-3 text-mist">₹{avg.toFixed(2)}</td>
                                  <td className="text-right pr-3 text-mist">₹{ltp.toFixed(2)}</td>
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
                  <div className="bg-graphite border border-slate rounded-2xl p-4">
                    <SectionHdr>Today's orders at Dhan ({liveDhanOrders.length})</SectionHdr>
                    {liveDhanOrders.length === 0 ? (
                      <p className="font-display tabular-nums text-[11px] text-mist">No orders placed today.</p>
                    ) : (
                      <div className="space-y-1.5 max-h-60 overflow-y-auto">
                        {liveDhanOrders.map((o, i) => {
                          const sym = o.tradingSymbol || o.symbol || "—";
                          const side = o.transactionType || o.side || "";
                          const qty = Number(o.quantity || 0);
                          const price = Number(o.price || o.averageTradedPrice || 0);
                          const status = (o.orderStatus || o.status || "").toUpperCase();
                          const statusColor = status === "TRADED" || status === "FILLED" ? "text-signal-buy"
                            : status === "REJECTED" || status === "CANCELLED" ? "text-signal-sell"
                            : "text-signal-hold";
                          return (
                            <div key={i} className="flex items-center justify-between bg-ink rounded-xl px-3 py-1.5 border border-slate">
                              <div className="flex items-center gap-2 font-display tabular-nums text-[11px]">
                                <span className={`font-bold ${side === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>{side}</span>
                                <span className="text-paper">{sym}</span>
                                <span className="text-mist">×{qty}</span>
                                {price > 0 && <span className="text-mist">@ ₹{price.toFixed(2)}</span>}
                              </div>
                              <span className={`font-display tabular-nums text-[10px] ${statusColor}`}>{status}</span>
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
                      className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-signal-prepare/10 border border-signal-prepare/30 text-signal-prepare disabled:opacity-40">
                      {actionBusy === "reconcile" ? "Checking…" : "🔄 Reconcile"}
                    </button>
                  )}
                  <button onClick={() => void loadPositionsAndOrders(mode)}
                    className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                    ↻
                  </button>
                </div>
              </div>

              {actionMsg && (
                <div className={`px-3 py-2 rounded-xl border font-display tabular-nums text-xs ${actionMsg.ok ? "bg-signal-buy/5 border-signal-buy/20 text-signal-buy" : "bg-signal-sell/5 border-signal-sell/20 text-signal-sell"}`}>
                  {actionMsg.text}
                </div>
              )}

              {positions.length === 0 ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">No open positions.</p>
                </div>
              ) : (
                <div className="space-y-2">
                  {positions.map(p => (
                    <div key={p.id} className="bg-graphite border border-slate rounded-2xl p-4">
                      <div className="flex items-start justify-between mb-2">
                        <div className="flex items-center gap-2.5">
                          <div className="w-8 h-8 shrink-0 rounded-full bg-ink border border-slate flex items-center justify-center font-display text-[11px] font-bold text-mist">
                            {p.symbol.slice(0, 2)}
                          </div>
                          <div>
                            <span className="font-display text-base font-bold text-paper">{p.symbol}</span>
                            <span className="font-display tabular-nums text-[10px] text-mist ml-2">{p.status}</span>
                          </div>
                        </div>
                        <div className="text-right">
                          <span className={`font-display tabular-nums text-base font-bold ${pnlColor(p.unrealized_pnl)}`}>
                            {p.unrealized_pnl >= 0 ? "+" : ""}{fmtInr(p.unrealized_pnl, 2)}
                          </span>
                          {p.pnl_pct != null && (
                            <span className={`font-display tabular-nums text-[10px] ml-1 ${pnlColor(p.pnl_pct)}`}>
                              ({p.pnl_pct >= 0 ? "+" : ""}{p.pnl_pct}%)
                            </span>
                          )}
                        </div>
                      </div>
                      <div className="grid grid-cols-5 gap-2 font-display tabular-nums text-[10px] text-mist mb-3">
                        <div>Qty<br/><span className="text-paper font-bold">{p.qty_open}</span></div>
                        <div>Entry<br/><span className="text-paper font-bold">₹{p.avg_entry_price}</span></div>
                        <div>Current<br/><span className="text-signal-prepare font-bold">{p.current_price != null ? `₹${p.current_price.toFixed(2)}` : "—"}</span></div>
                        <div>
                          Stop
                          {p.stop_distance_pct != null && <span className="text-mist"> ({p.stop_distance_pct}%)</span>}
                          <br/><span className="text-signal-sell font-bold">{p.current_stop ? `₹${p.current_stop}` : "—"}</span>
                        </div>
                        <div>
                          Target
                          {p.target_distance_pct != null && <span className="text-mist"> ({p.target_distance_pct}%)</span>}
                          <br/><span className="text-signal-buy font-bold">{p.current_target ? `₹${p.current_target}` : "—"}</span>
                        </div>
                      </div>
                      {/* Risk:reward bar with a live marker for where current price sits between stop and target */}
                      {p.current_stop && p.current_target && (
                        <div className="relative h-1.5 rounded-full bg-ink flex overflow-hidden mb-2">
                          <div className="bg-signal-sell/50" style={{ width: "50%" }} />
                          <div className="bg-signal-buy/50" style={{ width: "50%" }} />
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
                        <span className="font-display tabular-nums text-[10px] text-mist">{fmtDate(p.opened_at)} {fmtTime(p.opened_at)}</span>
                        {p.status === "PENDING_EXIT" ? (
                          <span className="font-display tabular-nums text-[10px] text-signal-hold">pending exit…</span>
                        ) : (
                          <button onClick={() => void doClosePosition(p)} disabled={actionBusy === `close:${p.id}`}
                            className="font-display tabular-nums text-[10px] px-2 py-1 rounded bg-signal-sell/10 border border-signal-sell/20 text-signal-sell disabled:opacity-40">
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
                  className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                  ↻
                </button>
              </div>
              <div className="flex gap-1.5">
                <button onClick={() => setOrderRange("today")}
                  className={`font-display tabular-nums text-[10px] px-3 py-1.5 rounded-xl border ${orderRange === "today"
                    ? "bg-signal-buy/10 border-signal-buy/40 text-signal-buy"
                    : "bg-graphite border-slate text-mist"}`}>
                  Today ({todayOrders.length})
                </button>
                <button onClick={() => setOrderRange("all")}
                  className={`font-display tabular-nums text-[10px] px-3 py-1.5 rounded-xl border ${orderRange === "all"
                    ? "bg-signal-buy/10 border-signal-buy/40 text-signal-buy"
                    : "bg-graphite border-slate text-mist"}`}>
                  All ({orders.length})
                </button>
              </div>
              {visibleOrders.length === 0 ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">
                    {orderRange === "today" ? "No orders today." : "No orders yet."}
                  </p>
                </div>
              ) : (
                <div className="bg-graphite border border-slate rounded-2xl overflow-hidden">
                  <table className="w-full font-display tabular-nums text-[11px]">
                    <thead className="border-b border-slate">
                      <tr className="text-mist">
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
                        const sc = o.status === "FILLED" ? "text-signal-buy"
                          : o.status === "CANCELLED" || o.status === "REJECTED" ? "text-signal-sell"
                          : o.status === "PLACED" ? "text-signal-hold"
                          : "text-mist";
                        const waiting = o.status === "PENDING" || o.status === "PLACED";
                        return (
                          <tr key={o.id} className="border-b border-slate hover:bg-ink">
                            <td className="p-3 font-bold text-paper">
                              {o.symbol}
                              {o.execution_source === "MANUAL" && (
                                <span className="ml-1.5 font-display tabular-nums text-[9px] text-signal-prepare align-middle">MANUAL</span>
                              )}
                            </td>
                            <td className={`p-3 font-bold ${o.side === "BUY" ? "text-signal-buy" : "text-signal-sell"}`}>{o.side}</td>
                            <td className="p-3 text-right text-mist">{o.qty}</td>
                            <td className="p-3 text-right text-mist">{o.limit_price ? `₹${o.limit_price}` : "MKT"}</td>
                            <td className="p-3 text-right">
                              {waiting && o.current_price != null ? (
                                <span className={o.limit_distance_pct != null && o.limit_distance_pct <= 0 ? "text-signal-buy" : "text-mist"}>
                                  ₹{o.current_price.toFixed(2)}
                                  {o.limit_distance_pct != null && (
                                    <span className="text-[9px] text-mist ml-1">
                                      ({o.limit_distance_pct > 0 ? "+" : ""}{o.limit_distance_pct}%)
                                    </span>
                                  )}
                                </span>
                              ) : <span className="text-mist">—</span>}
                            </td>
                            <td className={`p-3 text-center ${sc}`}>{o.status}</td>
                            <td className="p-3 text-right text-mist">
                              {orderRange === "today" ? fmtTime(o.created_at) : `${fmtDate(o.created_at)} ${fmtTime(o.created_at)}`}
                            </td>
                            <td className="p-3 text-right">
                              {o.status === "PLACED" && (
                                <button onClick={() => void doCancelOrder(o)} disabled={actionBusy === `cancel:${o.id}`}
                                  className="text-signal-sell hover:text-signal-sell disabled:opacity-40">
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
                  className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist disabled:opacity-40">
                  {candidatesLoading ? "…" : "↻"}
                </button>
              </div>
              <p className="font-display tabular-nums text-[10px] text-mist -mt-2">
                Stocks pulled from Hot Picks / IPO / market scan, evaluated by the risk engine each cycle.
              </p>

              {candidates.length === 0 ? (
                <div className="bg-graphite border border-slate rounded-2xl p-6 text-center">
                  <p className="font-display tabular-nums text-sm text-mist">
                    {candidatesLoading ? "Loading…" : "No candidates fetched yet — run a cycle or wait for Auto-Pilot."}
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  {candidates.map(c => {
                    const d = c.latest_decision;
                    const actionColor = d?.action === "ENTER" ? "text-signal-buy"
                      : d?.action === "WAIT" ? "text-signal-hold"
                      : d?.risk_verdict === "REJECTED" || d?.risk_verdict === "BLOCKED_GLOBAL" ? "text-signal-sell"
                      : "text-mist";
                    const expanded = expandedCandidateId === c.id;
                    return (
                      <div key={c.id} className="bg-graphite border border-slate rounded-2xl p-4">
                        <button
                          onClick={() => setExpandedCandidateId(expanded ? null : c.id)}
                          className="flex items-start justify-between mb-2 w-full text-left"
                        >
                          <div>
                            <span className="font-display tabular-nums text-base font-bold text-paper">{c.symbol}</span>
                            {c.fetch_count > 1 && (
                              <span className="font-display tabular-nums text-[9px] text-mist ml-2 px-1.5 py-0.5 rounded bg-ink border border-slate" title={`Seen ${c.fetch_count} times in this window`}>
                                ×{c.fetch_count}
                              </span>
                            )}
                            {c.source_tab && (
                              <span className="font-display tabular-nums text-[9px] text-mist ml-2 uppercase">{SOURCE_LABELS[c.source_tab] || c.source_tab}</span>
                            )}
                            {c.decision_label && (
                              <span className="font-display tabular-nums text-[9px] text-signal-prepare ml-2">{c.decision_label}</span>
                            )}
                          </div>
                          <span className="flex items-center gap-2">
                            <span className={`font-display tabular-nums text-[11px] font-bold ${actionColor}`}>
                              {d ? d.action : "not yet evaluated"}
                            </span>
                            <span className="font-display tabular-nums text-[9px] text-mist">{expanded ? "▲" : "▼"}</span>
                          </span>
                        </button>

                        <div className="grid grid-cols-4 gap-2 font-display tabular-nums text-[10px] text-mist mb-2">
                          <div>Signal<br/><span className="text-paper font-bold">{c.signal_price ? `₹${c.signal_price}` : "—"}</span></div>
                          <div>
                            {d?.action === "WAIT" ? "Waiting at" : "Entry limit"}
                            <br/>
                            <span className="text-signal-hold font-bold">{d?.proposed_price ? `₹${d.proposed_price}` : "—"}</span>
                          </div>
                          <div>
                            Current
                            <br/>
                            <span className="text-paper font-bold">
                              {c.current_price != null ? `₹${c.current_price.toFixed(2)}` : "—"}
                              {d?.limit_distance_pct != null && (
                                <span className={`ml-1 text-[9px] ${d.limit_distance_pct <= 0 ? "text-signal-buy" : "text-mist"}`}>
                                  ({d.limit_distance_pct > 0 ? "+" : ""}{d.limit_distance_pct}%)
                                </span>
                              )}
                            </span>
                          </div>
                          <div>Stop loss<br/><span className="text-signal-sell font-bold">{d?.proposed_stop ? `₹${d.proposed_stop}` : "—"}</span></div>
                        </div>

                        {d?.reasoning && (
                          <p className="font-display tabular-nums text-[10px] text-mist border-t border-slate pt-2 mt-1">
                            {d.risk_verdict && <span className={`font-bold mr-1 ${d.risk_verdict === "APPROVED" ? "text-signal-buy" : "text-signal-sell"}`}>[{d.risk_verdict}]</span>}
                            {d.reasoning}
                          </p>
                        )}

                        {expanded && (
                          <div className="border-t border-slate pt-2 mt-2 space-y-2">
                            <div className="grid grid-cols-3 gap-2 font-display tabular-nums text-[10px] text-mist">
                              <div>Conviction<br/><span className="text-paper font-bold">{c.conviction_score != null ? c.conviction_score.toFixed(1) : "—"}</span></div>
                              <div>Proposed qty<br/><span className="text-paper font-bold">{d?.proposed_qty ?? "—"}</span></div>
                              <div>Target<br/><span className="text-signal-buy font-bold">{d?.proposed_target ? `₹${d.proposed_target}` : "—"}</span></div>
                            </div>
                            {d?.risk_verdict_reason && d.risk_verdict_reason !== d.reasoning && (
                              <p className="font-display tabular-nums text-[10px] text-mist">
                                <span className="text-mist">Risk engine verdict — </span>{d.risk_verdict_reason}
                              </p>
                            )}
                            <p className="font-display tabular-nums text-[9px] text-mist">Candidate #{c.id}{d ? " · has been evaluated" : " · awaiting first evaluation"}</p>
                          </div>
                        )}

                        <div className="flex items-center justify-between mt-2">
                          <span className="font-display tabular-nums text-[9px] text-mist">
                            Fetched {fmtDate(c.received_at)} {fmtTime(c.received_at)}
                            {c.consumed ? "" : " · not yet evaluated"}
                          </span>
                          {d && <span className="font-display tabular-nums text-[9px] text-mist">Evaluated {fmtTime(d.evaluated_at)}</span>}
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

              <CatalystWatchlistPanel mode={mode} />

              <div className="bg-graphite border border-slate rounded-2xl p-4">
                <div className="flex items-center justify-between mb-3">
                  <div>
                    <SectionHdr>AI Trade pipeline — {mode}</SectionHdr>
                    <p className="font-display tabular-nums text-[10px] text-mist">Candidate → Risk → {mode === "REAL" ? "Dhan Order" : "Simulated Fill"} → Position → Exit</p>
                  </div>
                  {armed ? (
                    <button onClick={() => void doRunCycle()} disabled={cycleBusy}
                      className="px-4 py-2 rounded-xl bg-signal-buy/10 border border-signal-buy/30 font-display tabular-nums text-xs text-signal-buy disabled:opacity-40">
                      {cycleBusy ? "Running…" : "▶ Run Cycle"}
                    </button>
                  ) : (
                    <span className="font-display tabular-nums text-[10px] text-mist">Arm first to run cycles</span>
                  )}
                </div>

                {mode === "REAL" && (
                  <p className="font-display tabular-nums text-[10px] text-signal-hold/70 mb-3 bg-signal-hold/5 rounded-xl px-3 py-2 border border-signal-hold/20">
                    REAL mode: orders placed live at Dhan. Reconcile runs automatically at end of each cycle.
                  </p>
                )}

                {/* Auto-Pilot — runs this same cycle on a server-side timer
                    (market hours only) so it keeps working with this page
                    closed. Telegram must be configured on the backend
                    (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID) to get notified. */}
                <div className={`flex items-center justify-between mb-3 px-3 py-2 rounded-xl border ${
                  status?.auto_pilot_enabled ? "bg-signal-buy/5 border-signal-buy/20" : "bg-ink border-slate"
                }`}>
                  <div>
                    <p className="font-display tabular-nums text-[11px] text-paper">
                      🤖 Auto-Pilot {status?.auto_pilot_enabled ? <span className="text-signal-buy">ON</span> : <span className="text-mist">OFF</span>}
                    </p>
                    <p className="font-display tabular-nums text-[9px] text-mist">
                      {status?.auto_pilot_enabled
                        ? "Running this cycle automatically during market hours — Telegram notifies you of every action."
                        : "Off — Run Cycle only fires when you click it here."}
                    </p>
                  </div>
                  <button
                    onClick={() => void doToggleAutoPilot()}
                    disabled={!armed || autoPilotBusy}
                    className={`px-4 py-2 rounded-xl font-display tabular-nums text-xs disabled:opacity-40 ${
                      status?.auto_pilot_enabled
                        ? "bg-signal-sell/10 border border-signal-sell/30 text-signal-sell"
                        : "bg-signal-buy/10 border border-signal-buy/30 text-signal-buy"
                    }`}
                  >
                    {autoPilotBusy ? "…" : status?.auto_pilot_enabled ? "Turn Off" : "Turn On"}
                  </button>
                </div>
                {!armed && (
                  <p className="font-display tabular-nums text-[10px] text-mist mb-3">Arm {mode} first to enable Auto-Pilot.</p>
                )}

                {/* Scheduled Automation (2026-08-31) — three optional time-of-day
                    features layered on top of Auto-Pilot. This per-mode switch is
                    the SOLE on/off authority (the server-side env kill-switch was
                    removed 2026-09-01), plus the mode being armed, and fires at
                    most once per trading day. All default OFF — turn on in DEMO
                    first to prove the flow before enabling for REAL money. */}
                {status?.scheduled_automation && (
                  <div className="mb-3 rounded-xl border border-slate bg-ink/60 overflow-hidden">
                    <div className="px-3 py-2 border-b border-slate bg-graphite/40">
                      <p className="font-display tabular-nums text-[11px] text-paper">⏰ Scheduled Automation</p>
                      <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">
                        Pre-pick the best stocks before the open, auto-enter at market open, and square off before close.
                        {" "}Fires once per trading day. Test in DEMO before enabling for REAL.
                      </p>
                    </div>
                    <div className="divide-y divide-slate">
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
                          <div key={f.key} className={`flex items-start justify-between gap-3 px-3 py-2.5 ${on ? "bg-signal-buy/[0.04]" : ""}`}>
                            <div className="min-w-0">
                              <p className="font-display tabular-nums text-[11px] text-paper">
                                {f.icon} {f.title}{" "}
                                <span className="text-mist">· {st.time_ist} IST</span>{" "}
                                {on
                                  ? <span className="text-signal-buy">ON</span>
                                  : <span className="text-mist">OFF</span>}
                              </p>
                              <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">{f.desc}</p>
                              {st.last_run && (
                                <p className="font-display tabular-nums text-[9px] text-mist mt-0.5">Last ran: {st.last_run}</p>
                              )}
                            </div>
                            <button
                              onClick={() => void doToggleFeature(f.key, on)}
                              disabled={!armed || busy}
                              className={`shrink-0 px-3 py-1.5 rounded-xl font-display tabular-nums text-[11px] disabled:opacity-40 ${
                                on
                                  ? "bg-signal-sell/10 border border-signal-sell/30 text-signal-sell"
                                  : "bg-signal-buy/10 border border-signal-buy/30 text-signal-buy"
                              }`}
                            >
                              {busy ? "…" : on ? "Turn Off" : "Turn On"}
                            </button>
                          </div>
                        );
                      })}
                    </div>
                    {!armed && (
                      <p className="font-display tabular-nums text-[9px] text-mist px-3 py-2 border-t border-slate">
                        Arm {mode} first to enable scheduled automation.
                      </p>
                    )}
                  </div>
                )}

                {cycleBusy && (
                  <div className="h-1 w-full bg-ink rounded-full overflow-hidden mb-3">
                    <div className="h-full w-1/3 bg-signal-buy/60 animate-pulse rounded-full" />
                  </div>
                )}

                {(cycleResult || cycleBusy) && (
                  <div className="grid grid-cols-3 gap-2">
                    {[
                      { label: "Candidates", value: cycleResult?.new_candidates, color: "text-signal-prepare" },
                      { label: "Entered", value: cycleResult?.entry.entered, color: "text-signal-buy" },
                      { label: "Waited", value: cycleResult?.entry.waited, color: "text-signal-hold" },
                      { label: "Rejected", value: cycleResult?.entry.rejected, color: "text-signal-sell" },
                      { label: "Fills", value: cycleResult?.fills, color: "text-signal-buy" },
                      { label: "Expired", value: cycleResult?.expired_orders, color: "text-mist" },
                      { label: "Held", value: cycleResult?.exit.held, color: "text-mist" },
                      { label: "Trailed", value: cycleResult?.exit.trailed, color: "text-signal-prepare" },
                      { label: "Part. exit", value: cycleResult?.exit.partial_exits, color: "text-signal-hold" },
                      { label: "Closed", value: cycleResult?.exit.full_exits, color: "text-signal-buy" },
                      { label: "Time-stop", value: cycleResult?.exit.time_stops, color: "text-signal-sell" },
                    ].map(s => (
                      <StatCard key={s.label} label={s.label}
                        value={cycleBusy && s.value == null ? "…" : (s.value ?? 0)}
                        color={cycleBusy && s.value == null ? "text-mist animate-pulse" : s.color} />
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
                  className="font-display tabular-nums text-[10px] px-3 py-1 rounded-xl bg-ink border border-slate text-mist">
                  ↻ Refresh
                </button>
              </div>
              <div className="bg-graphite border border-slate rounded-2xl overflow-hidden">
                {auditRows.length === 0 ? (
                  <p className="font-display tabular-nums text-[11px] text-mist p-4">No activity yet — click Refresh.</p>
                ) : (
                  <div className="divide-y divide-slate">
                    {auditRows.map((r, i) => {
                      const isPositive = ["ARMED", "ADMIN_LOGIN", "DHAN_CONNECTED", "MANUAL_CLOSE", "RISK_CONFIG_CONFIRMED"].includes(r.action);
                      const isNegative = ["DISARMED", "ADMIN_LOGOUT", "MANUAL_CANCEL"].includes(r.action);
                      return (
                        <div key={i} className="flex items-start justify-between gap-3 px-4 py-2 hover:bg-ink">
                          <div className="flex items-start gap-2 min-w-0">
                            <span className={`flex-shrink-0 mt-0.5 ${isPositive ? "text-signal-buy" : isNegative ? "text-signal-sell" : "text-mist"}`}>●</span>
                            <div className="min-w-0">
                              <span className="font-display tabular-nums text-[11px] text-paper font-bold">{r.action}</span>
                              {r.detail && <span className="font-display tabular-nums text-[10px] text-mist ml-2 truncate">{r.detail}</span>}
                            </div>
                          </div>
                          <span className="font-display tabular-nums text-[10px] text-mist whitespace-nowrap flex-shrink-0">{fmtTime(r.occurred_at)}</span>
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
