// group 442: run history with download, and the run window (live output while it runs, steps, files, cancel).
import { useCallback, useEffect, useRef, useState } from "react";
import { ACTIVE, downloadRunFile, fmtDur, relTime, runnerApi, type RunRec } from "../../calibrationApi";
import { Btn, Card, Msg, StatusPill } from "./ui";
import { Modal } from "./SwitchesPanel";
import { useRunner } from "./RunnerContext";

export function RunsPanel() {
  const { summary, openRun, catalog } = useRunner();
  const [runs, setRuns] = useState<RunRec[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const load = useCallback(async () => {
    try { setRuns((await runnerApi.runs(80)).runs); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); }
  }, []);
  useEffect(() => { void load(); }, [load, summary?.state.current?.id, summary?.state.queued.length]);

  const shown = (runs || []).filter((r) => !filter || r.status === filter);
  return (
    <Card title="Runs and downloads" right={<>
      <select value={filter} onChange={(e) => setFilter(e.target.value)} aria-label="Filter by status" className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[11px]">
        <option value="">all</option>{["success", "attention", "failed", "timeout", "blocked", "cancelled", "running", "queued"].map((s) => <option key={s} value={s}>{s}</option>)}
      </select>
      <Btn small onClick={() => void load()}>Refresh</Btn></>}>
      {err && <Msg kind="err">{err}</Msg>}
      {runs && shown.length === 0 && <div className="text-mist">No runs yet. Start one from "Run plan" or "Tests & training".</div>}
      <ul className="divide-y divide-slate">
        {shown.map((r) => (
          <li key={r.id} className="flex flex-wrap items-center justify-between gap-2 py-1.5">
            <div className="min-w-0 flex-1">
              <button type="button" className="text-left font-bold underline-offset-2 hover:underline" onClick={() => openRun(r.id)}>{r.title}</button>{" "}
              <StatusPill status={r.status} />
              <div className="text-[10px] text-mist">{r.started_at ? r.started_at.slice(0, 16).replace("T", " ") : "queued"} IST · {r.trigger}{r.by ? ` · ${r.by}` : ""}{r.finished_at && r.started_at ? ` · ${fmtDur((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000)}` : ""}{catalog && r.kind === "pack" ? ` · ${r.jobs.length} jobs` : ""}</div>
              {r.headline[0] && <div className="truncate text-[11px] text-mist" title={r.headline.join("\n")}>→ {r.headline[0]}</div>}
              {r.status === "blocked" && <div className="text-[11px] text-signal-hold">{r.note}</div>}
            </div>
            <div className="flex gap-1.5">
              {!ACTIVE.includes(r.status) && r.status !== "blocked" && <Btn small onClick={() => void downloadRunFile(r.id, "output.txt").catch((e) => setErr(e?.message))}>txt</Btn>}
              {!ACTIVE.includes(r.status) && r.status !== "blocked" && <Btn small onClick={() => void downloadRunFile(r.id, "zip").catch((e) => setErr(e?.message))}>zip</Btn>}
            </div>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function RunWindow() {
  const { openRunId, openRun } = useRunner();
  const [run, setRun] = useState<RunRec | null>(null);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const offset = useRef(0);
  const box = useRef<HTMLPreElement | null>(null);
  const stick = useRef(true);

  useEffect(() => {
    setRun(null); setText(""); setErr(null); offset.current = 0; stick.current = true;
    if (!openRunId) return;
    let stop = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      if (stop || !openRunId) return;
      let live = true;
      try {
        const [r, t] = await Promise.all([runnerApi.run(openRunId), runnerApi.tail(openRunId, offset.current)]);
        if (stop) return;
        setRun(r);
        if (t.text) { offset.current = t.offset; setText((x) => x + t.text); }
        live = ACTIVE.includes(r.status) || t.offset < t.size;
        setErr(null);
      } catch (e: any) {
        if (!stop) setErr(e?.message || String(e));
      }
      if (!stop && live) timer = setTimeout(tick, 1500);
    };
    void tick();
    return () => { stop = true; if (timer) clearTimeout(timer); };
  }, [openRunId]);

  useEffect(() => { if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight; }, [text]);

  if (!openRunId) return null;
  const active = !!run && ACTIVE.includes(run.status);
  return (
    <Modal title={run?.title || "Run"} onClose={() => openRun(null)}>
      <div className="mb-2 flex flex-wrap items-center gap-2">
        {run && <StatusPill status={run.status} />}
        {run && <span className="text-[11px] text-mist">{run.id}</span>}
        <span className="ml-auto flex gap-1.5">
          {active && <Btn small onClick={() => void runnerApi.cancel(openRunId).catch((e) => setErr(e?.message))}>Cancel run</Btn>}
          <Btn small onClick={() => void downloadRunFile(openRunId, "output.txt").catch((e) => setErr(e?.message))}>Download txt</Btn>
          <Btn small onClick={() => void downloadRunFile(openRunId, "zip").catch((e) => setErr(e?.message))}>Zip</Btn>
        </span>
      </div>
      {err && <Msg kind="err">{err}</Msg>}
      {run?.headline?.length ? <div className="mb-2 rounded-lg border border-slate p-2 text-[11px]"><b>Headline lines</b>{run.headline.map((h, i) => <div key={i}>{h}</div>)}</div> : null}
      {run && run.jobs.length > 0 && <div className="mb-2 text-[11px] text-mist">{run.jobs.map((j) => `${j.title}: ${j.status}`).join(" · ")}</div>}
      <pre ref={box} onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 24; }} aria-label="Run output"
        className="h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-slate bg-black/30 p-2 text-[11px]">{text || (active ? "Waiting for output…" : "No output")}</pre>
      {run?.files && run.files.length > 1 && (
        <div className="mt-2 text-[11px]"><b>Files:</b>{" "}
          {run.files.map((f) => (
            <button key={f.name} type="button" className="mr-2 underline" onClick={() => void downloadRunFile(openRunId, f.name).catch((e) => setErr(e?.message))}>{f.name} ({Math.max(1, Math.round(f.bytes / 1024))} KB)</button>
          ))}
        </div>
      )}
      {run?.note && <p className="mt-2 text-[11px] text-signal-hold">{run.note}</p>}
      {run && <p className="mt-1 text-[10px] text-mist">Started {relTime(run.started_at)}.</p>}
    </Modal>
  );
}
