/**
 * BuySniperModal — high-conviction buy setups (1–4 cards)
 * Fed by POST /api/scan/find-buys (buy_sniper.py)
 *
 * 2026-09-03 UI upgrade: now rendered inside <BottomSheet> (slides up from
 * the bottom on mobile, centered dialog on desktop) with Groww-style cards
 * — rounded, symbol badge circle, large price typography.
 */
import React from "react";
import BottomSheet from "./BottomSheet";

export interface BuySuggestion {
  symbol: string;
  action: string;
  buy_price_range: string;
  buy_price_low?: number;
  buy_price_high?: number;
  entry_time: string;
  entry_window?: string;
  target_price: number;
  stop_loss: number;
  estimated_profit: string;
  estimated_profit_pct?: number;
  holding_duration: string;
  holding_period?: string;
  conviction_score: number;
  technical_score?: number;
  fundamental_score?: number;
  change_pct?: number;
  price?: number;
  sector?: string | null;
  rationale: string;
  decision?: string;
}

export interface BuySniperModalProps {
  isOpen: boolean;
  onClose: () => void;
  suggestions: BuySuggestion[];
  loading?: boolean;
  error?: string | null;
  onSelectSymbol?: (symbol: string) => void;
}

function fmtInr(n: number | undefined | null): string {
  if (n == null || !Number.isFinite(Number(n)) || Number(n) <= 0) return "—";
  return `₹${Number(n).toLocaleString("en-IN", { maximumFractionDigits: 2 })}`;
}

function actionBadgeClass(action: string): string {
  const a = (action || "").toUpperCase();
  if (a.includes("BUY NOW")) {
    return "bg-signal-buy/20 text-signal-buy border-signal-buy/40";
  }
  if (a.includes("BREAKOUT") || a.includes("PREPARE")) {
    return "bg-signal-prepare/20 text-signal-prepare border-signal-prepare/40";
  }
  return "bg-mist/10 text-mist border-slate/60";
}

export function BuySniperModal({
  isOpen,
  onClose,
  suggestions,
  loading = false,
  error = null,
  onSelectSymbol,
}: BuySniperModalProps) {
  const list = Array.isArray(suggestions) ? suggestions : [];

  return (
    <BottomSheet
      isOpen={isOpen}
      onClose={onClose}
      title="🎯 High-Conviction Buy Setups"
      subtitle="≤ ₹5000 · PREPARE TO BUY / BUY NOW · entry, target, stop, hold window"
      footer={
        <button
          type="button"
          onClick={onClose}
          className="w-full sm:w-auto sm:ml-auto sm:block font-display text-sm font-semibold text-mist hover:text-paper border border-slate rounded-xl px-4 py-2 transition"
        >
          Close
        </button>
      }
    >
        <div className="p-5">
          {loading ? (
            <div className="py-14 text-center">
              <div className="inline-flex items-center gap-2 font-display text-sm text-signal-buy animate-pulse">
                <span className="inline-block w-3.5 h-3.5 rounded-full border-2 border-t-transparent border-signal-buy animate-spin" />
                Sniping top buy opportunities…
              </div>
              <p className="text-[11px] text-mist mt-2">
                Scoring conviction · entry ranges · targets
              </p>
            </div>
          ) : error ? (
            <div className="py-10 text-center rounded-2xl border border-signal-sell/30 bg-signal-sell/5 px-4">
              <p className="font-display text-xs text-signal-sell uppercase tracking-widest mb-1">
                Sniper error
              </p>
              <p className="text-sm text-signal-sell/90 break-words">{error}</p>
            </div>
          ) : list.length === 0 ? (
            <div className="py-12 text-center">
              <p className="font-display text-sm text-mist">
                No ≤ ₹5000 setups meet conviction / decision criteria right now.
              </p>
              <p className="text-[11px] text-mist/70 mt-2 max-w-md mx-auto">
                Need PREPARE TO BUY / BUY NOW (or strong HOLD breakout) with conviction ≥ 58 and a
                valid price. Run a lite/full scan first, then try again.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {list.map((s) => (
                <div
                  key={s.symbol}
                  className="bg-ink border border-slate hover:border-signal-buy/40 rounded-2xl p-4 transition shadow-glow-sm"
                >
                  <div className="flex justify-between items-start gap-3 mb-3">
                    <div className="min-w-0 flex items-center gap-2.5">
                      {/* Groww-style symbol avatar badge */}
                      <div className="w-9 h-9 shrink-0 rounded-full bg-graphite border border-slate flex items-center justify-center font-display text-xs font-bold text-mist">
                        {s.symbol.slice(0, 2)}
                      </div>
                      <div className="min-w-0">
                        <button
                          type="button"
                          onClick={() => onSelectSymbol?.(s.symbol)}
                          className="text-base font-display font-bold text-paper hover:text-signal-buy transition truncate block"
                        >
                          {s.symbol}
                        </button>
                        <div className="mt-1 flex flex-wrap items-center gap-1.5">
                          <span
                            className={`text-[10px] font-semibold px-2 py-0.5 rounded-full border ${actionBadgeClass(
                              s.action
                            )}`}
                          >
                            {s.action}
                          </span>
                          {s.sector ? (
                            <span className="text-[10px] text-mist truncate max-w-[8rem]">
                              {s.sector}
                            </span>
                          ) : null}
                        </div>
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="font-display text-base font-extrabold text-paper">
                        {s.conviction_score}%
                      </div>
                      <div className="text-[9px] uppercase tracking-wider text-mist">
                        Match
                      </div>
                    </div>
                  </div>

                  <div className="space-y-1.5 text-[12px] text-paper/90">
                    <div className="flex justify-between gap-2">
                      <span className="text-mist shrink-0">Buy range</span>
                      <span className="text-paper font-semibold text-right">
                        {s.buy_price_range ||
                          `${fmtInr(s.buy_price_low)} – ${fmtInr(s.buy_price_high)}`}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist shrink-0">Best entry</span>
                      <span className="text-signal-buy text-right">
                        {s.entry_time || s.entry_window || "—"}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist shrink-0">Target (exit)</span>
                      <span className="text-signal-buy font-bold text-right">
                        {fmtInr(s.target_price)}
                        {s.estimated_profit ? (
                          <span className="text-mist font-normal ml-1">
                            ({s.estimated_profit})
                          </span>
                        ) : null}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist shrink-0">Stop loss</span>
                      <span className="text-signal-sell font-bold text-right">
                        {fmtInr(s.stop_loss)}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist shrink-0">Holding</span>
                      <span className="text-right">
                        {s.holding_duration || s.holding_period || "—"}
                      </span>
                    </div>
                    {(s.technical_score != null || s.fundamental_score != null) && (
                      <div className="flex justify-between gap-2 pt-0.5">
                        <span className="text-mist shrink-0">Tech / Fund</span>
                        <span className="text-right text-mist">
                          {s.technical_score ?? "—"} / {s.fundamental_score ?? "—"}
                        </span>
                      </div>
                    )}
                  </div>

                  {s.rationale ? (
                    <p className="text-[11px] text-mist mt-3 pt-2 border-t border-slate leading-relaxed">
                      {s.rationale}
                    </p>
                  ) : null}

                  {onSelectSymbol ? (
                    <button
                      type="button"
                      onClick={() => onSelectSymbol(s.symbol)}
                      className="mt-3 w-full text-[12px] font-semibold text-signal-buy border border-signal-buy/30 rounded-xl py-2 hover:bg-signal-buy/10 transition"
                    >
                      Open {s.symbol} analysis →
                    </button>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>
    </BottomSheet>
  );
}

export default BuySniperModal;
