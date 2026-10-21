// group 476: Auto-calibration. Every switch that has a test is set from that test's result; each change is sent to Telegram.
// This panel shows the master switch, which test decides which switch, what each switch is waiting for, the change history, and which switches are
// automated / protected / still manual. Backend: /autocalib/* on calibration-runner-service.
import { useCallback, useEffect, useState } from "react";
import { runnerApi, type AutoReport, type AutoStatus } from "../../calibrationApi";
import { Btn, Card, Chip, Msg } from "./ui";

const when = (ts: number) => new Date(ts * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }).slice(0, 17);
const STATUS_TONE: Record<string, "good" | "warn" | "bad" | undefined> = { changed: "good", ok: "good", would_change: "warn", waiting: "warn", cooldown: "warn", error: "bad", service_unreachable: "bad" };
const STATUS_TEXT: Record<string, string> = {
  changed: "CHANGED", ok: "ALREADY RIGHT", hold: "NO CHANGE", would_change: "WOULD CHANGE", waiting: "WAITING 2ND RUN", cooldown: "COOL-DOWN",
  pinned: "PINNED", no_evidence: "NO TEST RESULT", error: "ERROR", service_unreachable: "SERVICE DOWN", not_in_catalog: "NOT LISTED",
};

export default function AutoCalibPanel() {
  const [st, setSt] = useState<AutoStatus | null>(null);
  const [rep, setRep] = useState<AutoReport | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setSt(await runnerApi.autoStatus()); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); }
  }, []);
  useEffect(() => { void load(); const id = setInterval(() => void load(), 30000); return () => clearInterval(id); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); setErr(null); await load(); } catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  if (!st) return <div className="text-mist">{err ? <Msg kind="err">{err}</Msg> : "Loading auto-calibration…"}</div>;
  const s = st.settings;
  const togglePin = (key: string, pinned: boolean) => act(() => runnerApi.autoSettings({ pinned: pinned ? s.pinned.filter((k) => k !== key) : [...s.pinned, key] }));
  const cov = st.coverage;

  return (
    <div className="space-y-3" data-testid="autocalib">
      {err && <Msg kind="err">{err}</Msg>}
      <Card
        title="Auto-calibration"
        tone={s.enabled ? (s.dry_run ? "warn" : "good") : undefined}
        right={<>
          <Btn small disabled={busy} onClick={() => void act(() => runnerApi.autoSettings({ enabled: !s.enabled }))}>{s.enabled ? "Switch OFF" : "Switch ON"}</Btn>
          <Btn small disabled={busy} onClick={() => void act(() => runnerApi.autoSettings({ dry_run: !s.dry_run }))}>{s.dry_run ? "Dry run: ON (applies nothing)" : "Dry run: off"}</Btn>
        </>}
      >
        <p className="text-mist">
          When a test finishes, each switch that has a test is set from the result, with no manual toggling. You get a Telegram message for every change.
          A rule is <b>tightened at once</b>; a rule is <b>loosened only when two different test runs in a row</b> say so. A test that failed, is older than 40 days,
          or says "not enough data" changes nothing. Numbers move at most {s.step_pct * 100}% per evaluation. A changed switch is left alone for {s.cooldown_h} h. Pin a switch to stop it being touched.
        </p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Chip tone={s.enabled ? "good" : "bad"}>{s.enabled ? (s.dry_run ? "ON, dry run" : "ON, applying") : "OFF"}</Chip>
          {st.last && <Chip>last evaluation {st.last.at ? when(st.last.at) : "-"} IST · changed {st.last.changed ?? 0} · waiting {st.last.waiting ?? 0}</Chip>}
          <Btn small disabled={busy} onClick={() => void act(async () => setRep(await runnerApi.autoRun(true)))}>Preview now (no changes)</Btn>
          <Btn small primary disabled={busy || !s.enabled || s.dry_run} title={!s.enabled ? "switch auto-calibration ON first" : s.dry_run ? "turn the dry run off first" : undefined}
            onClick={() => { if (window.confirm("Evaluate every test result now and apply the changes? Each change is sent to Telegram.")) void act(async () => setRep(await runnerApi.autoRun())); }}>Evaluate and apply now</Btn>
        </div>
      </Card>

      {rep && (
        <Card title={rep.ran ? `Result of this evaluation${rep.dry_run ? " (preview, nothing applied)" : ""}` : "Not run"}>
          {!rep.ran && <Msg kind="info">{rep.reason}</Msg>}
          <div className="space-y-1">
            {rep.rows.map((r) => (
              <div key={`${r.service}${r.key}`} className="rounded-lg border border-slate px-2 py-1">
                <div className="flex flex-wrap items-center gap-2"><Chip tone={STATUS_TONE[r.status]}>{STATUS_TEXT[r.status] || r.status}</Chip><b>{r.title}</b>
                  <span className="text-mist">{r.key}{r.to != null && r.from !== r.to ? `: ${r.from} → ${r.to}` : r.from != null ? `: ${r.from}` : ""}</span></div>
                <div className="text-[11px] text-mist">{r.detail}</div>
              </div>
            ))}
          </div>
        </Card>
      )}

      <Card title="Which test decides which switch">
        <div className="space-y-1">
          {st.rules.map((r) => {
            const pend = st.pending[r.key];
            return (
              <div key={`${r.service}${r.key}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate px-2 py-1">
                <b>{r.title}</b><span className="text-mist">{r.key} · {r.service.replace("-service", "")}</span>
                <span className="text-[11px] text-mist">← {r.explain}</span>
                {pend && <Chip tone="warn">waiting: {pend.to} ({pend.votes.length} run)</Chip>}
                <span className="ml-auto"><Btn small disabled={busy} onClick={() => void togglePin(r.key, r.pinned)}>{r.pinned ? "Unpin" : "Pin (never auto)"}</Btn></span>
              </div>
            );
          })}
        </div>
      </Card>

      <Card title={`Changes made automatically (${st.history.length})`}>
        {st.history.length === 0 && <p className="text-mist">None yet. Changes appear here after a test run finishes with a clear verdict.</p>}
        <div className="space-y-1">
          {st.history.map((h, i) => (
            <div key={i} className="rounded-lg border border-slate px-2 py-1">
              <div className="flex flex-wrap items-center gap-2"><span className="text-mist">{when(h.at)}</span><b>{h.title}</b><span>{h.from} → {h.to}</span>
                <Chip tone={h.change === "LOOSENS" ? "warn" : "good"}>{h.change}</Chip>{h.restart_required && <Chip tone="warn">restart needed</Chip>}</div>
              <div className="text-[11px] text-mist">{h.reason}</div>
            </div>
          ))}
        </div>
      </Card>

      {cov && (
        <Card title={`All switches: ${cov.counts.automated ?? 0} automatic · ${cov.counts.protected ?? 0} protected · ${cov.counts.manual ?? 0} manual`}>
          {cov.unreachable.map((u) => <Msg key={u.service} kind="warn">{u.service} did not answer: {u.error}</Msg>)}
          <details><summary className="cursor-pointer text-mist">Show every switch and why</summary>
            <div className="mt-2 space-y-1">
              {cov.switches.map((c) => (
                <div key={`${c.service}${c.key}`} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate px-2 py-1">
                  <Chip tone={c.status === "automated" ? "good" : c.status === "protected" ? "warn" : undefined}>{c.status.toUpperCase()}</Chip>
                  <b>{c.title || c.key}</b><span className="text-mist">{c.key} · {c.service.replace("-service", "")} · now {c.effective ?? "-"}</span>
                  <span className="text-[11px] text-mist">{c.why}</span>
                </div>
              ))}
            </div>
          </details>
        </Card>
      )}
    </div>
  );
}
