/**
 * BuySniperModal — high-conviction buy setups (1–4 cards)
 * Fed by POST /api/scan/find-buys (buy_sniper.py)
 */
import React from "react";

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
  if (!isOpen) return null;

  const list = Array.isArray(suggestions) ? suggestions : [];

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/75 backdrop-blur-sm"
      role="dialog"
      aria-modal="true"
      aria-label="High-conviction buy setups"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="bg-graphite border border-emerald-500/25 rounded-2xl max-w-4xl w-full max-h-[90vh] overflow-y-auto shadow-2xl">
        {/* Header */}
        <div className="sticky top-0 z-10 flex items-start justify-between gap-4 px-5 py-4 border-b border-slate/70 bg-graphite/95 backdrop-blur">
          <div>
            <h2 className="text-lg font-semibold text-emerald-400 tracking-tight">
              🎯 High-Conviction Buy Setups
            </h2>
            <p className="text-xs text-mist/70 mt-0.5 font-mono">
              ≤ ₹5000 · PREPARE TO BUY / BUY NOW · entry, target, stop, hold window
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="font-mono text-mist hover:text-paper text-lg leading-none px-2 py-1 rounded-lg hover:bg-ink/60 transition"
            aria-label="Close"
          >
            ✕
          </button>
        </div>

        <div className="p-5">
          {loading ? (
            <div className="py-14 text-center">
              <div className="inline-flex items-center gap-2 font-mono text-sm text-emerald-400 animate-pulse">
                <span className="inline-block w-3.5 h-3.5 rounded-full border-2 border-t-transparent border-emerald-400 animate-spin" />
                Sniping top buy opportunities…
              </div>
              <p className="text-[11px] text-mist/50 mt-2 font-mono">
                Scoring conviction · entry ranges · targets
              </p>
            </div>
          ) : error ? (
            <div className="py-10 text-center rounded-xl border border-signal-sell/30 bg-signal-sell/5 px-4">
              <p className="font-mono text-xs text-signal-sell uppercase tracking-widest mb-1">
                Sniper error
              </p>
              <p className="text-sm text-signal-sell/90 break-words">{error}</p>
            </div>
          ) : list.length === 0 ? (
            <div className="py-12 text-center">
              <p className="font-mono text-sm text-mist/80">
                No ≤ ₹5000 setups meet conviction / decision criteria right now.
              </p>
              <p className="text-[11px] text-mist/45 mt-2 font-mono max-w-md mx-auto">
                Need PREPARE TO BUY / BUY NOW (or strong HOLD breakout) with conviction ≥ 58 and a
                valid price. Run a lite/full scan first, then try again.
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {list.map((s) => (
                <div
                  key={s.symbol}
                  className="bg-ink/50 border border-slate/70 hover:border-emerald-500/35 rounded-xl p-4 transition"
                >
                  <div className="flex justify-between items-start gap-2 mb-3">
                    <div className="min-w-0">
                      <button
                        type="button"
                        onClick={() => onSelectSymbol?.(s.symbol)}
                        className="text-lg font-bold text-paper font-mono hover:text-emerald-300 transition truncate"
                      >
                        {s.symbol}
                      </button>
                      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                        <span
                          className={`font-mono text-[10px] px-2 py-0.5 rounded border ${actionBadgeClass(
                            s.action
                          )}`}
                        >
                          {s.action}
                        </span>
                        {s.sector ? (
                          <span className="font-mono text-[10px] text-mist/50 truncate max-w-[8rem]">
                            {s.sector}
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <div className="text-right shrink-0">
                      <div className="font-mono text-sm font-bold text-amber-300">
                        {s.conviction_score}%
                      </div>
                      <div className="font-mono text-[9px] uppercase tracking-wider text-mist/45">
                        Match
                      </div>
                    </div>
                  </div>

                  <div className="space-y-1.5 font-mono text-[11px] text-mist/90">
                    <div className="flex justify-between gap-2">
                      <span className="text-mist/45 shrink-0">Buy range</span>
                      <span className="text-paper font-semibold text-right">
                        {s.buy_price_range ||
                          `${fmtInr(s.buy_price_low)} – ${fmtInr(s.buy_price_high)}`}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist/45 shrink-0">Best entry</span>
                      <span className="text-emerald-300/90 text-right">
                        {s.entry_time || s.entry_window || "—"}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist/45 shrink-0">Target (exit)</span>
                      <span className="text-emerald-400 font-bold text-right">
                        {fmtInr(s.target_price)}
                        {s.estimated_profit ? (
                          <span className="text-mist/60 font-normal ml-1">
                            ({s.estimated_profit})
                          </span>
                        ) : null}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist/45 shrink-0">Stop loss</span>
                      <span className="text-rose-400 font-bold text-right">
                        {fmtInr(s.stop_loss)}
                      </span>
                    </div>
                    <div className="flex justify-between gap-2">
                      <span className="text-mist/45 shrink-0">Holding</span>
                      <span className="text-right">
                        {s.holding_duration || s.holding_period || "—"}
                      </span>
                    </div>
                    {(s.technical_score != null || s.fundamental_score != null) && (
                      <div className="flex justify-between gap-2 pt-0.5">
                        <span className="text-mist/45 shrink-0">Tech / Fund</span>
                        <span className="text-right text-mist/80">
                          {s.technical_score ?? "—"} / {s.fundamental_score ?? "—"}
                        </span>
                      </div>
                    )}
                  </div>

                  {s.rationale ? (
                    <p className="text-[11px] text-mist/55 mt-3 pt-2 border-t border-slate/50 leading-relaxed">
                      {s.rationale}
                    </p>
                  ) : null}

                  {onSelectSymbol ? (
                    <button
                      type="button"
                      onClick={() => onSelectSymbol(s.symbol)}
                      className="mt-3 w-full font-mono text-[11px] text-emerald-300/90 border border-emerald-600/30 rounded-lg py-1.5 hover:bg-emerald-950/40 transition"
                    >
                      Open {s.symbol} analysis →
                    </button>
                  ) : null}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="px-5 pb-4 flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="font-mono text-xs text-mist hover:text-paper border border-slate/60 rounded-lg px-4 py-1.5 transition"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

export default BuySniperModal;
