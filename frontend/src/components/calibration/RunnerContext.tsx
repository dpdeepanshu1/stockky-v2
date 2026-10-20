// group 442: one place that talks to calibration-runner-service for the whole page: catalog, summary (what ran last, queue), starting a run (with the
// market-hours confirm step) and the run window. Polls fast while something is queued / running, slowly otherwise, and only while the tab is visible.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ACTIVE, ApiError, istStamp, relTime, runnerApi, telegramNote, type Catalog, type RunRec, type Summary } from "../../calibrationApi";
import { setVisibleInterval } from "../../visibleInterval";
import { Btn, Msg } from "./ui";
import { Modal } from "./SwitchesPanel";

/** group 471: progress of "Run all test cases" (one test after the other). */
export interface RunAllState {
  running: boolean; total: number; index: number; currentTitle: string; currentRunId: string | null;
  counts: Record<string, number>; skipped: { title: string; why: string }[]; stopped: boolean; finished: boolean;
}
const NO_RUN_ALL: RunAllState = { running: false, total: 0, index: 0, currentTitle: "", currentRunId: null, counts: {}, skipped: [], stopped: false, finished: false };

interface Ctx {
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
  startRunAll: (ids: string[]) => Promise<void>;      // group 471: start these tests one after the other (each waits for the one before)
  stopRunAll: () => void;
  toast: { kind: "ok" | "warn" | "err"; text: string } | null;
}

const RunnerCtx = createContext<Ctx | null>(null);
export function useRunner(): Ctx {
  const c = useContext(RunnerCtx);
  if (!c) throw new Error("useRunner outside RunnerProvider");
  return c;
}

export function RunnerProvider({ children, authKey }: { children: ReactNode; authKey: string }) {
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
  const allRunId = useRef<string | null>(null);

  const reload = useCallback(async () => {
    try {
      const [c, s] = await Promise.all([runnerApi.catalog(), runnerApi.summary()]);
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
  }, []);

  const refreshSummary = useCallback(async () => {
    try { const s = await runnerApi.summary(); if (alive.current && s && s.state) { setSummary(s); setError(null); } } catch { /* keep the last good one; reload() reports persistent errors */ }
  }, []);

  // group 456: a message used to stay on screen until the next run was started; it now clears itself (errors stay longer)
  useEffect(() => {
    if (!toast) return undefined;
    const t = setTimeout(() => setToast(null), toast.kind === "err" ? 15000 : 8000);
    return () => clearTimeout(t);
  }, [toast]);

  useEffect(() => { alive.current = true; setLoading(true); void reload(); return () => { alive.current = false; }; }, [reload, authKey]);

  const active = !!summary?.state && (summary.state.current != null || (summary.state.queued?.length ?? 0) > 0);
  useEffect(() => setVisibleInterval(() => void refreshSummary(), active ? 4000 : 20000), [active, refreshSummary]);

  const start = useCallback(async (kind: "job" | "pack", target: string, params: Record<string, any> = {}, resume = false) => {
    setToast(null);
    try {
      const run = await runnerApi.start(kind, target, params, false, resume);
      if (run.status === "blocked") { setBlocked({ run, params, resume }); return; }
      setToast({ kind: "ok", text: `${resume ? "Resuming" : "Started"}: ${run.title}. It runs in the queue, one at a time.` });
      setOpenRunId(run.id);
      await refreshSummary();
    } catch (e: any) {
      setToast({ kind: "err", text: e?.message || String(e) });
    }
  }, [refreshSummary]);

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
      const r = await runnerApi.start(run.kind, run.target, params, true, resume);
      setToast({ kind: "ok", text: `${resume ? "Resuming" : "Started"}: ${r.title}.` });
      setOpenRunId(r.id);
      await refreshSummary();
    } catch (e: any) {
      setToast({ kind: "err", text: e?.message || String(e) });
    }
  }, [blocked, refreshSummary]);

  // group 471: "Run all test cases". Each test is submitted only when the one before has ended, so the runner's queue (20, no duplicates) never overflows and
  // Stop really stops. A test the market-hours rule holds back is listed as skipped, never forced. A 401 ends it (log in, run again).
  const startRunAll = useCallback(async (ids: string[]) => {
    if (runAll.running || ids.length === 0) return;
    const title = (id: string) => (catalog?.jobs || []).find((j) => j.id === id)?.title || id;
    stopAll.current = false;
    let counts: Record<string, number> = {};
    const skipped: { title: string; why: string }[] = [];
    const push = (p: Partial<RunAllState>) => { if (alive.current) setRunAll((x) => ({ ...x, ...p })); };
    setRunAll({ ...NO_RUN_ALL, running: true, total: ids.length });
    for (let i = 0; i < ids.length && !stopAll.current && alive.current; i++) {
      const id = ids[i];
      push({ index: i + 1, currentTitle: title(id), currentRunId: null });
      let run: RunRec;
      try {
        run = await runnerApi.start("job", id, {}, false, false);
      } catch (e: any) {
        if (e instanceof ApiError && e.status === 401) { skipped.push({ title: title(id), why: "login expired: log in again and re-run" }); break; }
        skipped.push({ title: title(id), why: e?.message || String(e) });
        push({ skipped: [...skipped] });
        continue;
      }
      if (run.status === "blocked") { skipped.push({ title: title(id), why: (run.note || "held back by the market-hours rule").split(".")[0] }); push({ skipped: [...skipped] }); continue; }
      allRunId.current = run.id;
      push({ currentRunId: run.id });
      const limit = Date.now() + (((catalog?.jobs || []).find((j) => j.id === id)?.timeout_s || 3600) + 900) * 1000;
      let status: string = run.status;
      while (ACTIVE.includes(status as any) && Date.now() < limit && alive.current) {
        await new Promise((r) => setTimeout(r, 3000));
        try { status = (await runnerApi.run(run.id)).status; } catch (e: any) { if (e instanceof ApiError && e.status === 404) { status = "cancelled"; break; } }
      }
      allRunId.current = null;
      counts = { ...counts, [status]: (counts[status] || 0) + 1 };
      push({ counts });
      await refreshSummary();
    }
    push({ running: false, finished: true, stopped: stopAll.current, skipped, currentRunId: null });
    if (alive.current) setToast({ kind: stopAll.current ? "warn" : "ok", text: stopAll.current ? "Run all stopped." : "Run all finished: open \"Runs & downloads\" and use Download all latest results." });
  }, [runAll.running, catalog, refreshSummary]);

  const stopRunAll = useCallback(() => {
    stopAll.current = true;
    const id = allRunId.current;
    if (id) void runnerApi.cancel(id).catch(() => undefined);      // the test that is running right now is cancelled too
  }, []);

  const value = useMemo<Ctx>(() => ({
    catalog, summary, error, loading, busyActive: active, reload, start, openRun: setOpenRunId, cancel, openRunId, toast, runAll, startRunAll, stopRunAll,
  }), [catalog, summary, error, loading, active, reload, start, cancel, openRunId, toast, runAll, startRunAll, stopRunAll]);

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

