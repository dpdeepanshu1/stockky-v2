// group 442: one runnable job: what it does, when to run it, the last result, its options and a Run button.
import { useState } from "react";
import { downloadRunFile, fmtDur, relTime, type JobDef } from "../../calibrationApi";
import { Btn, StatusPill } from "./ui";
import { useRunner } from "./RunnerContext";

export default function JobCard({ job, compact }: { job: JobDef; compact?: boolean }) {
  const { summary, start, openRun } = useRunner();
  const last = summary?.jobs[job.id] || null;
  const active = summary?.state.current?.target === job.id || !!summary?.state.queued.some((q) => q.target === job.id);
  const [vals, setVals] = useState<Record<string, any>>({});
  const [showOpts, setShowOpts] = useState(false);
  const params = () => Object.fromEntries(job.params.map((p) => [p.name, vals[p.name] ?? p.default]));

  return (
    <div className="rounded-xl border border-slate p-2.5" data-testid={`job-${job.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-bold">{job.title}</span>
            <StatusPill status={active ? (summary?.state.current?.target === job.id ? "running" : "queued") : last?.status || "never"} />
            {job.heavy && <span className="rounded border border-slate px-1 text-[9px] text-mist" title="Downloads or replays a lot: held back during market hours">HEAVY</span>}
            {job.window === "open" && <span className="rounded border border-slate px-1 text-[9px] text-mist">market hours</span>}
            {job.auto_elsewhere && <span className="rounded border border-slate px-1 text-[9px] text-mist" title="Also runs by GitHub Actions">also automatic</span>}
          </div>
          {!compact && <p className="mt-0.5 text-mist">{job.what}</p>}
          <p className="text-[11px]"><b>When:</b> {job.when}</p>
          {last && (
            <p className="text-[11px] text-mist">
              Last: {relTime(last.finished_at)}{" "}
              <button type="button" className="underline" onClick={() => openRun(last.run_id)}>open</button>{" "}
              <button type="button" className="underline" onClick={() => void downloadRunFile(last.run_id, "output.txt").catch(() => undefined)}>txt</button>
              {last.headline[0] && <span className="block truncate" title={last.headline.join("\n")}>→ {last.headline[0]}</span>}
            </p>
          )}
          {job.needs.length > 0 && !compact && <p className="text-[10px] text-mist">Needs first: {job.needs.join(", ")}</p>}
          {!compact && <p className="text-[10px] text-mist">Time limit {fmtDur(job.timeout_s)}</p>}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <Btn primary small disabled={active} onClick={() => void start("job", job.id, params())}>{active ? "In queue" : "Run"}</Btn>
          {job.params.length > 0 && <button type="button" className="text-[10px] text-mist underline" onClick={() => setShowOpts((o) => !o)}>{showOpts ? "hide options" : "options"}</button>}
        </div>
      </div>
      {showOpts && (
        <div className="mt-2 grid gap-2 sm:grid-cols-2">
          {job.params.map((p) => (
            <label key={p.name} className="text-[11px]">
              <span className="block text-mist">{p.label}{p.help ? ` · ${p.help}` : ""}</span>
              {p.kind === "choice" ? (
                <select value={vals[p.name] ?? p.default} onChange={(e) => setVals((v) => ({ ...v, [p.name]: e.target.value }))} className="w-full rounded-lg border border-slate bg-transparent px-2 py-1">
                  {p.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              ) : p.kind === "bool" ? (
                <input type="checkbox" checked={!!(vals[p.name] ?? p.default)} onChange={(e) => setVals((v) => ({ ...v, [p.name]: e.target.checked }))} />
              ) : (
                <input type={p.kind === "int" ? "number" : "text"} min={p.min ?? undefined} max={p.max ?? undefined} value={vals[p.name] ?? p.default ?? ""}
                  onChange={(e) => setVals((v) => ({ ...v, [p.name]: p.kind === "int" ? Number(e.target.value) : e.target.value }))} className="w-full rounded-lg border border-slate bg-transparent px-2 py-1" />
              )}
            </label>
          ))}
        </div>
      )}
      {job.after && !compact && <p className="mt-1.5 text-[11px] text-mist"><b>What to do with the result:</b> {job.after}</p>}
    </div>
  );
}
