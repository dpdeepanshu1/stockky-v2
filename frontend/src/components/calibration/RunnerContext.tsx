// group 442: one place that talks to calibration-runner-service for the whole page: catalog, summary (what ran last, queue), starting a run (with the
// market-hours confirm step) and the run window. Polls fast while something is queued / running, slowly otherwise, and only while the tab is visible.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ACTIVE, ApiError, activeRuns, istStamp, relTime, runnerApi, svcLabel, telegramNote, type Catalog, type RunRec, type Summary, type Svc } from "../../calibrationApi";
import { setVisibleInterval } from "../../visibleInterval";
import { Btn, Msg } from "./ui";
import { Modal } from "./SwitchesPanel";

/** group 471 / E235: progress of "Run all test cases" (several tests at a time, as many as the runner has free slots). */
export interface RunAllState {
  running: boolean; total: number; index: number; currentTitle: string; currentRunId: string | null;
  counts: Record<string, number>; skipped: { title: string; why: string }[]; stopped: boolean; finished: boolean;
}
const NO_RUN_ALL: RunAllState = { running: false, total: 0, index: 0, currentTitle: "", currentRunId: null, counts: {}, skipped: [], stopped: false, finished: false };

interface Ctx {
  svc: Svc;      // E235: the page tab (Real Trade / Position Stocks) every run started from here is tagged with
  catalog: Catalog | null;
  summary: Summary | null;
  error: string | null;
  loading: boolean;
  busyActive: boolean;
  reload: () => Promise<void>;
  start: (kind: "job" | "pack", target: string, params?: Record<string, any>, resume?: boolean) => Promise<void>;      // group 466: resume = continue from the symbols a stopped run saved
  openRun: (id: string | null) => void;
  cancel: (id: string) => Promise<void>;
  openRunId: string | null;
  runAll: RunAllState;
  startRunAll: (ids: string[]) => Promise<void>;      // group 471 / E235: start these tests several at a time (a test that needs another test's data waits for it)
  stopRunAll: () => void;
  setWorkers: (n: number) => Promise<void>;      // E235: how many tests may run at once (saved on the runner)
  toast: { kind: "ok" | "warn" | "err"; text: string } | null;
}

const RunnerCtx = createContext<Ctx | null>(null);
export function useRunner(): Ctx {
  const c = useContext(RunnerCtx);
  if (!c) throw new Error("useRunner outside RunnerProvider");
  return c;
}

