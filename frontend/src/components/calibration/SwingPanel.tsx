// E144: the "Swing lane" section of the Real Money Calibration page. READ-ONLY: it shows what GET /swing/status and GET /swing/ledger say about the swing lane
// (real-trade-service) and nothing else. In shadow mode every row is a trade the lane WOULD have taken; no order exists. The switches (SWING_*) are on the
// Switches tab (Real Trade) and the jobs on the Tests & training tab. All wording and numbers come from swingView.ts (covered by swing.test.mjs).
// Same fetch / poll / layout pattern as ShortSellingPanel: load on mount, every 30 s while the browser tab is visible (setVisibleInterval), errors shown in a Msg.
import { useCallback, useEffect, useState } from "react";
import { realTradeApi, type SwingLedgerDoc, type SwingStatusDoc } from "../../realTradeApi";
import { ledgerLines, swingView } from "../../swingView";
import { setVisibleInterval } from "../../visibleInterval";
import { useRunner } from "./RunnerContext";
import { Btn, Card, Chip, Msg } from "./ui";
import { ModeLadder } from "../ui/ModeLadder";   // design v7
import { EvidenceMeter } from "../ui/EvidenceMeter";   // design v7

export default function SwingPanel() {
  const { start } = useRunner();
  const [doc, setDoc] = useState<SwingStatusDoc | null>(null);
  const [ledger, setLedger] = useState<SwingLedgerDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    const errs: string[] = [];
    try { setDoc(await realTradeApi.swingStatus()); } catch (e: any) { errs.push(`swing status: ${e?.message || e}`); }
    try { setLedger(await realTradeApi.swingLedger()); } catch (e: any) { errs.push(`swing ledger: ${e?.message || e}`); }
    setErr(errs.length ? errs.join(" · ") : null);
    setLoaded(true);
  }, []);
  useEffect(() => { void load(); return setVisibleInterval(() => void load(), 30000); }, [load]);

  const v = swingView(doc);
  const lines = ledgerLines(ledger?.rows);
  const cardTone = v.kind === "live" ? "bad" : v.kind === "unavailable" || v.problems.length > 0 ? "warn" : undefined;

  return (
    <div className="space-y-3" data-testid="swing-lane">
      {err && <Msg kind="err">{err}</Msg>}
      <Card title={v.kind === "live" ? "Swing lane (LIVE orders possible)" : "Swing lane (shadow: no order is placed)"} tone={cardTone}
        right={<Chip tone={v.kind === "live" ? "bad" : "warn"} title={v.kind === "live" ? "Real CNC swing orders can be placed by the schedule loop." : "Every row is a trade the lane WOULD have taken."}>{v.kind === "live" ? "real orders" : v.kind === "off" ? "off" : "no order is placed"}</Chip>}>
        {!loaded && <p className="text-mist">Loading…</p>}
        {v.kind !== "unavailable" && <ModeLadder mode={v.kind} lane="Swing lane" />}
        <p className="text-[13px]" data-testid="swing-headline">{v.headline}</p>
        <div className="mt-2 text-[12px]" data-testid="swing-verdict">
          <Chip tone={v.verdict.tone}>{v.verdict.code}</Chip> {v.verdict.text}
        </div>
        {v.kind !== "unavailable" && <EvidenceMeter done={v.progress.done} needed={v.progress.needed} days={v.progress.days} daysNeeded={v.progress.daysNeeded} />}
        <div className="mt-2 text-[12px]" data-testid="swing-progress">
          Evidence: {v.progress.done} of {v.progress.needed} closed shadow trades, {v.progress.days} of {v.progress.daysNeeded} entry days ({v.progress.pct}%).
        </div>
        {v.numbers.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2" data-testid="swing-numbers">
            {v.numbers.map((n) => <Chip key={n.label} title={n.label}>{n.label} {n.value}</Chip>)}
          </div>
        )}
        {v.statusCounts.length > 0 && (
          <div className="mt-2 flex flex-wrap items-center gap-2" data-testid="swing-status-counts">
            {v.statusCounts.map((s) => <Chip key={s.status}>{s.count} {s.status.toLowerCase()}</Chip>)}
          </div>
        )}
        <div className="mt-2 text-[12px]" data-testid="swing-scan"><Chip tone={v.scan.tone}>scan</Chip> {v.scan.text}</div>
        {v.pauses.map((p) => <div key={p.mode} className="mt-2"><Msg kind="warn">{p.mode} swing entries are paused: {p.why}. Resume with POST /swing/resume (not offered on this page).</Msg></div>)}
      </Card>

      {v.problems.length > 0 && (
        <Card title="Wiring: what would stop the lane" tone="warn">
          <ul className="list-disc space-y-1 pl-5" data-testid="swing-problems">{v.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
        </Card>
      )}

      <Card title={`Open swing positions (${v.openPositions.length})`}>
        {v.openPositions.length === 0 ? <p className="text-mist">None. Shadow mode never opens one.</p> : (
          <div className="space-y-1" data-testid="swing-open">
            {v.openPositions.map((p) => (
              <div key={`${p.mode}-${p.symbol}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate px-2 py-1">
                <span><b>{p.symbol}</b> <span className="text-[11px] text-mist">{p.mode}</span></span>
                <span>{p.qty} shares · entry {p.entry} · stop {p.stop}</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title={`Tomorrow's planned picks (${v.planned.length})`}>
        {v.planned.length === 0 ? <p className="text-mist">None planned. The evening scan adds them after the scan time on a trading day.</p> : (
          <div className="space-y-1" data-testid="swing-planned">
            {v.planned.map((p) => (
              <div key={`${p.symbol}-${p.signalDay}`} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate px-2 py-1">
                <span><b>{p.symbol}</b> <span className="text-[11px] text-mist">rank {p.rank}</span></span>
                <span>signal {p.signalDay} · first stop {p.stopPct} below entry</span>
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card title={`Latest shadow ledger rows (${lines.length}${ledger && ledger.count > lines.length ? ` of ${ledger.count}` : ""})`}>
        {lines.length === 0 ? <p className="text-mist">No shadow rows yet.</p> : (
          <div className="overflow-x-auto" data-testid="swing-ledger">
            <table className="w-full text-left text-[11px]">
              <thead className="text-mist"><tr><th className="pr-2">Stock</th><th className="pr-2">Status</th><th className="pr-2">Signal</th><th className="pr-2">Entry</th><th className="pr-2">Exit</th><th className="pr-2">Why</th><th className="text-right">Net</th></tr></thead>
              <tbody>
                {lines.map((l) => (
                  <tr key={l.id} className="border-t border-slate">
                    <td className="pr-2 font-bold">{l.symbol}</td><td className="pr-2">{l.status}</td><td className="pr-2">{l.signalDay}</td><td className="pr-2">{l.entryDay}</td><td className="pr-2">{l.exitDay}</td>
                    <td className="pr-2 text-mist">{l.why}</td>
                    <td className={`text-right ${l.netTone === "good" ? "text-signal-buy" : l.netTone === "bad" ? "text-signal-sell" : "text-mist"}`}>{l.net}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Btn small title="Reads the shadow ledger again now." onClick={() => void load()}>Refresh</Btn>
          <Btn small title="Runs the Friday shadow report job and keeps its txt output. Changes nothing." onClick={() => void start("job", "swing_shadow_report", {})}>Run shadow report</Btn>
        </div>
        {doc?.shadow?.note && <p className="mt-2 text-[11px] text-mist">{doc.shadow.note}</p>}
        <p className="mt-2 text-[11px] text-mist">Read-only. SWING_MODE and the other SWING_ switches are on the Switches tab (Real Trade); the evening scan and the S4 stop probe are started from the Tests &amp; training tab.</p>
      </Card>
    </div>
  );
}
