// frontend/src/components/SelfImproveTab.tsx  (round 542, E567, S2 of the Self Improve plan)
// "Self Improve System": ONE place for the nightly loop (tests -> AI agent -> queued tests -> auto-calibration). Five sections: Today / Loop, Ideas, Changes,
// Controls (round 543, S3), Scoreboard & health. It reads GET /agent/loop, /agent/history, /agent/weekly (round 545) and /autocalib/status?coverage=false on calibration-runner-service
// and refreshes every 30 s. The agent only SUGGESTS: nothing here sets a trading switch; Controls changes only the agent's own settings and auto-calibration's
// master switch, dry-run and pinned list. The page sits behind the shared admin login (AdminGate, TabContent.tsx). Pure helpers: selfImproveView.ts, selfImproveControls.ts.
import { useCallback, useEffect, useState } from "react";
import { runnerApi } from "../calibrationApi";
import type { AutoStatus } from "../calibrationApi";
import type { HistoryView, LoopView, SiSection, WeeklyView } from "../selfImproveView";
import SiNav from "./selfImprove/SiNav";
import TodayPanel from "./selfImprove/TodayPanel";
import IdeasPanel from "./selfImprove/IdeasPanel";
import HealthPanel from "./selfImprove/HealthPanel";
import ChangesPanel from "./selfImprove/ChangesPanel";
import ControlsPanel from "./selfImprove/ControlsPanel";
import { Msg } from "./calibration/ui";

export default function SelfImproveTab() {
  const [section, setSection] = useState<SiSection>("today");
  const [loop, setLoop] = useState<LoopView | null>(null);
  const [history, setHistory] = useState<HistoryView | null>(null);
  const [weekly, setWeekly] = useState<WeeklyView | null>(null);
  const [ac, setAc] = useState<AutoStatus | null>(null);
  const [acErr, setAcErr] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reload = useCallback(async () => {
    // the reads are independent: one failing must not blank the others (coverage=false: no call to the two trading services). The weekly review (round 545) is optional: an older runner answers 404 and the card is simply not shown.
    const [l, h, a, wk] = await Promise.allSettled([runnerApi.agentLoop(), runnerApi.agentHistory(30), runnerApi.autoStatus(false), runnerApi.agentWeekly()]);
    if (wk.status === "fulfilled") setWeekly(wk.value);
    if (l.status === "fulfilled") setLoop(l.value);
    if (h.status === "fulfilled") setHistory(h.value);
    if (a.status === "fulfilled") { setAc(a.value); setAcErr(null); } else setAcErr(a.reason?.message || String(a.reason));
    const bad = [l, h].find((x) => x.status === "rejected") as PromiseRejectedResult | undefined;
    setErr(bad ? (bad.reason?.message || String(bad.reason)) : null);
  }, []);
  useEffect(() => {
    void reload();
    const id = setInterval(() => { if (!document.hidden) void reload(); }, 30000);
    return () => clearInterval(id);
  }, [reload]);

  return (
    <div className="hx-page hx-flow space-y-3" data-testid="self-improve">
      <div className="cx-layout">
        <SiNav value={section} onChange={setSection} />
        <div className="cx-main space-y-3">
          {err && <Msg kind="err">{err}</Msg>}
          {!loop && !err && <div className="text-mist" role="status">Loading the loop…</div>}
          {loop && section === "today" && <TodayPanel loop={loop} latest={history?.days[0] ?? null} runs={history?.runs} reload={reload} />}
          {section === "ideas" && <IdeasPanel history={history} weekly={weekly} />}
          {loop && section === "changes" && <ChangesPanel loop={loop} ac={ac} acError={acErr} />}
          {loop && section === "controls" && <ControlsPanel loop={loop} ac={ac} acError={acErr} reload={reload} />}
          {loop && section === "health" && <HealthPanel loop={loop} reload={reload} />}
        </div>
      </div>
    </div>
  );
}
