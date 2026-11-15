// Self Improve System > Scoreboard & health (round 542, E567). Does the agent's idea survive its own test? Is it running? Are the keys and the call cap fine?
// Also the settings in force with their audit lines (read only here; they are changed under Controls, round 543). A key is never shown, only "set" or "NOT set".
import { useState } from "react";
import { runnerApi } from "../../calibrationApi";
import { keyLine, scoreLine, scoreTone, usageLine, whenIst } from "../../agentView";
import { auditText, healthWarnings, settingLines, type LoopView } from "../../selfImproveView";
import { Btn, Card, Chip, Msg } from "../calibration/ui";

export default function HealthPanel({ loop, reload }: { loop: LoopView; reload: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const a = loop.agent;
  const warns = healthWarnings(loop);
  const resume = async () => {
    setBusy(true);
    try { await runnerApi.agentResume(); setErr(null); await reload(); } catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };
  return (
    <div className="space-y-3" data-testid="si-health">
      {err && <Msg kind="err">{err}</Msg>}
      <Card title="Needs a person" tone={warns.length ? "warn" : "good"}>
        {warns.length === 0 && <p className="text-mist">Nothing needs a person right now.</p>}
        {warns.map((w, i) => <Msg key={i} kind="warn">{w}</Msg>)}
        {a.stopped && <div className="mt-2"><Btn small primary disabled={busy} onClick={() => void resume()}>Resume</Btn></div>}
      </Card>

      <Card title="Do its ideas survive their test?" tone={scoreTone(a.scoreboard)}>
        <p className="text-mist">{scoreLine(a.scoreboard)}</p>
        <p className="text-[11px] text-mist">Rule: {a.scoreboard.rule}.</p>
        {a.stopped && <Msg kind="warn">Switched itself off on {a.stopped.date || "an earlier day"}: {a.stopped.reason || "the stop rule fired"}.</Msg>}
      </Card>

      <Card title="Keys and calls">
        <div className="flex flex-wrap items-center gap-2">
          <Chip tone={a.keys.gemini || a.keys.groq ? undefined : "bad"}>{keyLine(a.keys)}</Chip>
          <Chip tone={a.usage_today.left <= 0 ? "bad" : a.usage_today.gemini_blocked ? "warn" : undefined}>{usageLine(a.usage_today)}</Chip>
        </div>
        {a.last && <p className="mt-2 text-[11px] text-mist">Last run: {a.last.date || "-"} ({a.last.mode || "-"}, {a.last.trigger || "-"}){a.last.at ? ` at ${whenIst(a.last.at)}` : ""}{a.last.source ? ` · answered by ${a.last.source}` : ""}{a.last.note ? ` · ${a.last.note}` : ""}</p>}
      </Card>

      <Card title="Settings in force (read only here; change them under Controls)">
        <div className="space-y-1">{settingLines(a.settings).map((l) => <div key={l} className="rounded-lg border border-slate px-2 py-1">{l}</div>)}</div>
        <p className="mt-1 text-[11px] text-mist">Allowed: mode {a.settings.bounds.modes.join(" / ")}; run-after {a.settings.bounds.run_after.join(" to ")} IST; max ideas {a.settings.bounds.max_ideas.join(" to ")}; max queued tests {a.settings.bounds.max_queue.join(" to ")}.</p>
        <div className="mt-2 text-[11px] font-bold uppercase tracking-widest text-mist">Last changes</div>
        {a.settings.audit.length === 0 && <p className="text-mist">No setting was changed from the page or API yet.</p>}
        {a.settings.audit.map((x, i) => <div key={i} className="text-[11px] text-mist">{auditText(x)}</div>)}
      </Card>
    </div>
  );
}