export function RunnerProvider({ children, authKey, svc }: { children: ReactNode; authKey: string; svc: Svc }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<{ run: RunRec; params: Record<string, any>; resume: boolean } | null>(null);
  const [toast, setToast] = useState<Ctx["toast"]>(null);
  const alive = useRef(true);
  const [runAll, setRunAll] = useState<RunAllState>(NO_RUN_ALL);
  const stopAll = useRef(false);
  const inFlight = useRef<Map<string, string>>(new Map());      // E235: run id -> title of every test "Run all" has going right now

  const reload = useCallback(async () => {
    try {
      const [c, s] = await Promise.all([runnerApi.catalog(), runnerApi.summary(svc)]);
      if (!alive.current) return;
      if (!c || !Array.isArray(c.jobs) || !s || !s.state) throw new Error("The runner answered with an unexpected shape (catalog / summary). Rebuild calibration-runner-service.");
      setCatalog(c);
      setSummary(s);
      setError(null);
    } catch (e: any) {
      if (!alive.current) return;
      setError(e instanceof ApiError && e.status === 0
        ? `${e.message}. Is calibration-runner-service running? (docker compose up -d --build calibration-runner-service, and the nginx /calibration/ block installed)`
        : e?.message || "runner unavailable");
    } finally {
      if (alive.current) setLoading(false);
    }
  }, [svc]);

  const refreshSummary = useCallback(async () => {
    try { const s = await runnerApi.summary(svc); if (alive.current && s && s.state) { setSummary(s); setError(null); } } catch { /* keep the last good one; reload() reports persistent errors */ }
  }, [svc]);

  // group 456: a message used to stay on screen until the next run was started; it now clears itself (errors stay longer)
  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => setToast(null), toast.kind === "err" ? 15000 : 8000);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => { alive.current = true; setLoading(true); void reload(); return () => { alive.current = false; }; }, [reload, authKey]);

  const active = !!summary?.state && (activeRuns(summary.state).length > 0 || (summary.state.queued?.length ?? 0) > 0);
  useEffect(() => setVisibleInterval(() => void refreshSummary(), active ? 4000 : 20000), [active, refreshSummary]);

  const start = useCallback(async (kind: "job" | "pack", target: string, params: Record<string, any> = {}, resume = false) => {
    setToast(null);
    try {
      const run = await runnerApi.start(kind, target, params, false, resume, svc);
      if (run.status === "blocked") { setBlocked({ run, params, resume }); return; }
      setToast({ kind: "ok", text: `${resume ? "Resuming" : "Started"}: ${run.title}. It runs in the queue (${svcLabel(svc)} tab); independent tests run side by side.` });
      setOpenRunId(run.id);
      await refreshSummary();
    } catch (e: any) {
      setToast({ kind: "err", text: e?.message || String(e) });
    }
  }, [refreshSummary, svc]);

  const cancel = useCallback(async (id: string) => {
    if (!window.confirm("Cancel this run? Whatever it already did stays; the rest is not run.")) return;
    try { await runnerApi.cancel(id); setToast({ kind: "ok", text: "Cancel requested. It stops within a few seconds." }); }
    catch (e: any) { setToast({ kind: "err", text: e?.message || String(e) }); }
    await refreshSummary();
  }, [refreshSummary]);

  const confirmBlocked = useCallback(async () => {
    if (!blocked) return;
    const { run, params, resume } = blocked;
    setBlocked(null);
    try {
      const r = await runnerApi.start(run.kind, run.target, params, true, resume, svc);
      setToast({ kind: "ok", text: `${resume ? "Resuming" : "Started"}: ${r.title}.` });
      setOpenRunId(r.id);
      await refreshSummary();
    } catch (e: any) {
      setToast({ kind: "err", text: e?.message || String(e) });
    }
  }, [blocked, refreshSummary, svc]);

  // group 471 / E235: "Run all test cases". The runner now runs several tests at once, so several lanes submit the plan in order (a lane takes the next test
  // when its own has ended). A test that reads data another test writes waits inside the runner, not here. The queue (60, no duplicates) never overflows and
  // Stop cancels every test that is going. A test the market-hours rule holds back is listed as skipped, never forced. A 401 ends it (log in, run again).
  const startRunAll = useCallback(async (ids: string[]) => {
    if (runAll.running || ids.length === 0) return;
    const title = (id: string) => (catalog?.jobs || []).find((j) => j.id === id)?.title || id;
    stopAll.current = false;
    inFlight.current = new Map();
    let counts: Record<string, number> = {};
    let next = 0; let started = 0; let lost = false;
    const skipped: { title: string; why: string }[] = [];
    const push = (p: Partial<RunAllState>) => { if (alive.current) setRunAll((x) => ({ ...x, ...p })); };
    const show = () => push({ currentTitle: [...inFlight.current.values()].join(" · "), currentRunId: [...inFlight.current.keys()][0] ?? null });
    const lanes = Math.max(1, Math.min(4, summary?.state?.workers ?? 3));
    setRunAll({ ...NO_RUN_ALL, running: true, total: ids.length });
    const lane = async () => {
      while (next < ids.length && !stopAll.current && !lost && alive.current) {
        const id = ids[next++]; started += 1; push({ index: started });
        let run: RunRec;
        try {
          run = await runnerApi.start("job", id, {}, false, false, svc);
        } catch (e: any) {
          if (e instanceof ApiError && e.status === 401) { skipped.push({ title: title(id), why: "login expired: log in again and re-run" }); lost = true; break; }
          skipped.push({ title: title(id), why: e?.message || String(e) });
          push({ skipped: [...skipped] });
          continue;
        }
        if (run.status === "blocked") { skipped.push({ title: title(id), why: (run.note || "held back by the market-hours rule").split(".")[0] }); push({ skipped: [...skipped] }); continue; }
        inFlight.current.set(run.id, title(id)); show();
        const limit = Date.now() + (((catalog?.jobs || []).find((j) => j.id === id)?.timeout_s || 3600) + 900) * 1000 + 3600_000;      // + the time it may wait for a data lock
        let status: string = run.status;
        while (ACTIVE.includes(status as any) && Date.now() < limit && alive.current) {
          await new Promise((r) => setTimeout(r, 3000));
          try { status = (await runnerApi.run(run.id)).status; } catch (e: any) { if (e instanceof ApiError && e.status === 404) { status = "cancelled"; break; } }
        }
        inFlight.current.delete(run.id);
        counts = { ...counts, [status]: (counts[status] || 0) + 1 };
        push({ counts }); show();
        await refreshSummary();
      }
    };
    await Promise.all(Array.from({ length: Math.min(lanes, ids.length) }, () => lane()));
    push({ running: false, finished: true, stopped: stopAll.current, skipped, currentRunId: null });
    if (alive.current) setToast({ kind: stopAll.current ? "warn" : "ok", text: stopAll.current ? "Run all stopped." : "Run all finished: open \"Runs & downloads\" and use Download all latest results." });
  }, [runAll.running, catalog, refreshSummary, svc, summary?.state?.workers]);

  const stopRunAll = useCallback(() => {
    stopAll.current = true;
    for (const id of inFlight.current.keys()) void runnerApi.cancel(id).catch(() => undefined);      // every test that is running right now is cancelled too
  }, []);

  const setWorkers = useCallback(async (n: number) => {
    try { await runnerApi.setWorkers(n); await refreshSummary(); }
    catch (e: any) { setToast({ kind: "err", text: e?.message || String(e) }); }
  }, [refreshSummary]);

  const value = useMemo<Ctx>(() => ({
    svc, catalog, summary, error, loading, busyActive: active, reload, start, openRun: setOpenRunId, cancel, openRunId, toast, runAll, startRunAll, stopRunAll, setWorkers,
  }), [setWorkers, svc, catalog, summary, error, loading, active, reload, start, cancel, openRunId, toast, runAll, startRunAll, stopRunAll]);

  return (
    <RunnerCtx.Provider value={value}>
      {children}
      <RunningDock />
      {blocked && (
        <Modal title="Not now?" onClose={() => setBlocked(null)}>
          <p>{blocked.run.note}</p>
          <div className="mt-3 flex gap-2"><Btn primary onClick={() => void confirmBlocked()}>Run anyway</Btn><Btn onClick={() => setBlocked(null)}>Cancel</Btn></div>
        </Modal>
      )}
    </RunnerCtx.Provider>
  );
}

