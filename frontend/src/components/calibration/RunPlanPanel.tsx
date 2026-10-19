// group 442: the written plan (Monday / daily / Friday / Saturday / monthly) with a button on every line, what already runs by itself, the optional timetable
// and the gate glossary. Same content on both service tabs; jobs that concern only the other service are hidden there.
import { useCallback, useEffect, useState } from "react";
import { relTime, runnerApi, type ScheduleSlot, type Svc } from "../../calibrationApi";
import { AUTOMATIC_ELSEWHERE, GATE_GLOSSARY, PLAN, jobAppliesTo } from "../../calibrationPlan";
import { Btn, Card, Msg, StatusPill } from "./ui";
import { useRunner } from "./RunnerContext";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export default function RunPlanPanel({ svc }: { svc: Svc }) {
  const { catalog, summary, start } = useRunner();
  const [sched, setSched] = useState<{ enabled: boolean; entries: ScheduleSlot[]; last_fired: Record<string, string> } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);

  const loadSched = useCallback(async () => { try { setSched(await runnerApi.schedule()); } catch (e: any) { setMsg(e?.message || String(e)); } }, []);
  useEffect(() => { void loadSched(); }, [loadSched]);

  const saveSched = async (body: { enabled?: boolean; entries?: ScheduleSlot[] }) => {
    try { setSched(await runnerApi.putSchedule(body)); setMsg(null); } catch (e: any) { setMsg(e?.message || String(e)); }
  };

  if (!catalog) return <div className="text-mist">Loading the plan…</div>;
  const jobTitle = (id: string) => catalog.jobs.find((j) => j.id === id)?.title || id;
  const packOf = (id: string) => catalog.packs.find((p) => p.id === id);

  return (
    <div className="space-y-3" data-testid={`plan-${svc}`}>
      <Card title="How to use this page">
        <ol className="list-decimal space-y-1 pl-5">
          <li>Press <b>Run</b> on a line below. The run joins a queue (one at a time, so it never starves the trading containers) and its output is kept as a txt file.</li>
          <li>Open the run to watch it live, then <b>download the txt</b> (or the whole run as a zip with the JSON / CSV files).</li>
          <li><b>OK</b> = ran fine. <b>LOOK</b> = it ran and its own verdict says look (FAIL / NO_GO / NOT_ENOUGH_DATA). <b>FAILED</b> = it did not run properly, read the txt.</li>
          <li>Nothing here switches a trading setting. Changing a switch is a separate, confirmed action on the Switches section.</li>
        </ol>
      </Card>

      {PLAN.map((sec) => (
        <Card key={sec.id} title={sec.title}>
          <p className="mb-2 text-mist">{sec.blurb}</p>
          <ul className="space-y-2">
            {sec.rows.map((row, i) => {
              const jobs = (row.jobs || []).filter((j) => jobAppliesTo(j, svc));
              const pack = row.pack ? packOf(row.pack) : undefined;
              const ps = pack ? summary?.packs[pack.id] : null;
              return (
                <li key={i} className="rounded-xl border border-slate p-2">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="font-bold">{row.when}</div>
                      <div>{row.what}</div>
                      {row.note && <div className="text-[11px] text-mist">{row.note}</div>}
                      {pack && <div className="mt-1 text-[11px] text-mist">Pack: {pack.jobs.map(([id]) => jobTitle(id)).join(" → ")}</div>}
                    </div>
                    <div className="flex shrink-0 flex-col items-end gap-1">
                      {pack && <><Btn primary small onClick={() => void start("pack", pack.id)}>Run pack</Btn><span className="text-[10px] text-mist"><StatusPill status={ps?.status || "never"} /> {ps ? relTime(ps.finished_at) : ""}</span></>}
                      {jobs.map((j) => <Btn key={j} small onClick={() => void start("job", j, {})}>{jobTitle(j)}</Btn>)}
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </Card>
      ))}

      <Card title="Already automatic (GitHub Actions)">
        <ul className="space-y-1">{AUTOMATIC_ELSEWHERE.map((a) => <li key={a.what}><b>{a.what}</b> · {a.when} · <span className="text-mist">{a.how}</span></li>)}</ul>
      </Card>

      <Card title="Optional timetable (IST)" right={sched && <Btn small primary={sched.enabled} onClick={() => void saveSched({ enabled: !sched.enabled })}>{sched.enabled ? "Timetable ON: switch off" : "Timetable OFF: switch on"}</Btn>}>
        <p className="mb-2 text-mist">When ON, the runner starts these packs by itself at the time shown. A slot missed while the runner was down is skipped, not caught up. Heavy packs that fall inside market hours are recorded as blocked.</p>
        {msg && <Msg kind="err">{msg}</Msg>}
        {sched && (
          <table className="w-full text-[11px]"><thead className="text-left text-mist"><tr><th>On</th><th>Pack</th><th>Days</th><th>Time</th><th>Last fired</th></tr></thead>
            <tbody>
              {sched.entries.map((e) => (
                <tr key={e.id} className="border-t border-slate">
                  <td className="py-1"><input type="checkbox" aria-label={`${e.id} enabled`} checked={e.enabled} onChange={(ev) => void saveSched({ entries: sched.entries.map((x) => (x.id === e.id ? { ...x, enabled: ev.target.checked } : x)) })} /></td>
                  <td>{packOf(e.pack)?.title || e.pack}{e.first_week_only ? " (first week)" : ""}</td><td>{e.days.map((d) => DAYS[d]).join(" ")}</td><td>{e.time}</td><td className="text-mist">{sched.last_fired[e.id] || "-"}</td>
                </tr>
              ))}
            </tbody></table>
        )}
      </Card>

      <Card title="Gate glossary: what G1 … means in this repo">
        <ul className="space-y-1">{GATE_GLOSSARY.map((g) => (
          <li key={g.gate} className={g.exists ? "" : "text-signal-hold"}><b>{g.gate}</b> · {g.means} <span className="text-mist">{g.exists ? `· ${g.job}` : ""}</span></li>
        ))}</ul>
      </Card>
    </div>
  );
}
