// E538 (redesign round 5b): the module-level types, formatters and small components that PositionStocksTab.tsx used to hold above and below the page.
// Moved unchanged and exported so the hook and the ten sub-tab files can share them.
import { RiskRail } from "../ui/RiskRail";
import { entryReason } from "../../setupView";
import { type ScalpStatus, type ScalpPositionRow, type ScalpCandidateRow, type ScalpCycleResult, type ScalpPipelineStatus } from "../../positionStocksApi";
import { PriceRoll, GlintBox } from "../ui/motion";
import { openCharges as moneyOpenCharges, afterCharges } from "../../moneyView";
import StructurePanel from "../StructurePanel";
import StockCandleBlock from "../StockCandleBlock";
import type { PriceLevel } from "../../chartContext";
import { isShort, legLabels, pnlPctFromPrices, sideBadge } from "../../positionSide";

export type Window = "1m" | "5m" | "15m" | "60m";

// group 259: "2026-09-01" -> "01 Sep" for the charges date ranges.
export const _CHG_MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
export function fmtChargeDay(d: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || "");
  return m ? `${m[3]} ${_CHG_MONTHS[Number(m[2]) - 1] || m[2]}` : (d || "—");
}

export function fmtInr(n: number | null | undefined, decimals = 0): string {
  if (n == null || Number.isNaN(n)) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1_00_00_000) return `${sign}₹${(abs / 1_00_00_000).toFixed(1)}Cr`;
  if (abs >= 1_00_000) return `${sign}₹${(abs / 1_00_000).toFixed(1)}L`;
  return `${sign}₹${abs.toLocaleString("en-IN", { maximumFractionDigits: decimals })}`;
}

export function fmtHms(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

// Same safe-extraction helper as RealAutoTrade.tsx — Dhan's fund object has
// inconsistent casing/typos (e.g. "availabelBalance") across SDK versions.
export function pickNum(obj: any, ...keys: string[]): number | null {
  for (const k of keys) {
    const v = obj?.[k];
    if (typeof v === "number") return v;
    if (typeof v === "string" && v.trim() !== "" && !Number.isNaN(Number(v))) return Number(v);
  }
  return null;
}

export function fmtDateTimeIst(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso);
    const date = d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", timeZone: "Asia/Kolkata" });
    const time = d.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" });
    return `${date}, ${time} IST`;
  } catch { return iso; }
}

export function statusColor(status: string): string {
  if (status === "OPEN") return "text-signal-prepare";
  if (status === "TARGET_HIT") return "text-signal-buy";
  if (status === "STOP_HIT") return "text-signal-sell";
  if (status === "EOD_SQUAREOFF" || status === "MANUAL_EXIT") return "text-signal-hold";
  // BUG FIX (this session): STAGNATION_EXIT is now a real status (was
  // silently stored as "MANUAL_EXIT" before, see backend) — give it its
  // own color so it's visually distinguishable from a true manual exit,
  // not just a hold-orange lookalike.
  if (status === "STAGNATION_EXIT") return "text-signal-prepare";
  // E275: the momentum-health exit booked the trade (in profit by construction): a win, same colour as a target
  if (status === "MOMENTUM_EXIT") return "text-signal-buy";
  // group 340: price sat under the stored stop and the watchdog closed it at market - a stop failure, loss colour.
  if (status === "STOP_BREACH_EXIT") return "text-signal-sell";
  // group 466: Close-all-and-disarm exit (plain MARKET SELL, reconcile resolves the fill)
  if (status === "CLOSE_ALL") return "text-signal-prepare";
  // E114: a plain-order short reached its target and was covered by hand (software target): a win, same colour as a bracket target
  if (status === "TARGET_EXIT") return "text-signal-buy";
  return "text-signal-avoid";
}

// group 340: a STOP_HIT that closed in profit is the trailing stop doing its job - say so instead of a loss-looking STOP_HIT.
export function statusLabel(p: { status: string; realized_pnl?: number | null }): string {
  if (p.status === "STOP_HIT" && (p.realized_pnl ?? 0) > 0) return "TRAIL_STOP";
  return p.status;
}

