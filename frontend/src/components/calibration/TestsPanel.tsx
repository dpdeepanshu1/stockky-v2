// group 442: every test / calibration / training job the runner knows, by group, each with options and a Run button.
import { useMemo, useState } from "react";
import { runnerApi, saveBlob, type Svc } from "../../calibrationApi";
import { jobAppliesTo } from "../../calibrationPlan";
import { Btn, Card, Msg } from "./ui";
import JobCard from "./JobCard";
import { useRunner } from "./RunnerContext";

export default function TestsPanel({ svc }: { svc: Svc }) {
  const { catalog } = useRunner();
  const [q, setQ] = useState("");
  const [all, setAll] = useState(false);
  const [sum, setSum] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const groups = useMemo(() => {
    if (!catalog) return [];
    const ql = q.trim().toLowerCase();
    return catalog.groups.map((g) => ({
      ...g,
      jobs: catalog.jobs.filter((j) => j.group === g.id && (all || jobAppliesTo(j.id, svc)) && (!ql || `${j.title} ${j.what} ${j.id}`.toLowerCase().includes(ql))),
    })).filter((g) => g.jobs.length);
  }, [catalog, q, all, svc]);

  if (!catalog) return <div className="text-mist">Loading jobs…</div>;
  const dp = catalog.data_present;
  const showSummary = async () => {
    try { setSum(await runnerApi.calibrationSummary()); setErr(null); } catch (e: any) { setSum(null); setErr(e?.message || String(e)); }
  };

  return (
    <div className="space-y-3" data-testid={`tests-${svc}`}>
      <Card title="Data on the runner" right={<Btn small onClick={() => void showSummary()} disabled={!dp.has_summary}>Show last calibration summary</Btn>}>
        <div className="flex flex-wrap gap-2 text-[11px]">
          <span className="rounded-lg border border-slate px-2 py-0.5">history folders: <b>{dp.history_folders.length ? dp.history_folders.join(", ") : "none yet"}</b></span>
          <span className="rounded-lg border border-slate px-2 py-0.5">saved record exports: <b>{dp.exports.length}</b></span>
          <span className="rounded-lg border border-slate px-2 py-0.5">recorded bar files: <b>{dp.bar_files}</b></span>
        </div>
        {dp.history_folders.length === 0 && <p className="mt-2 text-mist">The backtests need daily history. Run <b>Download daily history</b> or the <b>FULL calibration</b> first (market closed).</p>}
        {err && <div className="mt-2"><Msg kind="info">{err}</Msg></div>}
        {sum && <>
          <pre className="mt-2 max-h-80 overflow-auto rounded-lg border border-slate p-2 text-[11px] whitespace-pre-wrap">{sum}</pre>
          <Btn small onClick={() => saveBlob(new Blob([sum], { type: "text/plain" }), "calibration_summary.txt")}>Download txt</Btn>
        </>}
      </Card>
      <div className="flex flex-wrap items-center gap-2">
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search jobs…" aria-label="Search jobs" className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[12px]" />
        <label className="flex items-center gap-1 text-[11px] text-mist"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> also show jobs of the other service</label>
      </div>
      {groups.map((g) => (
        <Card key={g.id} title={g.title}>
          <div className="space-y-2">{g.jobs.map((j) => <JobCard key={j.id} job={j} />)}</div>
        </Card>
      ))}
    </div>
  );
}
