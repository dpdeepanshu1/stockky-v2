// E144: the "Reconciliation" section of the Real Money Calibration page. READ-ONLY.
//  Real Trade tab: the hourly broker-vs-book drift report (GET /reconcile-report/REAL: the STORED report, never run=true). It changes nothing in the book.
//  Position Stocks tab: exits still waiting for their real fill price (GET /reconcile/pending).
// The actions (Reconcile now, holdings sync, Resolve stuck now) stay where they were: Auto Trade -> Positions, Position Stocks -> Positions / Pipeline, or the API.
// Wording and tones come from reconcileView.ts (reconcile.test.mjs). Same fetch / poll / layout pattern as ShortSellingPanel and SwingPanel.
import { useCallback, useEffect, useState } from "react";
import type { Svc } from "../../calibrationApi";
import { positionStocksApi, type PendingReconcileDoc } from "../../positionStocksApi";
import { realTradeApi, type ReconcileReportDoc } from "../../realTradeApi";
import { pendingView, reconcileView } from "../../reconcileView";
import { setVisibleInterval } from "../../visibleInterval";
import { Btn, Card, Chip, Msg } from "./ui";
import { DriftBeat } from "../ui/DriftBeat";   // design v7

function RealTradeDrift() {
  const [doc, setDoc] = useState<ReconcileReportDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(async () => {
    try { setDoc(await realTradeApi.reconcileReport()); setErr(null); } catch (e: any) { setErr(`hourly reconcile: ${e?.message || e}`); }
    setLoaded(true);
  }, []);
  useEffect(() => { void load(); return setVisibleInterval(() => void load(), 60000); }, [load]);
  const v = reconcileView(doc);
  return (
    <div className="space-y-3" data-testid="reconcile-rt">
      {err && <Msg kind="err">{err}</Msg>}
      <Card title="Hourly broker vs book check (REAL)" tone={v.tone === "bad" ? "bad" : v.tone === "warn" ? "warn" : v.tone === "good" ? "good" : undefined}
        right={<Chip title="This check only reads. It never changes the book.">read-only</Chip>}>
        {!loaded && <p className="text-mist">Loading…</p>}
        {loaded && !doc && !err && <p className="text-mist">No report yet.</p>}
        {doc && <DriftBeat kind={v.kind} warnCount={v.warns.length} />}
        {doc && <p className="text-[13px]" data-testid="reconcile-headline">{v.headline}</p>}
        {doc && <p className="mt-1 text-[11px] text-mist">Last pass: {v.ranAt}. It runs about once an hour and sends one Telegram message only when the set of warnings changes.</p>}
        {v.warns.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5" data-testid="reconcile-warns">{v.warns.map((t, i) => <li key={i}>{t}</li>)}</ul>}
        {v.infos.length > 0 && (
          <details className="mt-2 text-[12px]" data-testid="reconcile-infos">
            <summary className="cursor-pointer text-mist">{v.infos.length} note{v.infos.length === 1 ? "" : "s"} (not warnings)</summary>
            <ul className="mt-1 list-disc space-y-1 pl-5 text-mist">{v.infos.map((t, i) => <li key={i}>{t}</li>)}</ul>
          </details>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2"><Btn small title="Reads the stored report again. Does not start a new pass." onClick={() => void load()}>Refresh</Btn></div>
        <p className="mt-2 text-[11px] text-mist">To fix a mismatch use Auto Trade → Positions → Reconcile (or the holdings sync). The per-cycle sync already corrects "missing at broker" and "book over broker" on its own once a position is past the settlement guard.</p>
      </Card>
    </div>
  );
}

function PositionStocksPending() {
  const [doc, setDoc] = useState<PendingReconcileDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(async () => {
    try { setDoc(await positionStocksApi.reconcilePending()); setErr(null); } catch (e: any) { setErr(`pending fills: ${e?.message || e}`); }
    setLoaded(true);
  }, []);
  useEffect(() => { void load(); return setVisibleInterval(() => void load(), 30000); }, [load]);
  const v = pendingView(doc);
  return (
    <div className="space-y-3" data-testid="reconcile-ps">
      {err && <Msg kind="err">{err}</Msg>}
      <Card title="Exits waiting for their real fill (Position Stocks)" tone={v.tone === "warn" ? "warn" : v.tone === "good" ? "good" : undefined}
        right={<Chip title="This list only reads.">read-only</Chip>}>
        {!loaded && <p className="text-mist">Loading…</p>}
        {doc && <p className="text-[13px]" data-testid="pending-headline">{v.headline}</p>}
        {v.lines.length > 0 && (
          <div className="mt-2 overflow-x-auto" data-testid="pending-rows">
            <table className="w-full text-left text-[11px]">
              <thead className="text-mist"><tr><th className="pr-2">Stock</th><th className="pr-2">Status</th><th className="pr-2">Closed</th><th className="pr-2">Waiting</th><th className="pr-2">Resolves via</th><th className="text-right">P&amp;L so far</th></tr></thead>
              <tbody>
                {v.lines.map((l) => (
                  <tr key={l.id} className="border-t border-slate">
                    <td className="pr-2 font-bold">{l.symbol}</td><td className="pr-2">{l.status}</td><td className="pr-2">{l.closedDay}</td>
                    <td className={`pr-2 ${l.tone === "bad" ? "text-signal-sell" : l.tone === "warn" ? "text-signal-hold" : ""}`}>{l.age}</td>
                    <td className="pr-2 text-mist">{l.how}</td><td className="text-right">{l.pnl}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2"><Btn small title="Reads the list again." onClick={() => void load()}>Refresh</Btn></div>
        <p className="mt-2 text-[11px] text-mist">A row's P&amp;L is a placeholder until the real fill is read from Dhan. After 3 days unresolved the sweep marks it UNRESOLVED and alerts once. "Resolve stuck now" (POST /reconcile/pending/resolve) is not offered on this page.</p>
      </Card>
    </div>
  );
}

export default function ReconcilePanel({ svc }: { svc: Svc }) {
  if (svc === "ps") return <PositionStocksPending />;
  if (svc === "rt") return <RealTradeDrift />;
  return <Msg kind="info">Pick Real Trade or Position Stocks Service above.</Msg>;
}
