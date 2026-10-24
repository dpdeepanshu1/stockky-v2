// E111: "Time Machine" tab on Position Stocks. Pick a stock, a side, an entry time, a stop % and a target %: it replays that rule on the stock's real
// recent 5-minute sessions AND on thousands of made-up sessions, and says how often the target comes first and what the trade earns in R after charges
// and slippage. EVIDENCE ONLY: it places, sizes and blocks nothing, and nothing reads its answer. Numbers and wording come from timeMachineView.ts
// (timeMachine.test.mjs); the maths is on the server (position-stocks-service timemachine/).
import { useEffect, useState, type ReactNode } from "react";
import { positionStocksApi } from "../positionStocksApi";
import { Card } from "./board/boardCard";
import "./board/board.css";
import {
  buildBody, controlLine, defaultForm, errorText, formProblem, gapLine, histBars, pct, rText, toneOfR, verdictView,
  type TmCompareDoc, type TmForm, type TmRunDoc, type TmStatusDoc,
} from "../timeMachineView";

const TONE: Record<string, string> = { good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold", mute: "text-mist" };
const INPUT = "w-full rounded-lg border border-slate bg-ink px-2 py-1 text-[12px] font-display tabular-nums text-paper";
const LABEL = "block text-[9px] font-display uppercase tracking-widest text-mist mb-0.5";

function Field({ label, children }: { label: string; children: ReactNode }) {
  return <label className="block"><span className={LABEL}>{label}</span>{children}</label>;
}
function Tile({ label, value, sub, tone }: { label: string; value: ReactNode; sub?: ReactNode; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate bg-ink px-2 py-1.5">
      <p className={LABEL}>{label}</p>
      <p className={`font-bold font-display tabular-nums text-[13px] ${tone ? TONE[tone] : "text-paper"}`}>{value}</p>
      {sub != null && <p className="text-[9px] text-mist tabular-nums">{sub}</p>}
    </div>
  );
}

export default function TimeMachinePanel({ active }: { active: boolean }) {
  const [status, setStatus] = useState<TmStatusDoc | null>(null);
  const [form, setForm] = useState<TmForm>(() => defaultForm(null));
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState<"" | "run" | "compare">("");
  const [err, setErr] = useState<string | null>(null);
  const [run, setRun] = useState<TmRunDoc | null>(null);
  const [cmp, setCmp] = useState<TmCompareDoc | null>(null);
  const [showDays, setShowDays] = useState(false);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    positionStocksApi.timeMachineStatus()
      .then((s: TmStatusDoc) => { if (alive) { setStatus(s); if (!touched) setForm((f) => ({ ...defaultForm(s), symbol: f.symbol, compareSymbols: f.compareSymbols })); } })
      .catch((e: any) => { if (alive) setErr(errorText(e)); });
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const set = <K extends keyof TmForm>(k: K, v: TmForm[K]) => { setTouched(true); setForm((f) => ({ ...f, [k]: v })); };
  const disabled = status?.enabled === false;

  const go = async (mode: "run" | "compare") => {
    const bad = formProblem(form, mode);
    if (bad) { setErr(bad); return; }
    setBusy(mode); setErr(null);
    try {
      if (mode === "run") { setRun(await positionStocksApi.timeMachineRun(buildBody(form, "run"))); setCmp(null); setShowDays(false); }
      else { setCmp(await positionStocksApi.timeMachineCompare(buildBody(form, "compare"))); setRun(null); }
    } catch (e: any) { setErr(errorText(e)); }
    finally { setBusy(""); }
  };

  const v = run ? verdictView(run.verdict.code) : null;
  const bars = run ? histBars(run.simulation.r_histogram, run.params.rr) : [];

  return (
    <div className="space-y-3" data-testid="time-machine">
      <Card title="Time Machine · odds of one trade" right={<span className="text-[9px] uppercase tracking-widest text-signal-hold">evidence only · places no order</span>}>
        <p className="text-[11px] text-mist mb-2">
          Pick a stock and a rule. It is walked over the stock&apos;s real recent 5-minute sessions and over thousands of made-up sessions that keep this stock&apos;s
          time-of-day volatility. A bar that touches both levels counts as the stop. A target at {form.stopPct && form.targetPct && Number(form.stopPct) > 0 ? (Number(form.targetPct) / Number(form.stopPct)).toFixed(1) : "?"}x the stop
          only needs to be reached about {Number(form.stopPct) > 0 && Number(form.targetPct) > 0 ? Math.round(100 / (1 + Number(form.targetPct) / Number(form.stopPct))) : "?"}% of the time to break even, before charges.
        </p>
        {disabled && <p className="text-[11px] text-signal-hold mb-2" data-testid="tm-disabled">The Time Machine is switched off (TIME_MACHINE_ENABLED on the Switches page).</p>}
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <Field label="Symbol"><input className={INPUT} value={form.symbol} placeholder="RELIANCE" onChange={(e) => set("symbol", e.target.value)} data-testid="tm-symbol" /></Field>
          <Field label="Side">
            <select className={INPUT} value={form.side} onChange={(e) => set("side", e.target.value as "LONG" | "SHORT")}>
              <option value="LONG">LONG (buy first)</option><option value="SHORT">SHORT (sell first)</option>
            </select>
          </Field>
          <Field label="Entry time (5-min mark)"><input className={INPUT} value={form.entryTime} placeholder="10:00" onChange={(e) => set("entryTime", e.target.value)} /></Field>
          <Field label="Flat by (blank = square-off)"><input className={INPUT} value={form.flatBy} placeholder={form.side === "SHORT" ? "14:55" : "15:00"} onChange={(e) => set("flatBy", e.target.value)} /></Field>
          <Field label="Stop %"><input className={INPUT} inputMode="decimal" value={form.stopPct} onChange={(e) => set("stopPct", e.target.value)} /></Field>
          <Field label="Target %"><input className={INPUT} inputMode="decimal" value={form.targetPct} onChange={(e) => set("targetPct", e.target.value)} /></Field>
          <Field label="Trade value (Rs, for charges)"><input className={INPUT} inputMode="numeric" value={form.valueRs} onChange={(e) => set("valueRs", e.target.value)} /></Field>
          <Field label="Made-up sessions">
            <input className={INPUT} inputMode="numeric" value={form.sims} onChange={(e) => set("sims", e.target.value)} />
          </Field>
          <Field label="Real history window">
            <select className={INPUT} value={form.lookback} onChange={(e) => set("lookback", e.target.value as "1mo" | "2mo")}>
              <option value="1mo">last month (~20 sessions)</option><option value="2mo">last 2 months (~40 sessions)</option>
            </select>
          </Field>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <button className="ob-chip" aria-pressed="true" disabled={!!busy || disabled} onClick={() => void go("run")} data-testid="tm-run">{busy === "run" ? "Running…" : "Run one stock"}</button>
          <span className="text-[10px] text-mist">or compare several (up to {status?.max_compare_symbols ?? 15}):</span>
          <input className={`${INPUT} !w-64`} value={form.compareSymbols} placeholder="TCS, INFY, SBIN" onChange={(e) => set("compareSymbols", e.target.value)} data-testid="tm-compare-symbols" />
          <button className="ob-chip" disabled={!!busy || disabled} onClick={() => void go("compare")} data-testid="tm-compare">{busy === "compare" ? "Comparing…" : "Compare"}</button>
        </div>
        {err && <p className="mt-2 text-[11px] text-signal-sell" data-testid="tm-error">{err}</p>}
      </Card>

      {run && v && (
        <>
          <Card title={`${run.symbol} · ${run.params.side} at ${run.params.entry_time}, stop ${run.params.stop_pct}% target ${run.params.target_pct}%`}
                right={<span className={`text-[10px] font-bold uppercase tracking-widest ${TONE[v.tone]}`} data-testid="tm-verdict">{v.label}</span>}>
            <p className="text-[12px]" data-testid="tm-verdict-line">{run.verdict.line}</p>
            <p className="mt-1 text-[11px] text-mist">{gapLine(run.history_vs_simulation_pts)}</p>
            {controlLine(run) && <p className="mt-1 text-[11px] text-mist" data-testid="tm-control">{controlLine(run)}</p>}
            <div className="mt-2 grid grid-cols-2 sm:grid-cols-4 gap-2">
              <Tile label="Simulated: target first" value={pct(run.simulation.p_target)} sub={`95% range ${pct(run.simulation.ci_low)} to ${pct(run.simulation.ci_high)} · ${run.simulation.n?.toLocaleString()} sessions`} />
              <Tile label="Needed to break even" value={pct(run.breakeven.win_net, 1)} sub={`${pct(run.breakeven.win_gross, 1)} before charges`} />
              <Tile label="Simulated average" value={rText(run.simulation.avg_r_net)} tone={toneOfR(run.simulation.avg_r_net)} sub={`${rText(run.simulation.avg_r_gross)} before charges`} />
              <Tile label="Real history average" value={rText(run.history.avg_r_net)} tone={toneOfR(run.history.avg_r_net, 0.1)} sub={run.history.n ? `${run.history.target} target · ${run.history.stop} stop · ${run.history.flat} flat of ${run.history.n}` : "no real history here"} />
              <Tile label="Stopped out (sim)" value={pct(run.simulation.p_stop)} />
              <Tile label="Still open at square-off (sim)" value={pct(run.simulation.p_flat)} sub={`closed at ${run.params.flat_by}`} />
              <Tile label="Spread of one trade (sim)" value={`${rText(run.simulation.r_spread.p05)} to ${rText(run.simulation.r_spread.p95)}`} sub={`middle half ${rText(run.simulation.r_spread.p25)} to ${rText(run.simulation.r_spread.p75)}`} />
              <Tile label="Charges + slippage" value={`${(run.charges_pct.target ?? 0).toFixed(2)}% to ${(run.charges_pct.stop ?? 0).toFixed(2)}%`} sub={`on Rs ${Math.round(run.params.value_rs).toLocaleString()} · slip ${run.params.slippage_pct}% per market leg`} />
            </div>
            {run.warnings.map((w, i) => <p key={i} className="mt-1 text-[11px] text-signal-hold" data-testid="tm-warning">{w}</p>)}
            <p className="mt-2 text-[10px] text-mist">
              {run.data.sessions} real sessions ({run.data.first_day} to {run.data.last_day}), last close {run.data.last_close}, {run.data.cached ? "cached data" : "fresh data"}, {run.elapsed_ms} ms.
            </p>
          </Card>

          <Card title="How one simulated trade ends (net R)">
            <div className="space-y-0.5" data-testid="tm-hist">
              {bars.map((b) => (
                <div key={b.label} className="flex items-center gap-2 text-[10px] tabular-nums">
                  <span className="w-24 text-mist shrink-0">{b.label}</span>
                  <div className="flex-1 h-2 rounded bg-ink">
                    <div className={`h-2 rounded ${b.kind === "stop" ? "bg-signal-sell" : b.kind === "target" ? "bg-signal-buy" : "bg-mist"}`} style={{ width: `${b.widthPct}%` }} />
                  </div>
                  <span className="w-12 text-right text-mist">{b.n}</span>
                </div>
              ))}
            </div>
          </Card>

          {run.by_entry_time.length > 0 && (
            <Card title="Same rule at other entry times">
              <div className="overflow-x-auto">
                <table className="w-full text-[11px] tabular-nums" data-testid="tm-table">
                  <thead><tr className="text-left text-[9px] uppercase tracking-widest text-mist"><th className="pr-3">Entry</th><th className="pr-3">Sim target first</th><th className="pr-3">Sim avg</th><th className="pr-3">History target first</th><th>History avg</th></tr></thead>
                  <tbody>
                    {run.by_entry_time.map((r) => (
                      <tr key={r.time} className={r.is_selected ? "font-bold text-paper" : "text-mist"}>
                        <td className="pr-3">{r.time}{r.is_selected ? " ◀" : ""}</td>
                        <td className="pr-3">{pct(r.sim_p_target)}</td>
                        <td className={`pr-3 ${TONE[toneOfR(r.sim_avg_r_net)]}`}>{rText(r.sim_avg_r_net)}</td>
                        <td className="pr-3">{r.hist_n ? `${pct(r.hist_p_target)} of ${r.hist_n}` : "-"}</td>
                        <td className={TONE[toneOfR(r.hist_avg_r_net, 0.1)]}>{rText(r.hist_avg_r_net)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="mt-1 text-[10px] text-mist">Neighbouring times on the same stock-day are not independent: look for a pattern across the table, not one best row.</p>
            </Card>
          )}

          <Card title="Real history, day by day" right={<button className="ob-chip" aria-pressed={showDays} onClick={() => setShowDays((x) => !x)}>{showDays ? "hide" : `show ${run.history_rows.length} days`}</button>}>
            {showDays ? (
              <div className="overflow-x-auto max-h-72 overflow-y-auto">
                <table className="w-full text-[11px] tabular-nums" data-testid="tm-days">
                  <thead><tr className="text-left text-[9px] uppercase tracking-widest text-mist"><th className="pr-3">Day</th><th className="pr-3">Result</th><th className="pr-3">Exit</th><th>Net R</th></tr></thead>
                  <tbody>
                    {[...run.history_rows].reverse().map((r) => (
                      <tr key={`${r.day}${r.entry}`}>
                        <td className="pr-3 text-mist">{r.day}</td>
                        <td className={`pr-3 ${r.outcome === "target" ? "text-signal-buy" : r.outcome === "stop" ? "text-signal-sell" : "text-signal-hold"}`}>{r.outcome === "flat" ? "open at square-off" : r.outcome}</td>
                        <td className="pr-3 text-mist">{r.exit}</td>
                        <td className={TONE[toneOfR(r.r_net, 0.1)]}>{rText(r.r_net)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <p className="text-[11px] text-mist">What really happened on each recent day with this exact rule.</p>}
          </Card>

          <Card title="Honest limits">
            <ul className="list-disc pl-4 space-y-0.5 text-[10px] text-mist">{run.limits.map((l, i) => <li key={i}>{l}</li>)}</ul>
          </Card>
        </>
      )}

      {cmp && (
        <Card title={`Compare · ${cmp.params.side} at ${cmp.params.entry_time}, stop ${cmp.params.stop_pct}% target ${cmp.params.target_pct}% · ${cmp.params.sims} sessions each`}>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] tabular-nums" data-testid="tm-compare-table">
              <thead><tr className="text-left text-[9px] uppercase tracking-widest text-mist"><th className="pr-3">Stock</th><th className="pr-3">Verdict</th><th className="pr-3">Sim target first</th><th className="pr-3">Sim avg</th><th className="pr-3">History</th><th className="pr-3">Mirror side avg</th><th>Window drift</th></tr></thead>
              <tbody>
                {cmp.rows.map((r) => {
                  const vv = verdictView(r.verdict);
                  return (
                    <tr key={r.symbol}>
                      <td className="pr-3 font-bold text-paper">{r.symbol}</td>
                      <td className={`pr-3 ${TONE[vv.tone]}`}>{vv.label}</td>
                      <td className="pr-3">{pct(r.sim_p_target)}</td>
                      <td className={`pr-3 ${TONE[toneOfR(r.sim_avg_r_net)]}`}>{rText(r.sim_avg_r_net)}</td>
                      <td className="pr-3 text-mist">{r.hist_n ? `${pct(r.hist_p_target)} of ${r.hist_n} · ${rText(r.hist_avg_r_net)}` : "-"}</td>
                      <td className="pr-3 text-mist">{rText(r.control_sim_avg_r_net)}</td>
                      <td className="text-mist">{r.window_drift_pct == null ? "-" : `${r.window_drift_pct >= 0 ? "+" : ""}${r.window_drift_pct.toFixed(2)}%`}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {cmp.failed.length > 0 && <p className="mt-2 text-[11px] text-signal-hold" data-testid="tm-failed">Could not load: {cmp.failed.map((f) => `${f.symbol} (${f.error})`).join("; ")}</p>}
          <p className="mt-1 text-[10px] text-mist">Ranked by the simulated average net R. A good rank on a rising window can be drift: read the mirror-side column beside it.</p>
        </Card>
      )}

      {status && status.recent.length > 0 && (
        <p className="text-[10px] text-mist">Recent runs: {status.recent.slice(0, 5).map((r) => `${r.symbol} ${r.side}`).join(", ")}</p>
      )}
    </div>
  );
}
