// E122: the "Trade History" tab of Real Trade - the same kind of record list Position Stocks has, built from this service's own closed positions
// (GET /positions/{mode}/records). Read-only: no button here places, changes or closes a trade. Money numbers never animate; only the cards rise in.
import { Fragment, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { realTradeApi, type TradeRecord } from "../../../realTradeApi";
import { setVisibleInterval } from "../../../visibleInterval";
import { EmptyState } from "../../ui/EmptyState";
import { KpiStrip, type Kpi } from "../../ui/KpiStrip";
import { Spark } from "../../ui/Spark";
import { AiOrb } from "../../ui/AiOrb";
import { Skeleton } from "../../ui/motion";
import type { Mode } from "../types";
import {
  HISTORY_RANGES, daysFor, equityCurve, heldLabel, inRange, insights, newestFirst, pctLabel, pnlOf, priceLabel,
  reasonLabel, signedInr, summarize, toCsv, type HistoryRange,
} from "../history";

type Outcome = "all" | "wins" | "losses";
const OUTCOMES: { id: Outcome; label: string }[] = [{ id: "all", label: "All" }, { id: "wins", label: "Winners" }, { id: "losses", label: "Losers" }];
const tone = (n: number | null): "up" | "down" | "flat" => (n == null || Math.abs(n) < 0.5 ? "flat" : n > 0 ? "up" : "down");
const stamp = (r: TradeRecord) => [r.exit_day_ist, r.exit_time_ist].filter(Boolean).join(" · ") || "—";

export interface HistoryTabProps { mode: Mode; loggedIn: boolean }

export function HistoryTab({ mode, loggedIn }: HistoryTabProps) {
  const [range, setRange] = useState<HistoryRange>("7d");
  const [outcome, setOutcome] = useState<Outcome>("all");
  const [query, setQuery] = useState("");
  const [records, setRecords] = useState<TradeRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState<number | null>(null);
  const [updated, setUpdated] = useState<Date | null>(null);
  const seq = useRef(0);
  const needsLogin = mode === "REAL" && !loggedIn;

  const load = useCallback(async () => {
    if (needsLogin) return;
    const mine = ++seq.current;   // a slow answer for an old mode / range must not overwrite a newer one
    setLoading(true);
    try {
      const res = await realTradeApi.tradeRecords(mode, daysFor(range));
      if (mine !== seq.current) return;
      setRecords(Array.isArray(res?.records) ? res.records : []);
      setError(null);
      setUpdated(new Date());
    } catch (e: any) {
      if (mine !== seq.current) return;
      setError(e?.message || "Could not load the trade history");
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, [mode, range, needsLogin]);

  useEffect(() => { setRecords(null); setOpen(null); void load(); }, [load]);
  useEffect(() => setVisibleInterval(() => load(), 60_000), [load]);

  const scoped = useMemo(() => inRange(records ?? [], range), [records, range]);
  const summary = useMemo(() => summarize(scoped), [scoped]);
  const curve = useMemo(() => equityCurve(scoped), [scoped]);
  const notes = useMemo(() => insights(scoped), [scoped]);
  const rows = useMemo(() => {
    const q = query.trim().toUpperCase();
    return newestFirst(scoped).filter((r) => {
      if (q && !String(r.symbol).toUpperCase().includes(q)) return false;
      const p = pnlOf(r);
      if (outcome === "wins") return p != null && p > 0.5;
      if (outcome === "losses") return p != null && p < -0.5;
      return true;
    });
  }, [scoped, query, outcome]);

  const kpis: Kpi[] = [
    { label: "Net P&L", value: signedInr(summary.net), sub: `${summary.counted} trades with a known result`, tone: tone(summary.net) },
    { label: "Win rate", value: summary.winRate == null ? "—" : `${summary.winRate.toFixed(0)}%`, sub: `${summary.wins} won · ${summary.losses} lost${summary.breakeven ? ` · ${summary.breakeven} flat` : ""}`, tone: summary.winRate == null ? "flat" : summary.winRate >= 50 ? "up" : "down" },
    { label: "Best trade", value: signedInr(summary.best), sub: "largest single gain", tone: tone(summary.best) },
    { label: "Worst trade", value: signedInr(summary.worst), sub: "largest single loss", tone: tone(summary.worst) },
    { label: "Charges", value: summary.charges ? `₹${Math.round(summary.charges).toLocaleString("en-IN")}` : "—", sub: "brokerage + taxes", tone: "info" },
  ];

  const download = () => {
    try {
      const url = URL.createObjectURL(new Blob([toCsv(rows)], { type: "text/csv;charset=utf-8" }));
      const a = document.createElement("a");
      a.href = url; a.download = `trade-history-${mode}-${range}.csv`; a.click();
      URL.revokeObjectURL(url);
    } catch { /* download blocked: nothing else to do */ }
  };

  const winPct = summary.counted ? (summary.wins / summary.counted) * 100 : 0;
  const lossPct = summary.counted ? (summary.losses / summary.counted) * 100 : 0;

  return (
    <div className="v6-history v6-flow space-y-3">
      <div className="v6-toolbar">
        <div className="v6-seg" role="group" aria-label="Period">
          {HISTORY_RANGES.map((x) => (
            <button key={x.id} type="button" aria-pressed={range === x.id} onClick={() => setRange(x.id)}>{x.label}</button>
          ))}
        </div>
        <div className="v6-seg" role="group" aria-label="Outcome">
          {OUTCOMES.map((x) => (
            <button key={x.id} type="button" aria-pressed={outcome === x.id} onClick={() => setOutcome(x.id)}>{x.label}</button>
          ))}
        </div>
        <input type="search" className="v6-search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find a symbol" aria-label="Find a symbol" />
        <span className="v6-toolbar-gap" />
        {updated && <span className="v6-updated">Updated {updated.toLocaleTimeString("en-IN", { hour: "2-digit", minute: "2-digit", timeZone: "Asia/Kolkata" })}</span>}
        <button type="button" className="v6-btn" onClick={() => void load()} disabled={loading || needsLogin}>{loading ? "Loading…" : "↻ Refresh"}</button>
        <button type="button" className="v6-btn" onClick={download} disabled={!rows.length}>⤓ CSV</button>
      </div>

      {needsLogin ? (
        <EmptyState icon="lock" title="Log in to see your real trades" hint="Open the Overview tab and sign in. The history is read-only." />
      ) : error && records == null ? (
        <div className="rt-alert" role="alert"><span>{error}</span><button onClick={() => void load()} aria-label="Retry">↻</button></div>
      ) : records == null ? (
        <div className="v6-skels" aria-busy="true">
          <Skeleton h={78} /><Skeleton h={120} /><Skeleton h={54} /><Skeleton h={54} />
        </div>
      ) : (
        <>
          {error && <div className="rt-alert" role="alert"><span>{error} (showing the last good data)</span></div>}
          <KpiStrip items={kpis} label="Trade history summary" />

          {scoped.length === 0 ? (
            <EmptyState icon="trend" title={range === "today" ? "No closed trades today" : "No closed trades in this period"} hint="Closed positions appear here with their entry, exit, reason and charges." />
          ) : (
            <>
              <div className="v6-grid2">
                <section className="v6-card" aria-label="Equity curve">
                  <h3 className="v6-card-h">Running P&L</h3>
                  <p className="v6-card-note">Cumulative result in close order: {curve.length > 1 ? `starts ${signedInr(curve[0])}, ends ${signedInr(curve[curve.length - 1])}` : "needs two or more trades"}.</p>
                  {curve.length > 1 && <Spark points={curve} height={84} label={`Running P&L from ${signedInr(curve[0])} to ${signedInr(curve[curve.length - 1])} over ${curve.length} trades`} />}
                  <div className="v6-mixbar" role="img" aria-label={`${summary.wins} winners, ${summary.losses} losers`}>
                    <i className="w" style={{ width: `${winPct}%` }} /><i className="l" style={{ width: `${lossPct}%` }} />
                  </div>
                  <p className="v6-card-note">▲ {summary.wins} winners · ▼ {summary.losses} losers{summary.payoff != null ? ` · avg win ${summary.payoff.toFixed(2)}× avg loss` : ""}</p>
                </section>

                <section className="v6-card v6-ai" aria-label="Pattern read">
                  <div className="v6-ai-head"><AiOrb state="idle" size={34} /><div><h3 className="v6-card-h">Pattern read</h3><p className="v6-card-note">What already happened, in plain words. Not a forecast.</p></div></div>
                  {notes.length === 0 ? <p className="v6-card-note">Needs at least 3 closed trades with a known result.</p> : (
                    <ul className="v6-insights">
                      {notes.map((n) => <li key={n.title} className={n.tone}><b>{n.title}</b><span>{n.body}</span></li>)}
                    </ul>
                  )}
                </section>
              </div>

              <section className="v6-card v6-records" aria-label="Closed trades">
                <h3 className="v6-card-h">Closed trades <span className="v6-count">{rows.length}</span></h3>
                {rows.length === 0 ? <p className="v6-card-note">No trade matches this filter.</p> : (
                  <ul className="v6-rowlist">
                    {rows.map((r, i) => {
                      const p = pnlOf(r);
                      const t = tone(p);
                      const isOpen = open === r.position_id;
                      return (
                        <Fragment key={r.position_id}>
                          <li className={`v6-row ${t}`} style={{ ["--i" as string]: Math.min(i, 14) } as CSSProperties}>
                            <button type="button" className="v6-row-main" aria-expanded={isOpen} onClick={() => setOpen(isOpen ? null : r.position_id)}>
                              <span className="v6-row-sym"><b>{r.symbol}</b><small>{stamp(r)}</small></span>
                              <span className="v6-row-px">{priceLabel(r.entry_price)} → {priceLabel(r.exit_price)}<small>{r.quantity ?? "—"} qty · {heldLabel(r)}</small></span>
                              <span className="v6-row-why">{reasonLabel(r.exit_reason)}</span>
                              <span className={`v6-pnl ${t}`}>{t === "up" ? "▲ " : t === "down" ? "▼ " : ""}{signedInr(p)}<small>{pctLabel(r.return_pct)}</small></span>
                              <span className="v6-chev" aria-hidden="true">{isOpen ? "▴" : "▾"}</span>
                            </button>
                            {isOpen && (
                              <dl className="v6-detail">
                                <div><dt>Gross P&L</dt><dd>{signedInr(r.gross_pnl)}</dd></div>
                                <div><dt>Charges</dt><dd>{r.charges != null ? `₹${Math.round(r.charges).toLocaleString("en-IN")}` : "—"}</dd></div>
                                <div><dt>Net P&L</dt><dd>{signedInr(r.net_pnl)}</dd></div>
                                <div><dt>Entry slippage</dt><dd>{pctLabel(r.entry_slippage_pct)}</dd></div>
                                <div><dt>Best run</dt><dd>{pctLabel(r.max_gain_pct)}</dd></div>
                                <div><dt>Worst dip</dt><dd>{pctLabel(r.max_drawdown_pct)}</dd></div>
                                <div><dt>Stop distance</dt><dd>{r.stop_pct != null ? `${r.stop_pct.toFixed(2)}%` : "—"}</dd></div>
                                <div><dt>Watchlist tier</dt><dd>{r.tier_name ?? "not from a watchlist entry"}</dd></div>
                                <div><dt>Catalyst</dt><dd>{r.catalyst_type ?? "—"}</dd></div>
                                <div><dt>Source</dt><dd>{r.source_tab ?? "—"}{r.origin === "imported" ? " · imported from broker" : ""}</dd></div>
                              </dl>
                            )}
                          </li>
                        </Fragment>
                      );
                    })}
                  </ul>
                )}
              </section>
            </>
          )}
        </>
      )}
    </div>
  );
}
