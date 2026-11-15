// Self Improve System > Ideas (round 542, E567). Last saved night in full, then one line per saved day with each idea's scoreboard outcome.
// An idea whose own rule says "no change" is labelled so: read it as unproven. Data: GET /agent/history. Read only.
import { dayLine, directionText, historyTotals, outcomeText, outcomeTone, supportText, supportTone, totalsLine, whyLine, type HistoryDay, type HistoryView } from "../../selfImproveView";
import { whenIst } from "../../agentView";
import { Card, Chip, Msg } from "../calibration/ui";

function IdeaRows({ day }: { day: HistoryDay }) {
  const ideas = day.ideas || [];
  return (
    <div className="space-y-1">
      {day.unreadable && <Msg kind="warn">The saved file of this day could not be read.</Msg>}
      {day.note && <Msg kind="info">{day.note}</Msg>}
      {(day.health || []).map((h, i) => <Msg key={i} kind="warn">{h}</Msg>)}
      {(day.errors_n ?? 0) > 0 && <Msg kind="warn">{day.errors_n} error(s) were saved with this run (an LLM call failed or answered badly).</Msg>}
      {(day.weaknesses || []).map((w, i) => <div key={i} className="text-mist">- {w}</div>)}
      {ideas.length === 0 && !day.note && !day.unreadable && <p className="text-mist">No ideas that night.</p>}
      {ideas.map((i) => (
        <div key={i.switch} className="rounded-lg border border-slate px-2 py-1">
          <div className="flex flex-wrap items-center gap-2">
            <Chip>{directionText(i.direction)}</Chip><b>{i.switch}</b>
            <Chip tone={outcomeTone(i.outcome)}>{outcomeText(i.outcome)}</Chip>
            {supportText(i.support) && <Chip tone={supportTone(i.support)}>{supportText(i.support)}</Chip>}
            {i.queued_test && <Chip>test queued: {i.queued_test}</Chip>}
          </div>
          {i.why && <div className="text-[11px] text-mist">{i.why}</div>}
        </div>
      ))}
      {(day.dropped || []).length > 0 && (
        <div className="rounded-lg border border-slate px-2 py-1">
          <div className="text-[11px] font-bold uppercase tracking-widest text-mist">Dropped by the rules ({(day.dropped || []).length})</div>
          {(day.dropped || []).map((d, i) => <div key={i} className="text-[11px] text-mist"><b>{d.switch}</b>: {d.reason}</div>)}
        </div>
      )}
      {(day.skipped_n ?? 0) > 0 && <p className="text-[11px] text-mist">{day.skipped_n} test(s) were not queued.</p>}
      {day.auto_calibration && <p className="text-[11px] text-mist">{day.auto_calibration}</p>}
    </div>
  );
}

export default function IdeasPanel({ history }: { history: HistoryView | null }) {
  if (!history) return <div className="text-mist">Loading the ideas…</div>;
  const days = history.days;
  const latest = days[0] || null;
  return (
    <div className="space-y-3" data-testid="si-ideas">
      <Card title={latest ? `Last saved run: ${latest.date} (${latest.mode || "-"}, ${latest.trigger || "-"}${latest.at ? ` at ${whenIst(latest.at)}` : ""})` : "Last saved run"}>
        {!latest && <p className="text-mist">The agent has not saved a run yet. After 15:45 IST use Run now (report) on the Today / Loop page.</p>}
        {latest && (
          <>
            <p className="mb-2 text-mist">{latest.unreadable ? "" : whyLine(latest.breakdown)}</p>
            <IdeaRows day={latest} />
          </>
        )}
      </Card>

      <Card title={`History (${history.count} day${history.count === 1 ? "" : "s"})`}>
        <p className="text-mist">{totalsLine(historyTotals(days))}</p>
        {history.note && <p className="text-[11px] text-mist">Note: {history.note}.</p>}
        <div className="mt-2 space-y-1">
          {days.map((d) => (
            <details key={d.date} className="rounded-lg border border-slate px-2 py-1">
              <summary className="cursor-pointer">{dayLine(d)}{(d.ideas || []).length > 0 ? ` · ${(d.ideas || []).map((i) => `${i.switch} ${i.direction} (${outcomeText(i.outcome)})`).join("; ")}` : ""}</summary>
              <div className="mt-2"><IdeaRows day={d} /></div>
            </details>
          ))}
        </div>
      </Card>
    </div>
  );
}
