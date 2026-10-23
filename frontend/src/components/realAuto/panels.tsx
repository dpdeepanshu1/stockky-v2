// E31: pipeline status, catalyst watchlist and order-flow panels of the Real Trade page (moved out of RealAutoTrade.tsx, code unchanged).
import { type Mode } from "./types";
import { type PipelineStatus, type ResilienceStatus, type WatchlistEntry, getSessionToken, realTradeApi } from "../../realTradeApi";
import { SectionHdr } from "./parts";
import { fmtDateTimeIst, fmtTime, msFmt } from "./format";
import { setVisibleInterval } from "../../visibleInterval";
import { useCallback, useEffect, useState } from "react";

// ── Live pipeline status (2026-08-27) — what the cycle is doing RIGHT NOW,
// whether triggered by the Run Cycle button or by Auto-Pilot in the
// background, plus recent-cycle history with per-stage timing. Purely a
// display of pipeline_status.py's in-memory snapshot — never triggers
// anything itself. ───────────────────────────────────────────────────────────
export const STAGE_LABELS: Record<string, string> = {
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

export const SOURCE_LABELS: Record<string, string> = {
  hot_picks: "Hot Picks",
  ipo: "IPO watchlist",
};


export function PipelineLiveStatus({ pipeline, mode }: { pipeline: PipelineStatus | null; mode: Mode }) {
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
export const SOURCE_TIER_LABELS: Record<number, string> = {
  1: "Tier 1 · full pipeline",
  2: "Tier 2 · raw event feed",
  3: "Tier 3 · volume shock",
};
export const WATCHLIST_STATUS_STYLE: Record<string, string> = {
  active: "text-signal-prepare border-signal-prepare/30 bg-signal-prepare/5",
  entered: "text-signal-buy border-signal-buy/30 bg-signal-buy/5",
  missed: "text-signal-sell border-signal-sell/30 bg-signal-sell/5",
  expired: "text-mist border-slate bg-ink",
};

export function BreakerDot({ state }: { state: "closed" | "open" | "half_open" }) {
  const color = state === "closed" ? "bg-signal-buy" : state === "half_open" ? "bg-signal-hold" : "bg-signal-sell";
  return <span className={`inline-block w-2 h-2 rounded-full ${color}`} />;
}

export function CatalystWatchlistPanel({ mode, loggedIn }: { mode: Mode; loggedIn: boolean }) {
  const [entries, setEntries] = useState<WatchlistEntry[] | null>(null);
  const [resilience, setResilience] = useState<ResilienceStatus | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    // Item 29: /resilience/status is admin-only on the server and REAL
    // watchlist entries need the admin session too. This panel polls every
    // 30 s, so with no session (logged out, or DEMO mode where login is not
    // needed) every poll was a guaranteed 401, and because both calls shared
    // one Promise.all a 401 on the admin-only one also blanked the DEMO
    // watchlist. Only ask for what the current session may read, and let each
    // result land on its own.
    const authed = !!getSessionToken();
    const wantWatchlist = mode !== "REAL" || authed;
    if (!wantWatchlist && !authed) return;
    setLoading(true);
    try {
      const [wl, res] = await Promise.allSettled([
        wantWatchlist ? realTradeApi.watchlistEntries(mode, statusFilter || undefined) : Promise.resolve(null),
        authed ? realTradeApi.resilienceStatus() : Promise.resolve(null),
      ]);
      // Best-effort observability panel — a failure here shouldn't disrupt
      // the rest of the Pipeline tab. A failed or skipped call leaves its
      // last-known data on screen.
      if (wl.status === "fulfilled" && wl.value) setEntries(wl.value.entries);
      if (res.status === "fulfilled" && res.value) setResilience(res.value);
    } finally {
      setLoading(false);
    }
  }, [mode, statusFilter, loggedIn]);

  useEffect(() => {
    void load();
    const stop = setVisibleInterval(() => void load(), 30_000);   // group 184: paused while the browser tab is hidden
    return () => stop();
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
                  {e.catalyst_ts && <span>Detected {fmtDateTimeIst(e.catalyst_ts)}</span>}
                  {e.expires_at && <span>Expires {fmtDateTimeIst(e.expires_at)}</span>}
                </div>
                {(() => {
                  // 2026-09-11 — surface staleness explicitly rather than
                  // silently showing an old catalyst as if it were fresh.
                  // A catalyst/news/result signal loses most of its trading
                  // relevance after the day it fired; flag anything older
                  // than "yesterday" so it's obviously not a same-day pick.
                  if (!e.catalyst_ts) return null;
                  const ageMs = Date.now() - new Date(e.catalyst_ts).getTime();
                  const ageDays = ageMs / (1000 * 60 * 60 * 24);
                  if (ageDays <= 1.5) return null;
                  return (
                    <p className="font-display tabular-nums text-[9px] text-signal-hold mt-1">
                      ⏱ Stale — detected {Math.floor(ageDays)}d ago, not today/yesterday
                    </p>
                  );
                })()}
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
export function OrderFlowDiagram({ mode }: { mode: Mode }) {
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