// Capital utilisation bar
export function CapitalBar({ allocated, available }: { allocated: number; available: number }) {
  if (!allocated || allocated <= 0) return null;
  const used = allocated - available;
  const usedPct = Math.min(100, Math.max(0, (used / allocated) * 100));
  const color = usedPct > 80 ? "bg-signal-sell" : usedPct > 50 ? "bg-signal-hold" : "bg-signal-buy";
  return (
    <div className="mt-3">
      <div className="flex justify-between font-display tabular-nums text-[9px] text-mist mb-1">
        <span>Capital utilised: {usedPct.toFixed(1)}%</span>
        <span>{fmtInr(used)} / {fmtInr(allocated)}</span>
      </div>
      <div className="h-1.5 rounded-full bg-slate overflow-hidden">
        <div className={`h-full rounded-full transition-all ${color}`} style={{ width: `${usedPct}%` }} />
      </div>
    </div>
  );
}

// Circuit breaker badge
export function CBadge({ cb }: { cb: ScalpStatus["circuit_breaker"] | undefined }) {
  if (!cb) return <span className="text-mist">—</span>;
  const stateColor = cb.state === "closed" ? "text-signal-buy" : cb.state === "open" ? "text-signal-sell" : "text-signal-hold";
  return (
    <span className={`font-display tabular-nums text-xs font-bold uppercase ${stateColor}`}>
      {cb.state}
      {cb.state === "open" && cb.seconds_until_retry != null
        ? ` (retry in ${cb.seconds_until_retry}s)`
        : cb.state !== "closed"
          ? ` (${cb.consecutive_failures}/${cb.failure_threshold} fails)`
          : ""}
    </span>
  );
}

// AUDIT ADD (this session): one stage of the Run Cycle / Auto-Pilot
// pipeline dashboard — WS Feed → Scan → Liquidity Gate → Rank → Quality
// Gate → Entry. `metric` is the live number that stage is currently
// producing (subscribed symbol count, candidates surfaced, etc.); `detail`
// lines are the static configured rule(s) that stage enforces, pulled from
// status.pipeline_config so they can never drift from what the backend is
// actually doing.
export function PipelineStage({
  n, title, metric, metricLabel, detail, active,
}: {
  n: number; title: string; metric?: string | number | null; metricLabel?: string;
  detail: string[]; active: boolean;
}) {
  return (
    <div className={`rounded-xl border p-3 flex-1 min-w-[min(150px,100%)] ${active ? "bg-signal-buy/5 border-signal-buy/20" : "bg-ink border-slate"}`}>
      <div className="flex items-center gap-1.5 mb-1.5">
        <div className={`w-4 h-4 rounded-full flex items-center justify-center text-[9px] font-bold flex-shrink-0 ${
          active ? "bg-signal-buy/20 text-signal-buy" : "bg-graphite text-mist"
        }`}>{n}</div>
        <p className="font-display tabular-nums text-[10px] uppercase tracking-widest text-mist">{title}</p>
      </div>
      {metric != null && (
        <p className={`font-display tabular-nums text-lg font-bold ${active ? "text-signal-buy" : "text-paper"}`}>
          {metric}
          {metricLabel && <span className="text-[9px] text-mist ml-1 font-normal">{metricLabel}</span>}
        </p>
      )}
      <div className="mt-1 space-y-0.5">
        {detail.map((d, i) => (
          <p key={i} className="font-display tabular-nums text-[9px] text-mist leading-tight">{d}</p>
        ))}
      </div>
    </div>
  );
}

export function PipelineArrow() {
  return <div className="hidden sm:flex items-center text-mist text-lg px-1 select-none">→</div>;
}

