// E538 (redesign round 5b): the "positions" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import { ExposureStrip } from "../ui/ExposureStrip";
import { railView } from "../../v7View";
import { positionStocksApi } from "../../positionStocksApi";
import { EMOTION_LABEL, FALLBACK_EMOTIONS } from "../board/journalNotesView";
import SellOrdersCard from "../board/SellOrdersCard";
import { PositionRow } from "./parts";
import type { PsState } from "./usePositionStocks";

export function PsPositionsTab({ s }: { s: PsState }) {
  const { status, positions, busy, manualBuySymbol, setManualBuySymbol, manualBuyQty, setManualBuyQty, manualBuyReason, setManualBuyReason, manualBuyEmotion, setManualBuyEmotion, lastRefreshed, subTab, loggedIn, loadAll, doAction, doManualBuy, doClosePosition, openPositions, closedToday, rejectedEntriesToday } = s;
  return (
      <>
      <SellOrdersCard active={subTab === "positions"} fetcher={() => positionStocksApi.shortOrders()} />
      {/* ── ADDED (this session): manual controls — Refresh / Check Exits
          Now (reconcile) / Manual Buy — directly on the Positions tab, so
          you don't have to switch to Pipeline to reconcile or wait 15s
          for the next poll to see a fresh price. Manual Exit lives per-row
          below instead, since it needs a specific position id. */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Manual Controls</p>
        <div className="flex flex-wrap items-center gap-2 mb-3">
          <button disabled={busy !== null} onClick={() => void loadAll()}
            className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40">
            ↻ Refresh
          </button>
          <button disabled={!loggedIn || busy !== null} onClick={() => doAction("reconcile")}
            className="px-4 py-2 rounded-xl bg-graphite border border-slate font-display tabular-nums text-xs text-mist disabled:opacity-40">
            {busy === "reconcile" ? "Checking…" : "Check Exits Now (Reconcile)"}
          </button>
          {lastRefreshed && (
            <span className="font-display tabular-nums text-[9px] text-mist">
              Updated {lastRefreshed.toLocaleTimeString()}
            </span>
          )}
        </div>
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Symbol</p>
            <input value={manualBuySymbol} onChange={e => setManualBuySymbol(e.target.value)}
              placeholder="e.g. RELIANCE" disabled={!loggedIn || busy !== null}
              className="px-3 py-2 rounded-xl bg-ink border border-slate font-display tabular-nums text-xs text-paper w-36 disabled:opacity-40" />
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Qty (optional — blank = auto-size)</p>
            <input value={manualBuyQty} onChange={e => setManualBuyQty(e.target.value)}
              placeholder="auto" inputMode="numeric" disabled={!loggedIn || busy !== null}
              className="px-3 py-2 rounded-xl bg-ink border border-slate font-display tabular-nums text-xs text-paper w-24 disabled:opacity-40" />
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Feeling (optional)</p>
            <select value={manualBuyEmotion} onChange={e => setManualBuyEmotion(e.target.value)} disabled={!loggedIn || busy !== null}
              className="px-2 py-2 rounded-xl bg-ink border border-slate font-display text-xs text-paper disabled:opacity-40">
              <option value="">-</option>
              {FALLBACK_EMOTIONS.map(e => <option key={e} value={e}>{EMOTION_LABEL[e]}</option>)}
            </select>
          </div>
          <div>
            <p className="text-[9px] text-mist uppercase tracking-widest mb-1">Why (optional, max 300)</p>
            <input value={manualBuyReason} onChange={e => setManualBuyReason(e.target.value)} maxLength={300}
              placeholder="e.g. breakout above day high" disabled={!loggedIn || busy !== null}
              className="px-3 py-2 rounded-xl bg-ink border border-slate font-display text-xs text-paper w-56 disabled:opacity-40" />
          </div>
          <button disabled={!loggedIn || busy !== null || !manualBuySymbol.trim()} onClick={() => void doManualBuy()}
            className="px-4 py-2 rounded-xl bg-profit/20 border border-profit font-display tabular-nums text-xs text-profit disabled:opacity-40">
            {busy === "manual_buy" ? "Placing…" : "Buy Now"}
          </button>
        </div>
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Manual BUY still goes through every real gate an automatic entry does — armed, kill switch,
          order budget, max concurrent positions, and available capital. It just skips the screener's
          own symbol selection.
        </p>
      </div>

      {/* ── Open positions ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Open Positions ({openPositions.length}/{status?.max_concurrent_scalp_positions ?? 5})</p>
        {openPositions.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No open scalp positions.</p>
        ) : (
          <div className="space-y-2">
            {/* design v7: where the open money sits, long vs short, from the open rows this tab already holds */}
            <ExposureStrip rows={openPositions.map(p => ({
              symbol: p.symbol, side: p.side, qty: p.quantity, entry: p.entry_price, pnl: p.unrealized_pnl,
              riskUsedPct: p.overnight_converted_to_cnc ? undefined : railView({ side: p.side, entry: p.entry_price, stop: p.stop_price, target: p.target_price, price: p.current_price }).riskUsedPct,
            }))} />
            {openPositions.map(p => <PositionRow key={p.id} p={p} onClose={doClosePosition} busy={busy} loggedIn={loggedIn} />)}
          </div>
        )}
      </div>

      {/* ── Closed today ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <p className="dash-section-title mb-3">Closed Today ({closedToday.length})</p>
        {closedToday.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No positions closed yet today.</p>
        ) : (
          <div className="space-y-2">{closedToday.map(p => <PositionRow key={p.id} p={p} />)}</div>
        )}
      </div>

      {/* ── Rejected entries (group192) — orders Dhan refused; no shares bought, not trades ── */}
      {rejectedEntriesToday.length > 0 && (
        <details className="bg-graphite border border-slate rounded-2xl p-4">
          <summary className="dash-section-title cursor-pointer">
            Rejected Entries Today ({rejectedEntriesToday.length}) — no shares bought
          </summary>
          <div className="space-y-2 mt-3">{rejectedEntriesToday.map(p => <PositionRow key={p.id} p={p} />)}</div>
        </details>
      )}

      </>
      );
}