/** group 456: a pill that stays on screen while a run is going and no run window is open, so you always see WHERE it is running and can watch or cancel it. */
function RunningDock() {
  const { summary, openRunId, openRun, cancel } = useRunner();
  const cur = summary?.state?.current;
  const waiting = summary?.state?.queued?.length ?? 0;
  if (!cur || openRunId || typeof document === "undefined") return null;
  return createPortal(
    <div className="fixed bottom-20 right-3 z-40 flex max-w-[calc(100vw-1.5rem)] flex-wrap items-center gap-2 rounded-xl border border-signal-buy bg-graphite px-3 py-2 text-[12px] shadow-2xl md:bottom-4 md:right-4" role="status" data-testid="running-dock">
      <span className="flex min-w-0 items-center gap-2 text-signal-buy">
        <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-current" aria-hidden="true" />
        <span className="min-w-0 truncate"><b>Running:</b> {cur.title}</span>
      </span>
      <span className="text-[11px] text-mist">started {relTime(cur.started_at)}{waiting > 0 ? ` · ${waiting} waiting` : ""}</span>
      <span className="ml-auto flex gap-1.5">
        <Btn small primary onClick={() => openRun(cur.id)}>Watch</Btn>
        <Btn small onClick={() => void cancel(cur.id)}>Cancel</Btn>
      </span>
    </div>,
    document.body,
  );
}

export function RunnerBanner() {
  const { summary, error, toast, openRun } = useRunner();
  const cur = summary?.state?.current;
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
          {cur && <button type="button" onClick={() => openRun(cur.id)} title="Open the live output" className="rounded-lg border border-signal-buy px-2 py-0.5 text-signal-buy hover:bg-white/5">▶ running: {cur.title} · watch live</button>}
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
