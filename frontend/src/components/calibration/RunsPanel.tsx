// group 442: run history with download, and the run window (live output while it runs, steps, files, cancel).
// group 447: only the LATEST result of each test / pack is kept (a new run replaces the old one), filter chips with counts, search, close icon on the run window.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ACTIVE, ApiError, downloadRunFile, fmtDur, istStamp, relTime, runnerApi, statusCounts, telegramNote, type RunRec } from "../../calibrationApi";
import { Btn, Card, Chip, Msg, StatusPill } from "./ui";
import { Modal } from "./SwitchesPanel";
import { useRunner } from "./RunnerContext";

const BORDER: Record<string, string> = {
  success: "border-l-signal-buy", attention: "border-l-signal-hold", failed: "border-l-signal-sell", timeout: "border-l-signal-sell",
  blocked: "border-l-signal-hold", running: "border-l-signal-buy", cancelled: "border-l-slate", queued: "border-l-slate",
};
const TONE: Record<string, "good" | "warn" | "bad" | undefined> = { success: "good", attention: "warn", blocked: "warn", failed: "bad", timeout: "bad", running: "good" };
const ORDER = ["running", "queued", "failed", "timeout", "attention", "blocked", "success", "cancelled"];

export function RunsPanel() {
  const { summary, openRun } = useRunner();
  const [runs, setRuns] = useState<RunRec[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");
  const [autoOnly, setAutoOnly] = useState(false);
  const load = useCallback(async () => {
    try { setRuns((await runnerApi.runs(80)).runs || []); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); }
  }, []);
  useEffect(() => { void load(); }, [load, summary?.state?.current?.id, summary?.state?.queued?.length, summary?.state?.last_automatic?.id]);

  const counts = useMemo(() => statusCounts(runs || []), [runs]);
  const shown = useMemo(() => {
    const ql = q.trim().toLowerCase();
    return (runs || []).filter((r) => (!filter || r.status === filter) && (!autoOnly || r.trigger === "schedule") && (!ql || `${r.title} ${r.target}`.toLowerCase().includes(ql)));
  }, [runs, filter, q, autoOnly]);
  const latestOnly = summary?.state?.keep_latest_only;
  const dl = (id: string, name: string) => void downloadRunFile(id, name).catch((e) => setErr(e?.message));

  return (
    <Card title="Runs and downloads" right={<Btn small onClick={() => void load()}>Refresh</Btn>}>
      {latestOnly && <p className="mb-2 text-[11px] text-mist" data-testid="latest-only-note">One line per test and settings: a new result replaces the older one of the same test with the same options, so the disk does not fill up. Download what you need before you run the same test again.</p>}
      {err && <Msg kind="err">{err}</Msg>}
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        <Chip active={!filter} onClick={() => setFilter("")}>all {runs ? runs.length : ""}</Chip>
        {ORDER.filter((s) => counts[s]).map((s) => <Chip key={s} tone={TONE[s]} active={filter === s} onClick={() => setFilter(filter === s ? "" : s)}>{s} {counts[s]}</Chip>)}
        <Chip active={autoOnly} onClick={() => setAutoOnly((a) => !a)} title="Only runs the timetable started">automatic only</Chip>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search runs…" aria-label="Search runs" className="ml-auto w-40 rounded-lg border border-slate bg-transparent px-2 py-1 text-[11px]" />
      </div>
      {runs && shown.length === 0 && <div className="text-mist">{runs.length === 0 ? "No runs yet. Start one from \"Run plan\" or \"Tests & training\"." : "No run matches this filter."}</div>}
      <ul className="space-y-1.5">
        {shown.map((r) => {
          const when = r.finished_at || r.started_at;
          return (
            <li key={r.id} className={`flex flex-wrap items-center justify-between gap-2 rounded-xl border border-slate border-l-4 ${BORDER[r.status] || "border-l-slate"} px-2.5 py-1.5`}>
              <div className="min-w-0 flex-1">
                <button type="button" className="text-left font-bold underline-offset-2 hover:underline" onClick={() => openRun(r.id)}>{r.title}</button>{" "}
                <StatusPill status={r.status} />
                {r.trigger === "schedule" && <span className="ml-1 rounded border border-slate px-1 text-[9px] text-mist" title="Started by the timetable">AUTO</span>}
                <div className="text-[10px] text-mist">
                  {when ? `${istStamp(when)} IST (${relTime(when)})` : "queued"}{r.by ? ` · ${r.by}` : ""}
                  {r.finished_at && r.started_at ? ` · took ${fmtDur((new Date(r.finished_at).getTime() - new Date(r.started_at).getTime()) / 1000)}` : ""}
                  {r.kind === "pack" ? ` · ${(r.jobs || []).length} jobs` : ""}
                  {r.notified ? ` · ${telegramNote(r.notified)}` : ""}
                </div>
                {r.headline?.[0] && <div className="truncate text-[11px] text-mist" title={(r.headline || []).join("\n")}>→ {r.headline[0]}</div>}
                {r.status === "blocked" && <div className="text-[11px] text-signal-hold">{r.note}</div>}
              </div>
              <div className="flex shrink-0 gap-1.5">
                <Btn small onClick={() => openRun(r.id)}>Open</Btn>
                {!ACTIVE.includes(r.status) && r.status !== "blocked" && <Btn small onClick={() => dl(r.id, "output.txt")}>txt</Btn>}
                {!ACTIVE.includes(r.status) && r.status !== "blocked" && <Btn small onClick={() => dl(r.id, "zip")}>zip</Btn>}
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

export function RunWindow() {
  const { openRunId, openRun } = useRunner();
  const [run, setRun] = useState<RunRec | null>(null);
  const [text, setText] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [gone, setGone] = useState(false);
  const [copied, setCopied] = useState(false);
  const offset = useRef(0);
  const box = useRef<HTMLPreElement | null>(null);
  const stick = useRef(true);

  useEffect(() => {
    setRun(null); setText(""); setErr(null); setGone(false); offset.current = 0; stick.current = true;
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
        if (stop) return;
        if (e instanceof ApiError && e.status === 404) { setGone(true); live = false; }      // group 447: replaced by a newer result of the same test
        else setErr(e?.message || String(e));
      }
      if (!stop && live) timer = setTimeout(tick, 1500);
    };
    void tick();
    return () => { stop = true; if (timer) clearTimeout(timer); };
  }, [openRunId]);

  useEffect(() => { if (stick.current && box.current) box.current.scrollTop = box.current.scrollHeight; }, [text]);

  if (!openRunId) return null;
  const close = () => openRun(null);
  const active = !!run && ACTIVE.includes(run.status);
  const copy = async () => { try { await navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); } catch { setErr("Copy is not allowed in this browser; use Download txt."); } };
  const jumpEnd = () => { stick.current = true; if (box.current) box.current.scrollTop = box.current.scrollHeight; };
  return (
    <Modal wide title={run?.title || "Run"} onClose={close}>
      {gone ? (
        <div className="space-y-2">
          <Msg kind="warn">This result is no longer on the runner: a newer run of the same test with the same settings replaced it (only the latest result is kept). Close this window and open the newer one from "Runs & downloads".</Msg>
          <Btn small onClick={close}>Close</Btn>
        </div>
      ) : (<>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          {run && <StatusPill status={run.status} />}
          {run && <span className="text-[11px] text-mist">{run.id}</span>}
          <span className="ml-auto flex flex-wrap gap-1.5">
            {active && <Btn small onClick={() => void runnerApi.cancel(openRunId).catch((e) => setErr(e?.message))}>Cancel run</Btn>}
            <Btn small onClick={() => void copy()} disabled={!text}>{copied ? "Copied" : "Copy"}</Btn>
            <Btn small onClick={() => void downloadRunFile(openRunId, "output.txt").catch((e) => setErr(e?.message))}>Download txt</Btn>
            <Btn small onClick={() => void downloadRunFile(openRunId, "zip").catch((e) => setErr(e?.message))}>Zip</Btn>
          </span>
        </div>
        {err && <Msg kind="err">{err}</Msg>}
        {run?.headline?.length ? <div className="mb-2 rounded-lg border border-slate p-2 text-[11px]"><b>Headline lines</b>{run.headline.map((h, i) => <div key={i}>{h}</div>)}</div> : null}
        {run && (run.jobs?.length ?? 0) > 0 && (
          <div className="mb-2 flex flex-wrap gap-1.5 text-[11px]">{(run.jobs || []).map((j) => <span key={j.id} className="inline-flex items-center gap-1 rounded-lg border border-slate px-1.5 py-0.5">{j.title} <StatusPill status={j.status} /></span>)}</div>
        )}
        <pre ref={box} onScroll={(e) => { const el = e.currentTarget; stick.current = el.scrollTop + el.clientHeight >= el.scrollHeight - 24; }} aria-label="Run output"
          className="h-[48vh] overflow-auto whitespace-pre-wrap rounded-lg border border-slate bg-black/30 p-2 text-[11px]">{text || (active ? "Waiting for output…" : "No output")}</pre>
        {run?.files && run.files.length > 1 && (
          <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]"><b>Files:</b>
            {run.files.map((f) => (
              <button key={f.name} type="button" className="rounded-lg border border-slate px-1.5 py-0.5 hover:bg-white/5" onClick={() => void downloadRunFile(openRunId, f.name).catch((e) => setErr(e?.message))}>{f.name} ({Math.max(1, Math.round(f.bytes / 1024))} KB)</button>
            ))}
          </div>
        )}
        {run?.note && <p className="mt-2 text-[11px] text-signal-hold">{run.note}</p>}
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[10px] text-mist">
          {run && <span>Started {relTime(run.started_at)}{run.finished_at ? ` · finished ${istStamp(run.finished_at)} IST` : ""}{run.notified ? ` · ${telegramNote(run.notified)}` : ""}.</span>}
          <span className="ml-auto flex gap-1.5"><Btn small onClick={jumpEnd}>Jump to end</Btn><Btn small onClick={close}>Close</Btn></span>
        </div>
      </>)}
    </Modal>
  );
}
