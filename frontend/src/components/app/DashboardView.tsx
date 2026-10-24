import { api, type Decision } from "../../api";
import Pipeline from "../Pipeline";
import DecisionCard from "../DecisionCard";
import SignalStream, { BreadthStrip } from "../SignalStream";
import ScanPanel from "../ScanPanel";
import MarketSentimentHeader from "../MarketSentimentHeader";
import MarketMovers from "../MarketMovers";
import { MarketClock } from "./clocks";
import type { ViewState } from "./appTypes";

// E69 - E31 step 13: the dashboard view (search, scan buttons, market header, idle / loading / error / stock / scan states), moved out of
// App.tsx (JSX unchanged). State and handlers stay in App and arrive as props. formatTime moved with it (only this view used it).
type Setter<T> = (v: T) => void;
type Act<A extends unknown[] = []> = (...a: A) => unknown;

function formatTime(seconds: number): string {
  const s = Math.max(0, Math.floor(Number(seconds) || 0));
  const hours = Math.floor(s / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  if (hours > 0) return `${hours}h ${mins}m ${secs}s`;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

export interface DashboardViewProps {
  view: ViewState; setView: Setter<ViewState>;
  query: string; setQuery: Setter<string>;
  watchlist: string[];
  handleSearch: Act<[string]>; handleScan: Act<[boolean]>; handleStopScan: Act; handleRetry: Act;
  handleAddToWatchlist: Act<[string]>; handleAddManyToWatchlist: Act<[string[], string]>;
  handleSendTopPicks: () => Promise<void>; handleSendAllActionable: () => Promise<void>;
  liveScanRows: Decision[]; scanTransport: "stream" | "poll" | null; scanTaskId: string | null;
  isRetrying: boolean; cancelRequested: boolean; stoppingScan: boolean;
  wsLive: boolean;
  signalFilter: "ALL" | "BUY" | "PREPARE" | "HOLD"; setSignalFilter: Setter<"ALL" | "BUY" | "PREPARE" | "HOLD">;
  openSettings: () => void;
}

export function DashboardView({
  view, setView, query, setQuery, watchlist,
  handleSearch, handleScan, handleStopScan, handleRetry, handleAddToWatchlist, handleAddManyToWatchlist, handleSendTopPicks, handleSendAllActionable,
  liveScanRows, scanTransport, scanTaskId, isRetrying, cancelRequested, stoppingScan, wsLive, signalFilter, setSignalFilter, openSettings,
}: DashboardViewProps) {
  return (
          <>
            {/* Dashboard content — terminal-style */}
            <section className="mb-6 sm:mb-8 dash-terminal">
              <div className="dash-hero terminal-panel">
                <div className="dash-hero-top">
                  <div>
                    <h1 className="page-title">Analyse any stock</h1>
                    <p className="page-desc">
                      Technical, fundamental, news and AI signals combined into one clear decision.
                    </p>
                  </div>
                  <MarketClock />
                </div>

                <div className="dash-search-row">
                  <div className="dash-search">
                    <span className="dash-search-prefix">NSE</span>
                    <input
                      value={query}
                      onChange={(e) => setQuery(e.target.value.toUpperCase())}
                      onKeyDown={(e) => e.key === "Enter" && handleSearch(query)}
                      placeholder="Enter a symbol, e.g. TCS, INFY, RELIANCE"
                      autoComplete="off"
                      spellCheck={false}
                      aria-label="Symbol search"
                    />
                    <button
                      type="button"
                      onClick={() => handleSearch(query)}
                      className="btn-primary-term shrink-0"
                      disabled={!query.trim()}
                    >
                      Analyse
                    </button>
                  </div>
                  <div className="dash-search-actions dash-scan-pair">
                    <button
                      type="button"
                      onClick={() => handleScan(true)}
                      className="btn-scan-lite whitespace-nowrap"
                      title="Quick scan: uses the stored data feed and instant scores, with minimal load on the services (recommended on the free tier)"
                    >
                      Quick scan
                    </button>
                    <button
                      type="button"
                      onClick={() => handleScan(false)}
                      className="btn-scan-full whitespace-nowrap"
                      title="Full scan: runs every check (technical, fundamental, news, events, prediction). Slower."
                    >
                      Full market scan
                    </button>
                  </div>
                </div>
              </div>

              {/* ── NEW: Market Sentiment Header placed above Market Movers ── */}
              <div className="mt-8">
                <MarketSentimentHeader />
              </div>

              <div className="mt-8">
                <MarketMovers onSelect={handleSearch} />
              </div>

              {watchlist.length > 0 && view.mode === "idle" && (
                <div className="flex flex-wrap gap-2 mt-4">
                  {watchlist.slice(0, 10).map((s) => (
                    <button
                      key={s}
                      onClick={() => handleSearch(s)}
                      className="font-mono text-[11px] text-mist hover:text-paper border border-slate/60 hover:border-mist/60 rounded-md px-2.5 py-1 transition"
                    >
                      {s}
                    </button>
                  ))}
                  {watchlist.length > 10 && (
                    <span className="font-mono text-[11px] text-mist/40 py-1">
                      +{watchlist.length - 10} more
                    </span>
                  )}
                </div>
              )}
            </section>

            <section>
              {view.mode === "idle" && (
                <>
                  <BreadthStrip mood={null} />
                  <div className="border border-dashed border-slate rounded-xl p-10 sm:p-16 text-center mb-4">
                    <p className="text-mist/40 font-mono text-xs">
                      Search a symbol or run Market Scan. Last scan results restore after refresh.
                    </p>
                  </div>
                </>
              )}

              {view.mode === "loading" && (
                <div className="w-full max-w-2xl space-y-4">
                  <div className="rounded-xl border border-slate bg-graphite p-6 sm:p-8">
                    <p className="font-mono text-xs text-mist mb-2">{view.label}</p>
                    {view.progress && (
                      <div className="mt-4 space-y-3">
                        {(() => {
                          const p = view.progress;
                          const pct = Math.min(100, Math.round((p.processed / Math.max(1, p.total)) * 100));
                          let rem = p.estimatedRemaining;
                          if ((rem === undefined || rem === null || rem <= 0) && p.processed >= 8 && p.total > p.processed && p.elapsed > 0) {
                            const avg = Math.min(30, Math.max(p.elapsed / p.processed, 0.3)); // clamp 0.3–30s/symbol
                            rem = Math.min(6 * 3600, (p.total - p.processed) * avg);
                          }
                          return (
                            <>
                              <div className="flex justify-between items-end font-mono text-[11px]">
                                <div>
                                  <span className="text-mist/50">Processed </span>
                                  <span className="text-paper font-semibold">{p.processed}</span>
                                  <span className="text-mist/40"> / {p.total}</span>
                                </div>
                                <div className="text-right">
                                  <span className="text-signal-prepare text-sm font-semibold">{pct}%</span>
                                  <span className="text-mist/50 ml-2">⏱️ {formatTime(p.elapsed)}</span>
                                </div>
                              </div>
                              <div className="relative w-full h-2.5 bg-ink/80 border border-slate/50 rounded-full overflow-hidden">
                                <div
                                  className="h-full rounded-full bg-gradient-to-r from-signal-prepare/80 via-sky-400/70 to-signal-prepare transition-all duration-500 ease-out"
                                  style={{ width: `${pct}%` }}
                                />
                                <div
                                  className="absolute inset-0 opacity-30 pointer-events-none"
                                  style={{
                                    background:
                                      "linear-gradient(90deg, transparent, rgba(255,255,255,0.15), transparent)",
                                    backgroundSize: "200% 100%",
                                    animation: pct < 100 ? "stockky-shimmer 1.6s linear infinite" : "none",
                                  }}
                                />
                              </div>
                              <div className="flex justify-between font-mono text-[10px] text-mist/50">
                                <span>
                                  {scanTransport === "stream"
                                    ? "● NDJSON stream"
                                    : wsLive
                                      ? "● Live WS"
                                      : "○ HTTP poll"}
                                </span>
                                <span>
                                  {rem != null && rem > 0
                                    ? `Est. remaining ${formatTime(rem)}`
                                    : p.processed === 0
                                      ? "Calculating…"
                                      : p.processed >= p.total
                                        ? "Finishing…"
                                        : ""}
                                </span>
                              </div>
                            </>
                          );
                        })()}
                      </div>
                    )}
                    {(scanTaskId || scanTransport === "stream") && (
                      <button
                        onClick={handleStopScan}
                        disabled={stoppingScan || cancelRequested}
                        className="mt-4 font-mono text-xs text-signal-avoid border border-signal-avoid/40 rounded-lg px-3 py-2 hover:bg-signal-avoid/10 transition disabled:opacity-50 w-full"
                      >
                        {cancelRequested ? "Stopping — finishing up..." : "⏹ Stop Scan"}
                      </button>
                    )}
                    <div className="mt-6">
                      <Pipeline running={true} />
                    </div>
                  </div>

                  {/* Live results strip while streaming */}
                  {scanTransport === "stream" && liveScanRows.length > 0 && (
                    <div className="rounded-xl border border-slate/80 bg-graphite/80 p-4">
                      <div className="flex items-center justify-between mb-3">
                        <p className="font-mono text-[10px] uppercase tracking-widest text-mist/60">
                          Live results
                        </p>
                        <p className="font-mono text-[10px] text-mist/40">
                          last {Math.min(liveScanRows.length, 12)} of {liveScanRows.length}
                        </p>
                      </div>
                      <div className="space-y-1 max-h-64 overflow-y-auto pr-1">
                        {[...liveScanRows].slice(-12).reverse().map((r, i) => {
                          const d = String(r.decision || "?");
                          const color =
                            d.includes("BUY")
                              ? "text-signal-buy"
                              : d === "SELL"
                                ? "text-signal-sell"
                                : d === "ERROR"
                                  ? "text-signal-avoid"
                                  : "text-mist";
                          return (
                            <button
                              key={r.symbol || `row-${i}`}
                              type="button"
                              onClick={() => r.symbol && handleSearch(r.symbol)}
                              className="w-full flex items-center justify-between gap-2 rounded-lg px-2 py-1.5 hover:bg-ink/50 transition text-left"
                            >
                              <span className="font-mono text-xs text-paper truncate">{r.symbol}</span>
                              <span className={`font-mono text-[10px] shrink-0 ${color}`}>{d}</span>
                              <span className="font-mono text-[10px] text-mist/40 shrink-0 w-10 text-right">
                                {r.combined_score != null ? Math.round(Number(r.combined_score)) : "—"}
                              </span>
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}
                </div>
              )}


              {view.mode === "error" && (
                <div className="rounded-xl border border-signal-sell/40 bg-signal-sell/5 p-6">
                  <p className="font-mono text-xs text-signal-sell/70 uppercase tracking-widest mb-1">
                    Error
                  </p>
                  <p className="text-sm text-signal-sell break-words">{view.message}</p>
                  {/* BUG FIX: `&&` binds tighter than `||` in JS, so this was
                      `A || (B && <p>)` — when the message contains "timeout"
                      (A true) the whole expression evaluated to the boolean
                      `true` itself (React renders booleans as nothing),
                      silently swallowing this hint in exactly the most common
                      error case it's meant for. Only the "reach" branch ever
                      actually rendered the paragraph. Parenthesize the OR. */}
                  {(view.message.includes("timeout") || view.message.includes("reach")) && (
                    <p className="text-xs text-mist/60 mt-2">
                      ⏳ Free‑tier services may take up to 60 seconds to wake up on the first request.
                      Wait a moment and try again.
                    </p>
                  )}
                  <div className="flex gap-4 mt-4">
                    <button
                      onClick={handleRetry}
                      disabled={isRetrying}
                      className="font-mono text-xs text-paper bg-signal-prepare/20 border border-signal-prepare/40 rounded-lg px-4 py-1.5 hover:bg-signal-prepare/30 transition disabled:opacity-50"
                    >
                      {isRetrying ? "⏳ Restarting..." : (scanTaskId ? "Resume Scan" : "Try again")}
                    </button>
                    <button
                      onClick={openSettings}
                      className="font-mono text-xs text-mist hover:text-paper underline"
                    >
                      Check backend settings
                    </button>
                  </div>
                </div>
              )}

              {view.mode === "stock" && (
                <DecisionCard
                  data={view.data}
                  onBack={() => setView({ mode: "idle" })}
                  onSearchRelated={handleSearch}
                  onAddToWatchlist={handleAddToWatchlist}
                />
              )}

              {view.mode === "scan" && (
                <>
                  <BreadthStrip
                    mood={(view.data as any)?.market_mood}
                    advancing={(view.data as any)?.market_stats?.buy_signals}
                    declining={(view.data as any)?.market_stats?.sell_signals}
                  />
                  <SignalStream
                    rows={
                      ((view.data as any)?.recommendations ||
                        (view.data as any)?.all_results ||
                        []) as any[]
                    }
                    partial={Boolean((view.data as any)?.partial || (view.data as any)?.stopped_early)}
                    filter={signalFilter}
                    onFilter={setSignalFilter}
                    onSelect={(sym) => handleSearch(sym)}
                    title="Signal Stream & Audit"
                  />
                  <ScanPanel
                    result={view.data}
                    onSelect={handleSearch}
                    onBack={() => setView({ mode: "idle" })}
                    onAddToWatchlist={handleAddToWatchlist}
                    onAddManyToWatchlist={handleAddManyToWatchlist}
                    onSendTopPicks={handleSendTopPicks}
                    onSendAllActionable={handleSendAllActionable}
                  />
                </>
              )}
            </section>
          </>
  );
}
