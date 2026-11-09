// F&O backtests: list of saved results with a reliability label, and buttons for the two runs the service can do itself (replay of recorded chains, simulation).
// The end-of-day backtest (real NSE bhavcopy) is a script: scripts/fno_eod_backtest.py.
import { useState } from "react";
import { fnoApi } from "../../fnoApi";
import { btLabel, inr } from "../../fnoView";
import { Btn, Card, Chip, Msg, pnlClass } from "../calibration/ui";
import { useFno } from "./useFno";

export default function FnoBacktests() {
  const b = useFno(fnoApi.backtests, 5000);
  const [und, setUnd] = useState<string[]>(["NIFTY"]);
  const [strategy, setStrategy] = useState("ALL");
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<{ name: string; doc: any } | null>(null);
  const run = async (kind: "replay" | "sim") => {
    setErr(null);
    try { await fnoApi.runBacktest(kind, und, {}, undefined, strategy); b.reload(); } catch (e: any) { setErr(String(e?.message || e)); }
  };
  const toggle = (u: string) => setUnd((cur) => (cur.includes(u) ? cur.filter((x) => x !== u) : [...cur, u]));
  const view = async (name: string) => { try { setOpen({ name, doc: await fnoApi.backtest(name) }); } catch (e: any) { setErr(String(e?.message || e)); } };
  const d = b.data;
  return (
    <div className="space-y-3">
      {(err || b.error) && <Msg kind="err">{err || b.error}</Msg>}
      <Card title="Run a backtest" right={<><Chip active={und.includes("NIFTY")} onClick={() => toggle("NIFTY")}>Nifty</Chip><Chip active={und.includes("SENSEX")} onClick={() => toggle("SENSEX")}>Sensex</Chip></>}>
        <div className="flex flex-wrap gap-2">
          <Btn disabled={!und.length || !d?.recorded_days.length} onClick={() => void run("replay")} title="Runs the recorded chain snapshots through the live engine">Replay recorded chains ({d?.recorded_days.length ?? 0} days)</Btn>
          <select className="rounded border border-slate bg-transparent px-2 text-[12px]" value={strategy} onChange={(e: any) => setStrategy(e.target.value)} title="Simulation only: ALL runs every strategy alone and lists them side by side">
            {["ALL", "CORE", "TREND_RIDER", "MOMENTUM_SURGE", "BEAR_SWEEP", "BEAR_PIVOT", "DRIFT_EDGE", "WEEKLY_OPENER"].map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
          <Btn disabled={!und.length} onClick={() => void run("sim")} title="Black-Scholes simulation on recent index candles">Quick simulation</Btn>
        </div>
        <p className="mt-2 text-[11px] text-mist">Replay is the most trustworthy but needs days of recorded chains (the engine records one snapshot a minute while the market is open). The simulation uses model prices, not real quotes. The end-of-day test on real NSE prices and the test on Dhan's recorded option candles run as scripts: <code>python scripts/fno_eod_backtest.py</code>, <code>python scripts/fno_rolling_backtest.py</code>.</p>
        {d?.jobs.slice(-3).map((j) => <p key={j.id} className="mt-1 text-[11px]"><Chip tone={j.state === "done" ? "good" : j.state === "failed" ? "bad" : "warn"}>{j.state}</Chip> {j.id} {j.verdict || ""} {j.error || ""}</p>)}
      </Card>
      <Card title="Results">
        {!d?.results.length && <p className="text-mist">No backtest has been saved yet.</p>}
        <ul className="space-y-1">
          {d?.results.map((r) => {
            const lab = btLabel(r.kind);
            return (
              <li key={r.name} className="flex flex-wrap items-center gap-2">
                <Chip tone={lab.label === "RECORDED" || lab.label === "REAL PREMIUMS" ? "good" : lab.label === "SIMULATED" ? "warn" : undefined} title={lab.text}>{lab.label}</Chip>
                <button type="button" className="underline" onClick={() => void view(r.name)}>{r.name}</button>
                <span className="text-mist">{r.underlying} · {r.from}..{r.to} · {r.trades ?? 0} trades · win {r.win_rate_pct ?? "-"}%</span>
                <b className={pnlClass(r.net)}>{inr(r.net)}</b><Chip>{r.verdict || "-"}</Chip>
              </li>
            );
          })}
        </ul>
      </Card>
      {open && (
        <Card title={open.name} right={<Btn small onClick={() => setOpen(null)}>Close</Btn>}>
          <p className="mb-1 text-mist">{btLabel(open.doc.kind).text}</p>
          <ul className="mb-2 list-disc pl-4">{(open.doc.notes || []).map((n: string, i: number) => <li key={i}>{n}</li>)}</ul>
          {Array.isArray(open.doc.by_strategy) && (
            <table className="mb-2 w-full text-left text-[11px]">
              <thead className="text-mist"><tr><th>Strategy</th><th>Trades</th><th>Win %</th><th>Net</th><th>Profit factor</th><th>Max DD</th><th>Verdict</th></tr></thead>
              <tbody>{open.doc.by_strategy.map((x: any) => (
                <tr key={x.strategy}><td>{x.strategy}</td><td>{x.trades}</td><td>{x.win_rate_pct}</td><td className={pnlClass(x.net)}>{inr(x.net)}</td><td>{x.profit_factor ?? "-"}</td><td>{inr(x.max_drawdown)}</td><td>{x.verdict}</td></tr>
              ))}</tbody>
            </table>
          )}
          <pre className="max-h-96 overflow-auto whitespace-pre-wrap text-[11px]">{JSON.stringify(open.doc.metrics?.overall ?? open.doc.metrics, null, 2)}</pre>
        </Card>
      )}
    </div>
  );
}
