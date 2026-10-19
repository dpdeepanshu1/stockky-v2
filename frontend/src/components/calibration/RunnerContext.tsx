// group 442: one place that talks to calibration-runner-service for the whole page: catalog, summary (what ran last, queue), starting a run (with the
// market-hours confirm step) and the run window. Polls fast while something is queued / running, slowly otherwise, and only while the tab is visible.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ACTIVE, ApiError, runnerApi, type Catalog, type RunRec, type Summary } from "../../calibrationApi";
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
  start: (kind: "job" | "pack", target: string, params?: Record<string, any>) => Promise<void>;
  openRun: (id: string | null) => void;
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
  const [blocked, setBlocked] = useState<{ run: RunRec; params: Record<string, any> } | null>(null);
  const [toast, setToast] = useState<Ctx["toast"]>(null);
  const alive = useRef(true);

  const reload = useCallback(async () => {
    try {
      const [c, s] = await Promise.all([runnerApi.catalog(), runnerApi.summary()]);
      if (!alive.current) return;
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
    try { const s = await runnerApi.summary(); if (alive.current) { setSummary(s); setError(null); } } catch { /* keep the last good one; reload() reports persistent errors */ }
  }, []);

  useEffect(() => { alive.current = true; setLoading(true); void reload(); return () => { alive.current = false; }; }, [reload, authKey]);

  const active = !!summary && (summary.state.current != null || summary.state.queued.length > 0);
  useEffect(() => setVisibleInterval(() => void refreshSummary(), active ? 4000 : 20000), [active, refreshSummary]);

  const start = useCallback(async (kind: "job" | "pack", target: string, params: Record<string, any> = {}) => {
    setToast(null);
    try {
      const run = await runnerApi.start(kind, target, params, false);
      if (run.status === "blocked") { setBlocked({ run, params }); return; }
      setToast({ kind: "ok", text: `Started: ${run.title}. It runs in the queue, one at a time.` });
      setOpenRunId(run.id);
      await refreshSummary();
    } catch (e: any) {
      setToast({ kind: "err", text: e?.message || String(e) });
    }
  }, [refreshSummary]);

  const confirmBlocked = useCallback(async () => {
    if (!blocked) return;
    const { run, params } = blocked;
    setBlocked(null);
    try {
      const r = await runnerApi.start(run.kind, run.target, params, true);
      setToast({ kind: "ok", text: `Started: ${r.title}.` });
      setOpenRunId(r.id);
      await refreshSummary();
    } catch (e: any) {
      setToast({ kind: "err", text: e?.message || String(e) });
    }
  }, [blocked, refreshSummary]);

  const value = useMemo<Ctx>(() => ({
    catalog, summary, error, loading, busyActive: active, reload, start, openRun: setOpenRunId, openRunId, toast,
  }), [catalog, summary, error, loading, active, reload, start, openRunId, toast]);

  return (
    <RunnerCtx.Provider value={value}>
      {children}
      {blocked && (
        <Modal title="Not now?" onClose={() => setBlocked(null)}>
          <p>{blocked.run.note}</p>
          <div className="mt-3 flex gap-2"><Btn primary onClick={() => void confirmBlocked()}>Run anyway</Btn><Btn onClick={() => setBlocked(null)}>Cancel</Btn></div>
        </Modal>
      )}
    </RunnerCtx.Provider>
  );
}

export function RunnerBanner() {
  const { summary, error, toast } = useRunner();
  const cur = summary?.state.current;
  return (
    <div className="space-y-2">
      {error && <Msg kind="err">{error}</Msg>}
      {toast && <Msg kind={toast.kind === "err" ? "err" : toast.kind === "warn" ? "warn" : "ok"}>{toast.text}</Msg>}
      {summary && (
        <div className="flex flex-wrap gap-2 text-[11px] text-mist">
          <span className={`rounded-lg border px-2 py-0.5 ${summary.state.market_open_now ? "border-signal-hold text-signal-hold" : "border-slate"}`}>{summary.state.market_open_now ? "market hours: heavy runs are held back" : "market closed: heavy runs allowed"}</span>
          <span className="rounded-lg border border-slate px-2 py-0.5">IST {summary.state.now_ist.slice(11, 16)}</span>
          {cur && <span className="rounded-lg border border-signal-buy px-2 py-0.5 text-signal-buy">running: {cur.title}</span>}
          {summary.state.queued.length > 0 && <span className="rounded-lg border border-slate px-2 py-0.5">{summary.state.queued.length} waiting</span>}
        </div>
      )}
    </div>
  );
}

export { ACTIVE };