/** group 456 / E235: a pill that stays on screen while runs are going and no run window is open: one row per active run (several at once), each with Watch / Cancel. */
function RunningDock() {
  const { summary, openRunId, openRun, cancel } = useRunner();
  const list = activeRuns(summary?.state);
  const waiting = summary?.state?.queued?.length ?? 0;
  if (!list.length || openRunId || typeof document === "undefined") return null;
  return createPortal(
    <div className="fixed bottom-20 right-3 z-40 flex max-w-[calc(100vw-1.5rem)] flex-col gap-1.5 rounded-xl border border-signal-buy bg-graphite px-3 py-2 text-[12px] shadow-2xl md:bottom-4 md:right-4" role="status" data-testid="running-dock">
      {list.map((cur) => (
        <div key={cur.id} className="flex flex-wrap items-center gap-2">
          <span className="flex min-w-0 items-center gap-2 text-signal-buy">
            <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-current" aria-hidden="true" />
            <span className="min-w-0 truncate"><b>Running:</b> {cur.title}</span>
          </span>
          <span className="text-[11px] text-mist">{svcLabel(cur.service)} · started {relTime(cur.started_at)}</span>
          <span className="ml-auto flex gap-1.5">
            <Btn small primary onClick={() => openRun(cur.id)}>Watch</Btn>
            <Btn small onClick={() => void cancel(cur.id)}>Cancel</Btn>
          </span>
        </div>
      ))}
      {waiting > 0 && <span className="text-[11px] text-mist">{waiting} waiting</span>}
    </div>,
    document.body,
  );
}

