// E31: small presentational pieces of the Real Trade page (moved out of RealAutoTrade.tsx, code unchanged).
import { GlintBox, PriceRoll } from "../ui/motion";
import { type Position } from "../../realTradeApi";
import StockCandleBlock from "../StockCandleBlock";
import { fmtDate, fmtInr, fmtTime, pnlColor, positionLevels } from "./format";

// AUDIT FIX (session64, issue #8 follow-up — "show demat-imported holdings
// under Positions in their own sub-section"): factored the existing
// per-position card out of the old flat positions.map() so it can be
// reused for both the "Auto-Pilot Positions" group and the new "Demat
// Positions" group below without duplicating this JSX twice. Rendering is
// unchanged from before except for the optional "Demat" badge next to the
// symbol when p.broker_imported is true.
export function PositionCard({
  p, onClose, closing,
}: {
  p: Position;
  onClose: () => void;
  closing: boolean;
}) {
  return (
    <GlintBox value={p.current_price} className="bg-graphite border border-slate rounded-2xl p-4">
      <div className="flex items-start justify-between mb-2">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 shrink-0 rounded-full bg-ink border border-slate flex items-center justify-center font-display text-[11px] font-bold text-mist">
            {p.symbol.slice(0, 2)}
          </div>
          <div>
            <span className="font-display text-base font-bold text-paper">{p.symbol}</span>
            <span className="font-display tabular-nums text-[10px] text-mist ml-2">{p.status}</span>
            {p.broker_imported && (
              <span className="font-display tabular-nums text-[9px] text-signal-prepare ml-2 px-1.5 py-0.5 rounded border border-signal-prepare/30 bg-signal-prepare/10">
                DEMAT
              </span>
            )}
            {/* 2026-09-18 fix (follow-on item #6): surfaces WHY this
                position skipped today's EOD square-off — previously only
                ever logged, never visible on the dashboard. */}
            {p.overnight_hold_reason && (
              <span
                title={p.overnight_hold_reason}
                className="font-display tabular-nums text-[9px] text-signal-hold ml-2 px-1.5 py-0.5 rounded border border-signal-hold/30 bg-signal-hold/10"
              >
                🌙 HELD OVERNIGHT
              </span>
            )}
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
          {/* 2026-09-18 fix (follow-on item #2): net-of-cost figure for any
              qty already closed on a PARTIALLY_CLOSED position — null on a
              still-fully-OPEN position (nothing closed yet to have a cost
              on), so this only appears once relevant. */}
          {p.net_realized_pnl != null && (
            <div className={`font-display tabular-nums text-[9px] ${pnlColor(p.net_realized_pnl)}`}>
              net {p.net_realized_pnl >= 0 ? "+" : ""}{fmtInr(p.net_realized_pnl, 2)}
              {p.realized_cost_estimate != null && (
                <span className="text-mist"> (costs ₹{p.realized_cost_estimate.toFixed(0)})</span>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="grid grid-cols-5 gap-2 font-display tabular-nums text-[10px] text-mist mb-3">
        <div>Qty<br/><span className="text-paper font-bold">{p.qty_open}</span></div>
        <div>Entry<br/><span className="text-paper font-bold">₹{p.avg_entry_price}</span></div>
        <div>Current<br/><span className="text-signal-prepare font-bold"><PriceRoll value={p.current_price} text={p.current_price != null ? `₹${p.current_price.toFixed(2)}` : "—"} /></span></div>
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
      <StockCandleBlock symbol={p.symbol} levels={positionLevels(p.avg_entry_price, p.current_stop, p.current_target)} />
      <div className="flex items-center justify-between mt-2">
        <span className="font-display tabular-nums text-[10px] text-mist">{fmtDate(p.opened_at)} {fmtTime(p.opened_at)}</span>
        {p.status === "PENDING_EXIT" ? (
          <span className="font-display tabular-nums text-[10px] text-signal-hold">pending exit…</span>
        ) : (
          <button onClick={onClose} disabled={closing}
            className="font-display tabular-nums text-[10px] px-2 py-1 rounded bg-signal-sell/10 border border-signal-sell/20 text-signal-sell disabled:opacity-40">
            {closing ? "Closing…" : "Close position"}
          </button>
        )}
      </div>
    </GlintBox>
  );
}

// ── Status dot ──────────────────────────────────────────────────────────────
export function Dot({ on, pulse }: { on: boolean; pulse?: boolean }) {
  return (
    <span className={`inline-block w-2 h-2 rounded-full ${on ? "bg-signal-buy" : "bg-signal-sell"} ${pulse && on ? "animate-pulse" : ""}`} />
  );
}

// ── Tiny stat card ──────────────────────────────────────────────────────────
export function StatCard({ label, value, sub, color }: { label: string; value: React.ReactNode; sub?: string; color?: string }) {
  return (
    <div className="rt-stat">
      <p className="rt-stat-label font-display tabular-nums">{label}</p>
      <p className={`rt-stat-value font-display tabular-nums ${color ?? "text-paper"}`}>{value}</p>
      {sub && <p className="rt-stat-sub font-display tabular-nums">{sub}</p>}
    </div>
  );
}

// ── Section header ───────────────────────────────────────────────────────────
export function SectionHdr({ children }: { children: React.ReactNode }) {
  return <p className="rt-section-hdr font-display tabular-nums">{children}</p>;
}
