// group 471: "Run all test cases" for the Switches / Daily trades / Run plan / Tests tabs: every test of the selected service, one after the other.
import { useMemo, useState } from "react";
import { planRunAll } from "../../calibrationPlan";
import { downloadLatestResults, type Svc } from "../../calibrationApi";
import { Btn, Msg } from "./ui";
import { Modal } from "./SwitchesPanel";
import { useRunner } from "./RunnerContext";

export default function RunAllBar({ svc }: { svc: Svc }) {
  const { catalog, runAll, startRunAll, stopRunAll, openRun, summary } = useRunner();
  const [ask, setAsk] = useState(false);
  const [heavy, setHeavy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const plan = useMemo(() => planRunAll(catalog?.jobs || [], svc, heavy), [catalog, svc, heavy]);
  const title = (id: string) => (catalog?.jobs || []).find((j) => j.id === id)?.title || id;
  const c = runAll.counts;
  const tally = `OK ${c.success || 0} · LOOK ${c.attention || 0} · FAILED ${(c.failed || 0) + (c.timeout || 0)}${c.cancelled ? ` · cancelled ${c.cancelled}` : ""}`;

  return (
    <div className="flex flex-wrap items-center gap-2 rounded-2xl border border-slate bg-graphite px-3 py-2 text-[12px]" data-testid="run-all-bar">
      <Btn primary disabled={!catalog || runAll.running} onClick={() => setAsk(true)} title="Starts every test of this service, several at a time">
        {runAll.running ? "Running all…" : "Run all test cases"}
      </Btn>
      {runAll.running && (<>
        <span className="text-[11px]" role="status">Started {runAll.index} of {runAll.total}: <b>{runAll.currentTitle || "waiting"}</b> · {tally}</span>
        {runAll.currentRunId && <Btn small onClick={() => openRun(runAll.currentRunId)}>Watch</Btn>}
        <Btn small onClick={stopRunAll} title="Cancels every test that is running now and starts no more">Stop</Btn>
        <span className="w-full text-[10px] text-mist">Keep this page open: the next test is started from here when a slot frees up.</span>
      </>)}
      {!runAll.running && runAll.finished && (
        <span className="text-[11px]" role="status">{runAll.stopped ? "Stopped" : "Finished"}: {tally}{runAll.skipped.length ? ` · skipped ${runAll.skipped.length}` : ""}</span>
      )}
      {!runAll.running && runAll.finished && <Btn small onClick={() => void downloadLatestResults(svc).catch((e) => setErr(e?.message || String(e)))}>Download all latest results</Btn>}
      {err && <div className="w-full"><Msg kind="err">{err}</Msg></div>}
      {!runAll.running && runAll.skipped.length > 0 && (
        <ul className="w-full list-disc pl-5 text-[11px] text-mist">{runAll.skipped.map((s, i) => <li key={i}><b>{s.title}</b>: {s.why}</li>)}</ul>
      )}
      {ask && (
        <Modal title="Run all test cases?" onClose={() => setAsk(false)}>
          <p>{plan.ids.length} test{plan.ids.length === 1 ? "" : "s"} for this service, started <b>in order, several at a time</b> ({summary?.state?.workers ?? 3} at a time; a test that needs another test's data waits for it). Nothing here changes a trading switch.</p>
          <label className="mt-2 flex items-start gap-2 text-[11px]">
            <input type="checkbox" checked={heavy} onChange={(e) => setHeavy(e.target.checked)} className="mt-0.5" />
            <span>Also the long tests (history download, A/B replay, backtests: can take hours). They are held back in market hours and listed as skipped.{!heavy && plan.heavySkipped.length ? ` Left out now: ${plan.heavySkipped.length}.` : ""}</span>
          </label>
          <p className="mt-2 text-[11px] text-mist">Never included: retrain, score outcomes and structure refresh (they change data, they are not tests). A test that needs data from another test shows LOOK until that one has run.</p>
          <ol className="mt-2 max-h-48 list-decimal overflow-auto pl-5 text-[11px]">{plan.ids.map((id) => <li key={id}>{title(id)}</li>)}</ol>
          <div className="mt-3 flex gap-2">
            <Btn primary disabled={plan.ids.length === 0} onClick={() => { setAsk(false); void startRunAll(plan.ids); }}>Run {plan.ids.length} test{plan.ids.length === 1 ? "" : "s"}</Btn>
            <Btn onClick={() => setAsk(false)}>Cancel</Btn>
          </div>
        </Modal>
      )}
    </div>
  );
}
