// E271: the "Order-flow tape" section of the Real Money Calibration page. READ-ONLY.
// Shows, for the selected service (Real Trade or Position Stocks): is the tape reliable and how much of the volume it covers (market-data), how many entry
// checks the tape gate refused / had no opinion on, and how many open trades the tape tightened (E270). Numbers are in-process counters since the last restart
// of that service. Wording and tones come from tapeView.ts (tape.test.mjs). The switches (FLOW_GATE_*, FLOW_EXIT_*) are on the Switches tab; the evidence
// report is the script scripts/flow_tape_report.py. Same fetch / poll / layout pattern as ReconcilePanel.
import { useCallback, useEffect, useState } from "react";
import type { Svc } from "../../calibrationApi";
import { positionStocksApi } from "../../positionStocksApi";
import { realTradeApi } from "../../realTradeApi";
import { tapeView, type FlowStatsDoc } from "../../tapeView";
import { setVisibleInterval } from "../../visibleInterval";
import TapeSymbolCard from "./TapeSymbolCard";
import { Btn, Card, Chip, Msg } from "./ui";

export default function TapePanel({ svc }: { svc: Svc }) {
  const [doc, setDoc] = useState<FlowStatsDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(async () => {
    try {
      setDoc(svc === "ps" ? await positionStocksApi.flowStats() : await realTradeApi.flowStats());
      setErr(null);
    } catch (e: any) { setDoc(null); setErr(`tape stats: ${e?.message || e}`); }
    setLoaded(true);
  }, [svc]);
  useEffect(() => { setLoaded(false); void load(); return setVisibleInterval(() => void load(), 30000); }, [load]);
  const v = tapeView(doc);
  const name = svc === "ps" ? "Position Stocks" : "Real Trade";
  return (
    <div className="space-y-3" data-testid="tape-panel">
      {err && <Msg kind="err">{err}</Msg>}
      <Card title={`Order-flow tape (${name})`} tone={v.tone === "bad" ? "bad" : v.tone === "warn" ? "warn" : v.tone === "good" ? "good" : undefined}
        right={<Chip title="This card only reads. It never changes a switch or a trade.">read-only</Chip>}>
        {!loaded && <p className="text-mist">Loading…</p>}
        {loaded && !doc && !err && <p className="text-mist">No numbers yet.</p>}
        {doc && <p className="text-[13px]" data-testid="tape-headline">{v.headline}</p>}
        {doc && (
          <div className="mt-2 flex flex-wrap items-center gap-2" data-testid="tape-health">
            <Chip tone={v.health.reliable === true ? "good" : v.health.reliable === false ? "warn" : undefined} title="At least one symbol has a tape that passes the reliability rule (enough intervals, minutes and volume coverage).">
              {v.health.reliable === true ? "reliable" : v.health.reliable === false ? "not reliable yet" : "reliability unknown"}</Chip>
            <Chip title="Share of the day's volume the tape saw trade by trade, averaged over today's symbols. The gate needs 35%.">coverage {v.health.coverage ?? "-"}%</Chip>
            <Chip>{v.health.symbols ?? "-"} symbols today</Chip>
          </div>
        )}
        {doc && (
          <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-2">
            <div className="rounded-lg border border-slate px-2 py-1 text-[12px]" data-testid="tape-entry">
              <div className="font-medium">Entry gate <span className="text-mist">mode {v.entry.mode}{doc.min_against != null ? ` · refuses at ${doc.min_against}+ adverse` : ""}</span></div>
              <div>{v.entry.blocks} refused of {v.entry.calls} checks{v.entry.blockPct != null ? ` (${v.entry.blockPct}%)` : ""}</div>
              <div className="text-mist">no usable tape {v.entry.noTape} · unreliable {v.entry.unreliable} · errors {v.entry.errors}</div>
            </div>
            <div className="rounded-lg border border-slate px-2 py-1 text-[12px]" data-testid="tape-exit">
              <div className="font-medium">Exit tightening <span className="text-mist">mode {v.exit.mode}{doc.exit_min_against != null ? ` · needs ${doc.exit_min_against}+ adverse` : ""}</span></div>
              <div>{v.exit.applied} trade{v.exit.applied === 1 ? "" : "s"} tightened · {v.exit.tighten} asked · {v.exit.hold} held</div>
              <div className="text-mist">{v.exit.calls} checks · errors {v.exit.errors}</div>
            </div>
          </div>
        )}
        {v.warnings.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-[12px]" data-testid="tape-warnings">{v.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
        <div className="mt-2 flex flex-wrap items-center gap-2"><Btn small title="Reads the numbers again." onClick={() => void load()}>Refresh</Btn></div>
        <p className="mt-2 text-[11px] text-mist">Counters start at zero when the service restarts. To see whether the cut-off is right, run <code>scripts/flow_tape_report.py</code> on the trade history; it needs trades that carry the entry tape, and with the gate on <b>live</b> the refused trades never exist, so set FLOW_GATE_MODE to <b>log</b> for a while to judge them. Switches: Switches tab (FLOW_GATE_*, FLOW_EXIT_*).</p>
      </Card>
      <TapeSymbolCard svc={svc} />
    </div>
  );
}
