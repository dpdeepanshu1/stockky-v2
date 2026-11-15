// round 536 (agent Round 6): the AI agent card on Real Money Calibration > Auto-calibration. Reads GET /agent/status on calibration-runner-service.
// The agent only SUGGESTS: last night's ideas, the tests it queued, how often an idea survived its own test, and how many free-tier LLM calls were used.
// "Run now" is refused in market hours (the runner's own guard; the button is disabled with the reason). After a self-off the card shows Resume.
// Nothing here sets a trading switch and no key is ever shown (the status only says whether a key is set). Pure helpers: agentView.ts.
import { useCallback, useEffect, useState } from "react";
import { runnerApi } from "../../calibrationApi";
import { headline, keyLine, runBlock, runResultLines, scoreLine, scoreTone, usageLine, whenIst, type AgentRunResult, type AgentStatus } from "../../agentView";
import { Btn, Card, Chip, Msg } from "./ui";

export default function AgentCard() {
  const [st, setSt] = useState<AgentStatus | null>(null);
  const [res, setRes] = useState<AgentRunResult | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setSt(await runnerApi.agentStatus()); setErr(null); } catch (e: any) { setErr(e?.message || String(e)); }
  }, []);
  useEffect(() => { void load(); const id = setInterval(() => void load(), 30000); return () => clearInterval(id); }, [load]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try { await fn(); setErr(null); await load(); } catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  const run = (mode: "report" | "propose") => act(async () => { setRes(await runnerApi.agentRun(mode)); });

  if (!st) return <div className="text-mist" data-testid="agent-card">{err ? <Msg kind="err">{err}</Msg> : "Loading the AI agent…"}</div>;
  const h = headline(st);
  const block = runBlock(st);
  const rep = st.last_report;
  const sb = st.scoreboard;

  return (
    <div data-testid="agent-card">
      <Card title="AI agent (idea source)" tone={h.tone}>
        <p className="text-mist">
          Once a weekday evening after {st.run_after_ist} IST a free LLM looks at the verdicts of your tests and suggests which switches look weak and which way they should move.
          It <b>only suggests</b>: it cannot change a switch, place an order or touch the broker. Your existing tests and the auto-calibration rules above decide whether anything changes.
        </p>
        {err && <Msg kind="err">{err}</Msg>}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Chip tone={h.tone}>{h.text}</Chip>
          <Chip tone={st.keys.gemini || st.keys.groq ? undefined : "bad"}>{keyLine(st.keys)}</Chip>
          <Chip tone={st.usage_today.left <= 0 ? "bad" : st.usage_today.gemini_blocked ? "warn" : undefined}>{usageLine(st.usage_today)}</Chip>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {st.stopped && <Btn small primary disabled={busy} onClick={() => void act(async () => { await runnerApi.agentResume(); setRes(null); })}>Resume</Btn>}
          <Btn small disabled={busy || !!block} title={block || "Ask the AI now (report only)"} onClick={() => void run("report")}>Run now (report)</Btn>
          <Btn small disabled={busy || !!block} title={block || "Ask the AI now and queue the matching tests"} onClick={() => void run("propose")}>Run now (propose)</Btn>
        </div>
        {block && <p className="mt-1 text-[11px] text-mist">{block}</p>}
        {st.stopped && <Msg kind="warn">{st.stopped.reason || "The stop rule fired."} Work on data and costs instead; Resume starts a fresh count.</Msg>}
        {res && <div className="mt-2 space-y-1" role="status">{runResultLines(res).map((l, i) => <Msg key={i} kind={res.ran ? (l.startsWith("Warning") ? "warn" : "info") : "warn"}>{l}</Msg>)}</div>}
      </Card>

      <Card title="Do its ideas survive their test?" tone={scoreTone(sb)}>
        <p className="text-mist">{scoreLine(sb)}</p>
        <p className="text-[11px] text-mist">Rule: {sb.rule}.</p>
      </Card>

      <Card title={rep ? `Last run: ${rep.date || "-"} (${rep.mode || "-"}, ${rep.trigger || "-"}) at ${whenIst(rep.at)}` : "Last run"}>
        {!rep && <p className="text-mist">The agent has not run yet. After 15:45 IST use Run now (report) once and read the Telegram message.</p>}
        {rep && (
          <div className="space-y-1">
            {rep.note && <Msg kind="info">{rep.note}</Msg>}
            {rep.health.map((x, i) => <Msg key={i} kind="warn">{x}</Msg>)}
            {rep.weaknesses.map((w, i) => <div key={i} className="text-mist">- {w}</div>)}
            {rep.ideas.length === 0 && !rep.note && <p className="text-mist">No ideas that night.</p>}
            {rep.ideas.map((i) => (
              <div key={i.switch} className="rounded-lg border border-slate px-2 py-1">
                <div className="flex flex-wrap items-center gap-2"><Chip>{i.direction === "up" ? "▲ UP" : "▼ DOWN"}</Chip><b>{i.switch}</b></div>
                {i.why && <div className="text-[11px] text-mist">{i.why}</div>}
              </div>
            ))}
            {rep.queued.length > 0 && <p className="text-mist">Queued tests: {Array.from(new Set(rep.queued.map((q) => q.test))).join(", ")}</p>}
            {rep.skipped.length > 0 && <p className="text-[11px] text-mist">Not queued: {rep.skipped.map((s) => `${s.test} (${s.reason})`).join("; ")}</p>}
            {rep.dropped > 0 && <p className="text-[11px] text-mist">{rep.dropped} answer(s) outside the rules were dropped.</p>}
          </div>
        )}
      </Card>
    </div>
  );
}
