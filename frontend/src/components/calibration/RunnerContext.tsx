// group 442: one place that talks to calibration-runner-service for the whole page: catalog, summary (what ran last, queue), starting a run (with the
// market-hours confirm step) and the run window. Polls fast while something is queued / running, slowly otherwise, and only while the tab is visible.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ACTIVE, ApiError, istStamp, relTime, runnerApi, telegramNote, type Catalog, type RunRec, type Summary } from "../../calibrationApi";
import { setVisibleInterval } from "../../visibleInterval";
import { Btn, Msg } from "./ui";
import { Modal } from "./SwitchesPanel";

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

  const value = useMemo<Ctx>(() => ({
    catalog, summary, error, loading, busyActive: active, reload, start, openRun: setOpenRunId, cancel, openRunId, toast,
  }), [catalog, summary, error, loading, active, reload, start, cancel, openRunId, toast]);

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