export function RunnerBanner() {
  const { summary, error, toast, openRun, setWorkers } = useRunner();
  const running = activeRuns(summary?.state);
  const la = summary?.state?.last_automatic;
  const laTone = la && ["failed", "timeout", "blocked"].includes(la.status) ? "border-signal-sell text-signal-sell" : la && la.status === "attention" ? "border-signal-hold text-signal-hold" : "border-slate";
  return (
    <div className="space-y-2">
      {error && <Msg kind="err">{error}</Msg>}
      {toast && <Msg kind={toast.kind === "err" ? "err" : toast.kind === "warn" ? "warn" : "ok"}>{toast.text}</Msg>}
      {summary?.state && (
        <div className="flex flex-wrap gap-2 text-[11px] text-mist">
          <span className={`rounded-lg border px-2 py-0.5 ${summary.state.market_open_now ? "border-signal-hold text-signal-hold" : "border-slate"}`}>{summary.state.market_open_now ? "market hours: heavy runs are held back" : "market closed: heavy runs allowed"}</span>
          <span className="rounded-lg border border-slate px-2 py-0.5">IST {String(summary.state.now_ist || "").slice(11, 16)}</span>
          {running.map((cur) => <button key={cur.id} type="button" onClick={() => openRun(cur.id)} title="Open the live output" className="rounded-lg border border-signal-buy px-2 py-0.5 text-signal-buy hover:bg-white/5">▶ running: {cur.title} · {svcLabel(cur.service)} · watch live</button>)}
          {summary.state.workers != null && (
            <span className="flex items-center gap-1 rounded-lg border border-slate px-2 py-0.5" title="Tests that may run at the same time (1-8). Saved on the runner; a run already going is never stopped by lowering it." data-testid="workers-control">
              {running.length} of {summary.state.workers} slots busy
              <button type="button" aria-label="Fewer tests at once" className="px-1 hover:bg-white/10" disabled={summary.state.workers <= 1} onClick={() => void setWorkers(summary.state.workers! - 1)}>−</button>
              <button type="button" aria-label="More tests at once" className="px-1 hover:bg-white/10" disabled={summary.state.workers >= 8} onClick={() => void setWorkers(summary.state.workers! + 1)}>+</button>
            </span>
          )}
          {/* group 447: when the timetable last ran something by itself, and whether Telegram was told */}
          <span data-testid="last-automatic" className={`rounded-lg border px-2 py-0.5 ${laTone}`} title={la ? `${la.title}: ${la.status}` : "No run has been started by the timetable yet"}>
            {la ? `last automatic run: ${istStamp(la.finished_at)} IST (${relTime(la.finished_at)}) · ${la.title} · ${la.status}${la.telegram ? ` · ${telegramNote(la.telegram)}` : ""}` : "last automatic run: none yet"}
          </span>
          {(summary.state.queued?.length ?? 0) > 0 && <span className="rounded-lg border border-slate px-2 py-0.5">{summary.state.queued.length} waiting</span>}
        </div>
      )}
    </div>
  );
}

export { ACTIVE };