export function fmtMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(2)}s`;
}

// AUDIT ADD (this session): renders the full stage-by-stage breakdown a
// Run Cycle (manual OR the last automatic tick) actually did — what each
// stage took, and which stock(s) it looked at / picked. Requested
// specifically so "Run Cycle Now" shows more than a one-line tally.
export function RunCycleResultPanel({ result }: { result: ScalpCycleResult }) {
  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4">
      <div className="flex items-center justify-between mb-3 flex-wrap gap-2">
        <p className="dash-section-title">
          Run Cycle Result — {result.trigger === "MANUAL" ? "manual click" : "automatic tick"}
        </p>
        <div className="flex items-center gap-3 font-display tabular-nums text-[10px]">
          <span className="text-paper">{fmtDateTimeIst(result.started_at)}</span>
          <span className="text-signal-prepare">total {fmtMs(result.total_duration_ms)}</span>
        </div>
      </div>

      {/* ── Headline: what actually happened ── */}
      <div className="flex flex-wrap gap-2 mb-3">
        <span className="px-2 py-1 rounded-lg bg-ink border border-slate font-display tabular-nums text-[10px] text-mist">
          {result.candidates_seen} candidate(s) seen
        </span>
        {result.entered_symbol && (
          <span className="px-2 py-1 rounded-lg bg-signal-buy/15 border border-signal-buy/40 font-display tabular-nums text-[10px] text-signal-buy">
            ENTERED {result.entered_symbol}
          </span>
        )}
        {result.eod_fired && (
          <span className="px-2 py-1 rounded-lg bg-signal-hold/15 border border-signal-hold/40 font-display tabular-nums text-[10px] text-signal-hold">
            EOD squareoff fired
          </span>
        )}
        {result.skipped_reason && (
          <span className="px-2 py-1 rounded-lg bg-signal-avoid/15 border border-signal-avoid/40 font-display tabular-nums text-[10px] text-signal-avoid">
            skipped: {result.skipped_reason}
          </span>
        )}
      </div>

      {/* ── Stage-by-stage timing ── */}
      <div className="space-y-1.5">
        {result.stages.map((stage, i) => (
          <div key={i} className="rounded-xl border border-slate bg-ink p-2.5">
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-2">
                <span className="w-4 h-4 rounded-full bg-graphite text-mist flex items-center justify-center text-[9px] font-bold flex-shrink-0">{i + 1}</span>
                <span className="font-display tabular-nums text-[11px] text-paper">{stage.label}</span>
              </div>
              <span className="font-display tabular-nums text-[10px] text-signal-prepare">{fmtMs(stage.duration_ms)}</span>
            </div>
            {stage.detail && (
              <p className="font-display tabular-nums text-[10px] text-mist mt-1 leading-relaxed">{stage.detail}</p>
            )}

            {/* Scan stage: top candidates surfaced, by symbol */}
            {stage.candidates && stage.candidates.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                {stage.candidates.map((c, j) => (
                  <span key={j} className="px-1.5 py-0.5 rounded-md bg-graphite border border-slate font-display tabular-nums text-[9px] text-paper">
                    {c.symbol} <span className="text-mist">{c.window}</span> {c.pct_change >= 0 ? "+" : ""}{c.pct_change}%
                  </span>
                ))}
              </div>
            )}

            {/* Quality gate stage: pass/fail per symbol checked */}
            {stage.checked && stage.checked.length > 0 && (
              <div className="mt-2 space-y-1">
                {stage.checked.map((c, j) => (
                  <div key={j} className="flex items-center gap-1.5 flex-wrap">
                    <span className={`px-1.5 py-0.5 rounded-md border font-display tabular-nums text-[9px] ${
                      c.passed ? "bg-signal-buy/15 border-signal-buy/40 text-signal-buy" : "bg-signal-avoid/15 border-signal-avoid/40 text-signal-avoid"
                    }`}>
                      {c.passed ? "✓" : "✗"} {c.symbol}
                    </span>
                    {c.reason && <span className="font-display tabular-nums text-[9px] text-mist">{c.reason}</span>}
                  </div>
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ADDED (session48 — "no live process shows and no stock name shows"):
// live counterpart to RunCycleResultPanel above. That panel only ever
// rendered a *completed* cycle's result; this one shows what the
// background AUTO loop (or an in-flight manual click) is doing right now,
// fed by the new GET /pipeline/status poll. Most ticks finish in
// single-digit milliseconds with nothing to catch mid-flight — the "Idle"
// state below is the common, correct, non-broken case — but a tick that
// reaches Quality Gate makes real outbound HTTP calls and can run for
// real seconds, which is exactly when this becomes useful. Unlike
// real-trade-service's equivalent, quality-gate checks here run
// concurrently (asyncio.gather, see main.py's _run_cycle) rather than one
// symbol at a time, so there's no single "currently checking symbol X of
// N" to show — only the most recently completed stage and, once scan has
// run, which stock(s) it surfaced.
export function LivePipelineStatus({ live }: { live: ScalpPipelineStatus | null }) {
  if (!live) {
    return (
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-2">Live cycle status</p>
        <p className="font-display tabular-nums text-[11px] text-mist">Loading…</p>
      </div>
    );
  }
  return (
    <div className="bg-graphite border border-slate rounded-2xl p-4 space-y-2">
      <div className="flex items-center justify-between">
        <p className="dash-section-title">Live cycle status</p>
        {live.running ? (
          <span className="font-display tabular-nums text-[10px] text-signal-buy flex items-center gap-1.5">
            <span className="w-1.5 h-1.5 rounded-full bg-signal-buy animate-pulse" />
            {live.trigger === "AUTO" ? "Auto-Pilot cycle running" : "Cycle running"}
          </span>
        ) : (
          <span className="font-display tabular-nums text-[10px] text-mist">Idle — no cycle running</span>
        )}
      </div>
      {live.running && (
        <div className="bg-ink border border-slate rounded-xl p-3 space-y-2">
          <p className="font-display tabular-nums text-xs text-paper">{live.stage_label || live.stage}</p>
          {live.candidates.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {live.candidates.map((c, j) => (
                <span key={j} className="px-1.5 py-0.5 rounded-md bg-graphite border border-slate font-display tabular-nums text-[9px] text-paper">
                  {c.symbol} <span className="text-mist">{c.window}</span> {c.pct_change >= 0 ? "+" : ""}{c.pct_change}%
                </span>
              ))}
            </div>
          )}
        </div>
      )}
      {/* Show last completed cycle summary even when idle */}
      {!live.running && live.last_cycle && (
        <div className="bg-ink border border-slate rounded-xl p-2.5 space-y-1.5">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-display tabular-nums text-[10px] text-mist">
              Last cycle — {live.last_cycle.trigger} at {fmtDateTimeIst(live.last_cycle.started_at)}
            </span>
            <span className="font-display tabular-nums text-[10px] text-signal-prepare">
              {fmtMs(live.last_cycle.total_duration_ms)}
            </span>
            {live.last_cycle.candidates_seen > 0 && (
              <span className="font-display tabular-nums text-[10px] text-paper">
                {live.last_cycle.candidates_seen} candidate(s) seen
              </span>
            )}
            {live.last_cycle.entered_symbol && (
              <span className="px-1.5 py-0.5 rounded-md bg-signal-buy/15 border border-signal-buy/40 font-display tabular-nums text-[9px] text-signal-buy">
                ENTERED {live.last_cycle.entered_symbol}
              </span>
            )}
            {live.last_cycle.skipped_reason && (
              <span className="font-display tabular-nums text-[9px] text-mist">{live.last_cycle.skipped_reason}</span>
            )}
          </div>
          {/* Show the scan stage candidates from last_cycle if any */}
          {(() => {
            const scanStage = live.last_cycle.stages?.find((s: any) => s.name === "scan");
            if (!scanStage?.candidates?.length) return null;
            return (
              <div className="flex flex-wrap gap-1">
                {scanStage.candidates.map((c: any, j: number) => (
                  <span key={j} className="px-1.5 py-0.5 rounded-md bg-graphite border border-slate font-display tabular-nums text-[9px] text-paper">
                    {c.symbol} <span className="text-mist">{c.window}</span> {c.pct_change >= 0 ? "+" : ""}{c.pct_change}%
                  </span>
                ))}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}

export function CandidateList({ rows }: { rows: ScalpCandidateRow[] }) {
  if (rows.length === 0) return <p className="font-display tabular-nums text-[11px] text-mist">No candidates.</p>;
  return (
    <div className="space-y-1">
      {rows.slice(0, 10).map((c, i) => (
        <div key={`${c.symbol}-${c.window}-${i}`} className="flex items-center justify-between border-t border-slate pt-1 first:border-t-0 first:pt-0">
          <span className="font-display tabular-nums text-xs text-paper">{c.symbol}</span>
          <span className={`font-display tabular-nums text-xs ${c.pct_change >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
            {c.pct_change >= 0 ? "+" : ""}{c.pct_change.toFixed(2)}%
          </span>
          <span className="font-display tabular-nums text-[10px] text-mist">score {c.composite_score.toFixed(1)}</span>
        </div>
      ))}
    </div>
  );
}

