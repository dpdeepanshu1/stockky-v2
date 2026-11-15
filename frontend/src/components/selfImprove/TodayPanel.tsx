// Self Improve System > Today / Loop (round 542, E567). Shows the nightly chain with an honest state per step, the auto-calibration banner (OFF / DRY-RUN / LIVE),
// the "why no ideas" line with its counts, the timetable and the next agent run. Buttons: Run now (report), Run now (propose, behind a second click) and
// a PREVIEW of auto-calibration (dry run: it applies nothing). Nothing here sets a trading switch. Pure helpers: selfImproveView.ts, agentView.ts.
import { useEffect, useState } from "react";
import { runnerApi, type AutoReport } from "../../calibrationApi";
import { runBlock, runResultLines, type AgentRunResult } from "../../agentView";
import { bannerLabel, bannerTone, breakdownChips, chainSummary, nextArm, nextRunText, slotLine, slotTodayText, slotTodayTone, stepLabel, stepMark, stepTone, whyLine, type HistoryDay, type LoopView } from "../../selfImproveView";
import { Btn, Card, Chip, Msg } from "../calibration/ui";

const ARM_MS = 8000;

export default function TodayPanel({ loop, latest, reload }: { loop: LoopView; latest: HistoryDay | null; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<AgentRunResult | null>(null);
  const [prev, setPrev] = useState<AutoReport | null>(null);
  const [armed, setArmed] = useState<string | null>(null);

  // an armed (first-clicked) action disarms itself, so a stray second click minutes later cannot fire it
  useEffect(() => {
    if (!armed) return;
    const id = setTimeout(() => setArmed(null), ARM_MS);
    return () => clearTimeout(id);
  }, [armed]);

  const act = async (fn: () => Promise<void>) => {
    setBusy(true);
    try { await fn(); setErr(null); await reload(); } catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  const block = runBlock(loop.agent);
  const click = (id: string, fire: () => Promise<void>, twoClicks: boolean) => {
    if (!twoClicks) { void act(fire); return; }
    const n = nextArm(armed, id);
    setArmed(n.armed);
    if (n.fire) void act(fire);
  };
  const a = loop.agent;
  const ac = loop.autocalib;

  return (
    <div className="space-y-3" data-testid="si-today">
      {err && <Msg kind="err">{err}</Msg>}

      <Card title={`Auto-calibration: ${bannerLabel(loop.banner)}`} tone={bannerTone(loop.banner)}>
        <p className="text-mist">{loop.banner.text}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Chip tone={bannerTone(loop.banner)}>{bannerLabel(loop.banner)}</Chip>
          <Chip>{loop.banner.unpinned.length} unpinned: {loop.banner.unpinned.length ? loop.banner.unpinned.join(", ") : "none (the agent can name nothing)"}</Chip>
          <Chip>last evaluation: {ac.last ? `changed ${ac.last.changed ?? 0} · waiting ${ac.last.waiting ?? 0}${ac.last.dry_run ? " (dry-run)" : ""}` : "none yet"}</Chip>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Btn small disabled={busy} title="Dry run: shows what auto-calibration would do and applies nothing" onClick={() => void act(async () => setPrev(await runnerApi.autoRun(true)))}>Preview auto-calibration (changes nothing)</Btn>
        </div>
        <p className="mt-1 text-[11px] text-mist">Applying changes is not done from this page. Auto-calibration's own controls are under Real Money Calibration &gt; Auto-calibration.</p>
        {prev && (
          <div className="mt-2 space-y-1" role="status">
            <Msg kind={prev.ran ? "info" : "warn"}>{prev.ran ? `Preview: changed ${prev.changed ?? 0} · waiting ${prev.waiting ?? 0} · errors ${prev.errors ?? 0}${prev.dry_run ? " (nothing applied)" : ""}` : prev.reason || "The preview did not run."}</Msg>
            {prev.rows.filter((r) => r.status !== "pinned").map((r) => (
              <div key={`${r.service}${r.key}`} className="rounded-lg border border-slate px-2 py-1">
                <div className="flex flex-wrap items-center gap-2"><Chip>{r.status.toUpperCase()}</Chip><b>{r.key}</b>{r.to != null && <span className="text-mist">{r.from} → {r.to}</span>}</div>
                <div className="text-[11px] text-mist">{r.detail}</div>
              </div>
            ))}
            {prev.rows.filter((r) => r.status === "pinned").length > 0 && <p className="text-[11px] text-mist">{prev.rows.filter((r) => r.status === "pinned").length} pinned switch(es) are not shown: they are never changed automatically.</p>}
          </div>
        )}
      </Card>

      <Card title="Tonight's chain" right={<span className="text-[11px] text-mist">{loop.weekday} {loop.now_ist} IST</span>}>
        <p className="text-mist">Tests, then the agent, then the tests it queued, then auto-calibration. A step is DONE, WAITING, SKIPPED, FAILED, NOT DUE or UNKNOWN; it is never guessed.</p>
        <div className="mt-2 space-y-1" role="list" aria-label="Tonight's chain">
          {loop.chain.map((c) => (
            <div key={c.id} role="listitem" className="rounded-lg border border-slate px-2 py-1">
              <div className="flex flex-wrap items-center gap-2"><Chip tone={stepTone(c.state)}>{stepMark(c.state)} {stepLabel(c.state)}</Chip><b>{c.title}</b></div>
              <div className="text-[11px] text-mist">{c.detail}</div>
            </div>
          ))}
        </div>
        {loop.chain.length > 0 && <p className="mt-1 text-[11px] text-mist">{chainSummary(loop.chain)}</p>}
      </Card>

      <Card title="AI agent" tone={a.stopped || a.mode_effective === "off" ? "bad" : a.mode_effective === "propose" ? "warn" : "good"}>
        <p className="text-mist">{nextRunText(a.next_run)}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Chip tone={a.stopped || a.mode_effective === "off" ? "bad" : a.mode_effective === "propose" ? "warn" : "good"}>mode: {a.stopped ? "OFF (switched itself off)" : a.mode_effective}</Chip>
          <Chip>runs after {a.run_after_ist} IST on weekdays</Chip>
          {a.last && <Chip>last run: {a.last.date || "-"} ({a.last.trigger || "-"}) · {a.last.ideas ?? 0} idea(s)</Chip>}
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {a.stopped && <Btn small primary disabled={busy} onClick={() => void act(async () => { await runnerApi.agentResume(); setRes(null); })}>Resume</Btn>}
          <Btn small disabled={busy || !!block} title={block || "Ask the AI now (report only: queues nothing)"} onClick={() => click("report", async () => setRes(await runnerApi.agentRun("report")), false)}>Run now (report)</Btn>
          <Btn small primary={armed === "propose"} disabled={busy || !!block} title={block || "Ask the AI now and queue the matching tests. Needs a second click."} onClick={() => click("propose", async () => setRes(await runnerApi.agentRun("propose")), true)}>
            {armed === "propose" ? "Click again to confirm: run and queue tests" : "Run now (propose)"}
          </Btn>
          {armed === "propose" && <Btn small onClick={() => setArmed(null)}>Cancel</Btn>}
        </div>
        {block && <p className="mt-1 text-[11px] text-mist">{block}</p>}
        {armed === "propose" && <p className="mt-1 text-[11px] text-mist">Propose also queues the existing tests behind the ideas. It still sets no trading switch.</p>}
        {res && <div className="mt-2 space-y-1" role="status">{runResultLines(res).map((l, i) => <Msg key={i} kind={res.ran ? (l.startsWith("Warning") ? "warn" : "info") : "warn"}>{l}</Msg>)}</div>}
      </Card>

      <Card title={latest ? `Why ${latest.ideas && latest.ideas.length ? "these ideas" : "no ideas"}? Run of ${latest.date}` : "Why no ideas?"}>
        {!latest && <p className="text-mist">The agent has not saved a run yet. After 15:45 IST use Run now (report) once.</p>}
        {latest && (
          <>
            <p className="text-mist">{latest.unreadable ? "The saved file of this run could not be read." : whyLine(latest.breakdown)}</p>
            <div className="mt-2 flex flex-wrap items-center gap-2">{breakdownChips(latest.breakdown).map((c, i) => <Chip key={i} tone={c.tone}>{c.label}</Chip>)}</div>
          </>
        )}
      </Card>

      <Card title="Test timetable" tone={loop.schedule.enabled ? undefined : "warn"}>
        {!loop.schedule.enabled && <Msg kind="warn">The timetable is switched off: the evening tests do not start by themselves.</Msg>}
        <div className="space-y-1">
          {loop.schedule.slots.map((s) => (
            <div key={s.id} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate px-2 py-1">
              <b>{slotLine(s)}</b>
              <Chip tone={slotTodayTone(s.today)}>{slotTodayText(s.today)}</Chip>
              <span className="text-[11px] text-mist">next: {s.next_ist ? `${s.next_ist} IST` : "-"}</span>
            </div>
          ))}
          {loop.schedule.slots.length === 0 && <p className="text-mist">No timetable slots.</p>}
        </div>
      </Card>
    </div>
  );
}
