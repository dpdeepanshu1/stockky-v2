// E538 (redesign round 5b): the "screener" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import { positionStocksApi } from "../../positionStocksApi";
import StructureGateCard from "../StructureGateCard";
import { type Window, fmtDateTimeIst, CandidateList } from "./parts";
import type { PsState } from "./usePositionStocks";

export function PsScreenerTab({ s }: { s: PsState }) {
  const { status, candidates, windowFilter, setWindowFilter, candidateLogError, restrictedSymbols, restrictedSymbolsError, loadCandidateLog, loadRestrictedSymbols, filteredCandidates, grouped, filteredCandidateLog, qualityGateDropped } = s;
  return (
      <>
      {/* ── Live screener ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Live Screener</p>
          <div className="flex gap-1">
            {(["all", "1m", "5m", "15m", "60m"] as const).map(w => (
              <button key={w} onClick={() => setWindowFilter(w)}
                className={`px-2 py-1 rounded-lg font-display tabular-nums text-[10px] uppercase border ${
                  windowFilter === w ? "bg-signal-prepare/20 border-signal-prepare/40 text-signal-prepare" : "border-slate text-mist"
                }`}>{w}</button>
            ))}
          </div>
        </div>
        {windowFilter === "all" ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
            {(["1m", "5m", "15m", "60m"] as Window[]).map(w => (
              <div key={w}>
                <p className="text-[9px] text-mist uppercase tracking-widest mb-1">{w} window</p>
                <CandidateList rows={grouped[w]} />
              </div>
            ))}
          </div>
        ) : (
          <CandidateList rows={filteredCandidates} />
        )}
      </div>

      {/* ── Quality gate — recently dropped (session62, issue #5) ── */}
      {qualityGateDropped.length > 0 && (
        <div className="bg-graphite border border-signal-hold/40 rounded-2xl p-4">
          <div className="flex items-center justify-between mb-2">
            <p className="dash-section-title text-signal-hold">
              Quality Gate — {qualityGateDropped.length} recently dropped
            </p>
            <span className="font-display tabular-nums text-[9px] text-mist">
              Floor: Fund ≥ {status?.pipeline_config?.min_fundamental_score ?? 40}, Tech ≥{" "}
              {status?.pipeline_config?.min_technical_score ?? 40}, Mcap ≥ ₹
              {status?.pipeline_config?.min_market_cap_cr ?? 500}cr
            </span>
          </div>
          <p className="font-display tabular-nums text-[10px] text-mist mb-2">
            Candidates the price/volume screener ranked highly, but the fundamental/technical/market-cap floor
            rejected before capital was ever checked. If a symbol here looks like a genuine mover, MIN_FUNDAMENTAL_SCORE
            / MIN_TECHNICAL_SCORE / MIN_MARKET_CAP_CR are tunable env vars on this service (see docker-compose.yml) —
            no code change needed to loosen the floor.
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">%Chg</th>
                  <th className="py-1 pr-3">Fund</th>
                  <th className="py-1 pr-3">Tech</th>
                  <th className="py-1 pr-3">Mcap ₹cr</th>
                  <th className="py-1 pr-3">Why</th>
                  <th className="py-1">When</th>
                </tr>
              </thead>
              <tbody>
                {qualityGateDropped.slice(0, 20).map(c => (
                  <tr key={c.id} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{c.symbol}</td>
                    <td className={`py-1 pr-3 ${c.pct_change >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                      {c.pct_change >= 0 ? "+" : ""}{c.pct_change.toFixed(2)}%
                    </td>
                    <td className="py-1 pr-3 text-mist">{c.fundamental_score != null ? c.fundamental_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.technical_score != null ? c.technical_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.market_cap_cr != null ? c.market_cap_cr.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-signal-hold">{c.reason?.replace("QUALITY_GATE:", "") ?? "—"}</td>
                    <td className="py-1 text-mist">{fmtDateTimeIst(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* group355: how the daily-structure gate is behaving on the scalp entry (read-only) */}
      <StructureGateCard kind="position" title="Daily structure gate" load={(h) => positionStocksApi.structureStatus(h)} />

      {/* ── Candidate log (why entered/skipped) ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Candidate Log — Why Entered / Skipped</p>
          <button onClick={() => void loadCandidateLog()}
            className="px-3 py-1 rounded-lg bg-graphite border border-slate font-display tabular-nums text-[10px] text-mist">
            Refresh
          </button>
        </div>
        {candidateLogError ? (
          <p className="font-display tabular-nums text-[11px] text-signal-sell">{candidateLogError}</p>
        ) : filteredCandidateLog.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">
            {windowFilter === "all" ? "No candidates logged yet this session." : `No ${windowFilter} candidates logged yet this session.`}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Window</th>
                  <th className="py-1 pr-3">%Chg</th>
                  <th className="py-1 pr-3">Score</th>
                  <th className="py-1 pr-3">Decision</th>
                  <th className="py-1 pr-3">Reason</th>
                  <th className="py-1 pr-3">Fund</th>
                  <th className="py-1 pr-3">Tech</th>
                  <th className="py-1 pr-3">Mcap ₹cr</th>
                  <th className="py-1 pr-3">Catalyst</th>
                  <th className="py-1">When</th>
                </tr>
              </thead>
              <tbody>
                {filteredCandidateLog.map(c => (
                  <tr key={c.id} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{c.symbol}</td>
                    <td className="py-1 pr-3 text-mist">{c.window_source}</td>
                    <td className={`py-1 pr-3 ${c.pct_change >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>
                      {c.pct_change >= 0 ? "+" : ""}{c.pct_change.toFixed(2)}%
                    </td>
                    <td className="py-1 pr-3 text-mist">{c.composite_score != null ? c.composite_score.toFixed(1) : "—"}</td>
                    <td className={`py-1 pr-3 font-bold ${c.decision === "ENTERED" ? "text-signal-buy" : "text-signal-hold"}`}>{c.decision}</td>
                    <td className="py-1 pr-3 text-mist">{c.reason ?? "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.fundamental_score != null ? c.fundamental_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.technical_score != null ? c.technical_score.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.market_cap_cr != null ? c.market_cap_cr.toFixed(0) : "—"}</td>
                    <td className="py-1 pr-3 text-mist">{c.has_positive_catalyst == null ? "—" : c.has_positive_catalyst ? "Yes" : "No"}</td>
                    <td className="py-1 text-mist">{fmtDateTimeIst(c.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Full audit trail of every scanned candidate the quality gate looked at and why it was taken or skipped — not just the final screener output above.
        </p>
      </div>

      {/* ── Learned intraday-restricted symbols (T2T/ASM/GSM) ── */}
      <div className="bg-graphite border border-slate rounded-2xl p-4">
        <div className="flex items-center justify-between mb-3">
          <p className="dash-section-title">Intraday-Restricted Symbols</p>
          <button onClick={() => void loadRestrictedSymbols()}
            className="px-3 py-1 rounded-lg bg-graphite border border-slate font-display tabular-nums text-[10px] text-mist">
            Refresh
          </button>
        </div>
        {restrictedSymbolsError ? (
          <p className="font-display tabular-nums text-[11px] text-signal-sell">{restrictedSymbolsError}</p>
        ) : restrictedSymbols.length === 0 ? (
          <p className="font-display tabular-nums text-xs text-mist">No restricted symbols learned yet — none of Dhan's SELL rejections so far have been intraday-eligibility related.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] font-display tabular-nums">
              <thead>
                <tr className="text-mist text-left border-b border-slate">
                  <th className="py-1 pr-3">Symbol</th>
                  <th className="py-1 pr-3">Hits</th>
                  <th className="py-1 pr-3">First Seen</th>
                  <th className="py-1 pr-3">Last Seen</th>
                  <th className="py-1">Last Rejection Detail</th>
                </tr>
              </thead>
              <tbody>
                {restrictedSymbols.map(r => (
                  <tr key={r.symbol} className="border-b border-slate/50">
                    <td className="py-1 pr-3 text-paper font-bold">{r.symbol}</td>
                    <td className="py-1 pr-3 text-signal-sell">{r.hit_count}</td>
                    <td className="py-1 pr-3 text-mist">{fmtDateTimeIst(r.first_detected_at)}</td>
                    <td className="py-1 pr-3 text-mist">{fmtDateTimeIst(r.last_detected_at)}</td>
                    <td className="py-1 text-mist">{r.last_detail ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="font-display tabular-nums text-[9px] text-mist mt-2">
          Symbols Dhan has actually rejected as "not allowed to trade in Intraday" (T2T/ASM/GSM surveillance) — learned
          from live SELL rejections, not a static list. These are filtered out of every future cycle's candidates
          before capital or a Dhan call is committed to them.
        </p>
      </div>
      </>
      );
}