// Round 270: estimated cost of getting in and out of an OPEN row at its live price (same rate card as the Charges tab); null without a live price.
export function psOpenCharges(p: ScalpPositionRow): number | null {
  return moneyOpenCharges({ side: p.side, qty: p.quantity, entry: p.entry_price, price: p.current_price, delivery: !!p.overnight_converted_to_cnc });
}

export function PositionRow({ p, onClose, busy, loggedIn }: {
  p: ScalpPositionRow;
  onClose?: (id: number, symbol: string) => void;
  busy?: string | null;
  loggedIn?: boolean;
}) {
  const pnlPct = p.realized_pnl_pct ?? pnlPctFromPrices(p);   // short-aware (a short books entry - exit)
  const legs = legLabels(p);
  // ADDED (this session — "manual exit if needed"): only offer Exit Now
  // for a position that's actually still open at the broker (OPEN, or
  // EXIT_LEGS_REJECTED — a position whose bracket legs failed to place
  // and so has no working target/stop at all, the case that most needs
  // a manual way out).
  const canClose = onClose && (p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED");
  return (
    <GlintBox value={p.current_price} className={`v7-pos${isShort(p) ? " short" : ""} border border-slate rounded-xl p-3`}>
      <div className="flex items-center justify-between mb-2">
        <span className="font-display tabular-nums font-bold text-sm text-paper">{p.symbol}{isShort(p) && (
          <span className="v7-side short ml-2" title="Short: sold first, covered (bought back) later. Intraday only."><i aria-hidden="true">↘</i>{sideBadge(p)}</span>
        )}</span>
        <div className="flex items-center gap-2">
          <span className="font-display tabular-nums text-[10px] text-mist">{p.window_source}</span>
          <span className={`font-display tabular-nums text-[10px] uppercase font-bold ${statusColor(p.status)}`}>{statusLabel(p)}</span>
          {/* AUDIT ADD (2026-09-19): overnight-carry badge — nothing on
              this card previously distinguished a CNC-carried position
              from a normal same-day INTRADAY one. */}
          {p.overnight_converted_to_cnc && (
            <span className="font-display tabular-nums text-[10px] text-signal-prepare" title="Converted to CNC and carried overnight">🌙 CNC</span>
          )}
          {canClose && (
            <button disabled={!loggedIn || busy !== null} onClick={() => onClose!(p.id, p.symbol)}
              className="px-2 py-1 rounded-lg bg-signal-sell/20 border border-signal-sell font-display tabular-nums text-[10px] text-signal-sell disabled:opacity-40">
              {busy === `close_${p.id}` ? "Exiting…" : "Exit Now"}
            </button>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-2 text-[11px]">
        <div><span className="text-mist">{legs.entry} </span><span className="tabular-nums text-paper">₹{p.entry_price.toFixed(2)}</span></div>
        <div><span className="text-mist">{legs.exit} </span><span className="tabular-nums text-paper">{p.exit_price != null ? `₹${p.exit_price.toFixed(2)}` : "—"}</span></div>
        <div><span className="text-mist">Qty </span><span className="tabular-nums text-paper">{p.quantity}</span></div>
        {/* AUDIT ADD (this session): order value (price × qty) — the actual
            rupee amount bought/sold, distinct from the per-share price
            already shown above, same distinction Trade History's Buy/Sell
            Total columns make. */}
        <div><span className="text-mist">Total </span><span className="tabular-nums text-paper">
          {fmtInr(p.entry_price * p.quantity)}{p.exit_price != null ? ` → ${fmtInr(p.exit_price * p.quantity)}` : ""}
        </span></div>
        {/* E272: why this trade was taken (setup stamp); "-" for a trade opened before entries were stamped */}
        {(() => { const er = entryReason(p.setup); return (
          <div title={er.title} data-testid="entry-reason"><span className="text-mist">Entry reason </span>
            <span className={`tabular-nums ${er.known ? "text-paper" : "text-mist"}`}>{er.label}{er.known && <span className="ml-1 text-[9px] text-mist">{er.slot}</span>}</span></div>); })()}
        <div>
          <span className="text-mist">Target </span>
          {p.overnight_converted_to_cnc ? (
            // AUDIT FIX (2026-09-19): the intraday bracket's TARGET_LEG was
            // cancelled the moment this position converted to CNC for the
            // overnight carry — showing p.target_price here would show a
            // dead order that will never fill.
            <span className="tabular-nums text-mist text-[10px]">— (bracket cancelled, carried overnight)</span>
          ) : (
            <span className="tabular-nums text-signal-buy">₹{p.target_price.toFixed(2)} <span className="text-[9px]">({p.adaptive_target_pct.toFixed(1)}%)</span></span>
          )}
        </div>
        <div><span className="text-mist">Stop loss </span>
          {p.overnight_converted_to_cnc ? (
            // AUDIT FIX (2026-09-19): same as Target above — the real
            // protection now is the plain STOP_LOSS_MARKET order at
            // overnight_stop_price, not the cancelled p.stop_price.
            <span className="tabular-nums text-signal-sell">
              {p.overnight_stop_price != null ? `₹${p.overnight_stop_price.toFixed(2)}` : "NONE"}
              <span className="text-[9px]"> {p.overnight_stop_order_id ? "🌙 overnight" : "⚠ opted out, no stop"}</span>
            </span>
          ) : (
            <span className="tabular-nums text-signal-sell">₹{p.stop_price.toFixed(2)} <span className="text-[9px]">({p.adaptive_stop_pct.toFixed(1)}%)</span></span>
          )}
          {/* this session: shows whether/where breakeven-stop will move
              (or already moved) this position's stop — see
              orders/breakeven.py. Only rendered when a trigger was
              actually recorded (positions predating the feature have
              none — see models.py comment). Breakeven doesn't apply once
              a position is carried overnight (the bracket is gone). */}
          {!p.overnight_converted_to_cnc && p.breakeven_trigger_pct != null && (
            <span className={`ml-1 text-[9px] ${p.stop_moved_to_breakeven ? "text-signal-buy" : "text-mist"}`}>
              {p.stop_moved_to_breakeven ? "🔒 breakeven" : `BE@${p.breakeven_trigger_pct.toFixed(0)}%`}
            </span>
          )}
        </div>
      </div>
      {/* design v7: stop / entry / target to scale for a live position (long or short); not drawn once the bracket is gone (CNC carry) */}
      {(p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED") && !p.overnight_converted_to_cnc && p.stop_price > 0 && p.target_price > 0 && (
        <RiskRail side={p.side} entry={p.entry_price} stop={p.stop_price} target={p.target_price} price={p.current_price} />
      )}
      {/* group354: how the daily-structure gate saw this entry (collapsed one-liner; renders nothing when there is no snapshot) */}
      {p.structure && <StructurePanel snapshot={p.structure} atrStop={p.stop_price} />}
      {/* AUDIT ADD (session60): live current price / unrealized P&L for an
          OPEN position — mirrors real-trade-service's Positions tab, which
          has always shown Current + a live +/-% next to Entry/Stop/Target.
          Only rendered for OPEN rows since current_price is only populated
          server-side while a position is open. "—" when this service's
          Angel One feed hasn't ticked the symbol yet (same fail-open
          convention as the rest of this dashboard). */}
      {/* BUG FIX (this session): was `p.status === "OPEN"` only — now also
          shown for EXIT_LEGS_REJECTED, which is real live exposure too
          (see openPositions fix above) and now gets a live price from the
          backend as well. */}
      {(p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED") && (
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] mt-2 pt-2 border-t border-slate/50">
          <div><span className="text-mist">Current </span><span className="tabular-nums text-paper"><PriceRoll value={p.current_price} text={p.current_price != null ? `₹${p.current_price.toFixed(2)}` : "—"} /></span></div>
          <div><span className="text-mist">Value </span><span className="tabular-nums text-paper">{p.current_amount != null ? fmtInr(p.current_amount) : "—"}</span></div>
          <div>
            <span className="text-mist">Unrealized </span>
            {p.unrealized_pnl != null ? (
              <span className={`tabular-nums font-bold ${p.unrealized_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                {fmtInr(p.unrealized_pnl)} ({p.unrealized_pnl_pct?.toFixed(2)}%)
              </span>
            ) : <span className="tabular-nums text-paper">—</span>}
            {/* Round 270: the same open P&L after the estimated charges of closing now */}
            {(() => { const ch = psOpenCharges(p); const net = afterCharges(p.unrealized_pnl, ch);
              return net != null && ch != null ? (
                <div className={`text-[9px] tabular-nums ${net >= 0 ? "text-signal-buy" : "text-signal-sell"}`} data-testid="ps-after-charges">
                  after charges {net >= 0 ? "+" : ""}{fmtInr(net, 2)} <span className="text-mist">(charges ≈ ₹{ch.toFixed(2)})</span>
                </div>) : null; })()}
          </div>
          <div><span className="text-mist">To target/stop </span><span className="tabular-nums text-paper">
            {/* AUDIT FIX (2026-09-19): was requiring BOTH values non-null
                — a carried CNC position never has a target_distance_pct
                (no live target leg, see above) but does have a real
                stop_distance_pct, so it always showed "—" here even
                though the stop distance was known. Show whichever is
                available instead of demanding both. */}
            {p.target_distance_pct != null && p.stop_distance_pct != null
              ? `${p.target_distance_pct.toFixed(1)}% / ${p.stop_distance_pct.toFixed(1)}%`
              : p.stop_distance_pct != null
              ? `— / ${p.stop_distance_pct.toFixed(1)}%`
              : p.target_distance_pct != null
              ? `${p.target_distance_pct.toFixed(1)}% / —`
              : "—"}
          </span></div>
        </div>
      )}
      {p.realized_pnl != null && (
        <p className={`font-display tabular-nums text-xs mt-2 font-bold ${p.realized_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
          P&L {fmtInr(p.realized_pnl)} {pnlPct != null ? `(${pnlPct.toFixed(2)}%)` : ""}
        </p>
      )}
      {/* AUDIT ADD (this session): an ERROR row renders Buy price/Total
          identically to a real trade with no sell yet — but the entry
          order was actually rejected/cancelled by Dhan and never filled,
          so no money was ever at risk. Without this note it reads as an
          open or lost trade. */}
      {p.status === "ERROR" && (
        <p className="font-display tabular-nums text-[10px] text-signal-avoid mt-1">
          ⚠ Entry order was rejected/never filled — no shares bought, no capital was at risk. Buy price/Total above are the attempted order, not a real fill.
          {p.error_message?.startsWith("Entry leg") && p.error_message.includes(": ")
            ? ` Dhan: ${p.error_message.slice(p.error_message.indexOf(": ") + 2)}`
            : ""}
        </p>
      )}
      {p.status !== "ERROR" && (
        <StockCandleBlock symbol={p.symbol} defaultOpen={p.status === "OPEN" || p.status === "EXIT_LEGS_REJECTED"}
          levels={[
            { label: "Entry", price: p.entry_price, tone: "mute" as PriceLevel["tone"] },
            ...(p.stop_price > 0 && !p.overnight_converted_to_cnc ? [{ label: "Stop loss", price: p.stop_price, tone: "bad" as PriceLevel["tone"] }] : []),
            ...(p.target_price > 0 && !p.overnight_converted_to_cnc ? [{ label: "Target", price: p.target_price, tone: "good" as PriceLevel["tone"] }] : []),
          ].filter((l) => Number.isFinite(l.price) && l.price > 0)} />
      )}
      <p className="font-display tabular-nums text-[9px] text-mist mt-1">
        Opened {fmtDateTimeIst(p.opened_at)}
        {p.closed_at ? ` · Closed ${fmtDateTimeIst(p.closed_at)}` : ""}
        {p.is_first_live_order ? " · 🟡 FIRST LIVE ORDER" : ""}
        {p.dhan_super_order_id ? ` · Order ${p.dhan_super_order_id}` : ""}
        {/* AUDIT FIX (session22 cont'd): dhan_exit_order_id was captured by
            reconcile.py for every closed position but never returned by the
            API or shown here — only the entry/bracket order id was visible,
            so there was no way to see which specific leg (target or stop)
            actually filled. Only shown when it's a real, distinct order id
            (reconcile.py falls back to the super order id itself when
            Dhan's leg payload omits its own orderId, in which case there's
            nothing new to show). */}
        {p.dhan_exit_order_id && p.dhan_exit_order_id !== p.dhan_super_order_id
          ? ` · Exit Order ${p.dhan_exit_order_id}`
          : ""}
      </p>
    </GlintBox>
  );
}
