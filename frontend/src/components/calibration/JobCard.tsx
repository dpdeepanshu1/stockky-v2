// group 442: one runnable job: what it does, when to run it, the last result, its options and a Run button.
import { useState } from "react";
import { activeRuns, downloadRunFile, fmtDur, istStamp, relTime, type JobDef } from "../../calibrationApi";
import { Btn, StatusPill } from "./ui";
import { useRunner } from "./RunnerContext";

export default function JobCard({ job, compact }: { job: JobDef; compact?: boolean }) {
  const { summary, start, openRun, cancel } = useRunner();
  const last = summary?.jobs?.[job.id] || null;
  // group 456: tell RUNNING from WAITING (both used to say "In queue") and keep the run's id so it can be watched / cancelled from here
  const running = activeRuns(summary?.state).find((r) => r.target === job.id) || null;      // E235: several runs may be going at once
  const queuedRec = running ? null : (summary?.state?.queued || []).find((q) => q.target === job.id) || null;
  const activeRec = running || queuedRec;
  const active = !!activeRec;
  const [vals, setVals] = useState<Record<string, any>>({});
  const [showOpts, setShowOpts] = useState(false);
  const params = () => Object.fromEntries((job.params || []).map((p) => [p.name, vals[p.name] ?? p.default]));

  return (
    <div className="rounded-xl border border-slate p-2.5" data-testid={`job-${job.id}`}>
      <div className="flex flex-wrap items-start justify-between gap-x-3 gap-y-2">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="font-bold">{job.title}</span>
            <StatusPill status={running ? "running" : queuedRec ? "queued" : last?.status || "never"} />
            {job.heavy && <span className="rounded border border-slate px-1 text-[9px] text-mist" title="Downloads or replays a lot: held back during market hours">HEAVY</span>}
            {job.window === "open" && <span className="rounded border border-slate px-1 text-[9px] text-mist">market hours</span>}
            {job.auto_elsewhere && <span className="rounded border border-slate px-1 text-[9px] text-mist" title="Also runs by GitHub Actions">also automatic</span>}
          </div>
          {!compact && <p className="mt-0.5 text-mist">{job.what}</p>}
          <p className="text-[11px]"><b>When:</b> {job.when}</p>
          {last && (
            <p className="text-[11px] text-mist">
              Last result: {relTime(last.finished_at)} <span title="IST">({istStamp(last.finished_at)})</span>{" "}
              <button type="button" className="underline" onClick={() => openRun(last.run_id)}>open</button>{" "}
              <button type="button" className="underline" onClick={() => void downloadRunFile(last.run_id, "output.txt").catch(() => undefined)}>txt</button>
              {last.headline?.[0] && <span className="block truncate" title={(last.headline || []).join("\n")}>→ {last.headline[0]}</span>}
            </p>
          )}
          {(job.needs?.length ?? 0) > 0 && !compact && <p className="text-[10px] text-mist">Needs first: {(job.needs || []).join(", ")}</p>}
          {!compact && <p className="text-[10px] text-mist">Time limit {fmtDur(job.timeout_s)}</p>}
        </div>
        <div className="run-actions">
          <Btn primary small disabled={active} onClick={() => void start("job", job.id, params())}>{running ? "Running…" : queuedRec ? "In queue" : "Run"}</Btn>
          {activeRec && <Btn small onClick={() => openRun(activeRec.id)} title="Open the live output">Watch</Btn>}
          {activeRec && <Btn small onClick={() => void cancel(activeRec.id)} title="Stop this run">Cancel</Btn>}
          {(job.params?.length ?? 0) > 0 && <button type="button" className="run-link text-[10px] text-mist underline" onClick={() => setShowOpts((o) => !o)}>{showOpts ? "hide options" : "options"}</button>}
        </div>
      </div>
      {activeRec && (
        <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-lg border border-signal-buy px-2 py-1 text-[11px]" role="status" data-testid={`job-live-${job.id}`}>
          <span className="font-bold text-signal-buy">{running ? "● Running now" : "◌ Waiting in the queue"}</span>
          <span className="text-mist">{running ? `started ${relTime(running.started_at)} · time limit ${fmtDur(job.timeout_s)}` : (queuedRec?.waiting_for ? `${queuedRec.waiting_for}` : "it starts when a slot is free")}</span>
          <button type="button" className="underline" onClick={() => openRun(activeRec.id)}>Watch live output</button>
        </div>
      )}
      {showOpts && (
        <div className="mt-2 rounded-lg border border-slate p-2">
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <span className="text-[10px] font-bold uppercase tracking-widest text-mist">Options</span>
            <button type="button" aria-label="Close options" title="Close options" onClick={() => setShowOpts(false)}
              className="shrink-0 rounded border border-slate px-1.5 text-[13px] leading-none text-mist hover:border-signal-sell hover:text-signal-sell">✕</button>
          </div>
          <div className="grid items-end gap-x-3 gap-y-2 sm:grid-cols-2">
            {(job.params || []).map((p) => (
              p.kind === "bool" ? (
                <label key={p.name} className="flex items-center gap-2 text-[11px]">
                  <input type="checkbox" checked={!!(vals[p.name] ?? p.default)} onChange={(e) => setVals((v) => ({ ...v, [p.name]: e.target.checked }))} />
                  <span>{p.label}{p.help ? <span className="text-mist"> · {p.help}</span> : null}</span>
                </label>
              ) : (
                <label key={p.name} className="text-[11px]">
                  <span className="mb-0.5 block text-mist">{p.label}{p.help ? ` · ${p.help}` : ""}</span>
                  {p.kind === "choice" ? (
                    <select value={vals[p.name] ?? p.default} onChange={(e) => setVals((v) => ({ ...v, [p.name]: e.target.value }))} className="w-full rounded-lg border border-slate bg-transparent px-2 py-1">
                      {p.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                    </select>
                  ) : (
                    <input type={p.kind === "int" ? "number" : "text"} min={p.min ?? undefined} max={p.max ?? undefined} value={vals[p.name] ?? p.default ?? ""}
                      onChange={(e) => setVals((v) => ({ ...v, [p.name]: p.kind === "int" ? Number(e.target.value) : e.target.value }))} className="w-full rounded-lg border border-slate bg-transparent px-2 py-1" />
                  )}
                </label>
              )
            ))}
          </div>
        </div>
      )}
      {job.after && !compact && <p className="mt-1.5 text-[11px] text-mist"><b>What to do with the result:</b> {job.after}</p>}
    </div>
  );
}
