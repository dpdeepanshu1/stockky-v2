// Self Improve System > Changes (round 543, E568, S3). What auto-calibration changed: from / to / why, whether it took effect live, which moves are waiting
// for a second agreeing test run, and the last evaluation. Read only. Data: GET /autocalib/status?coverage=false (history, pending, last) and the dry-run banner.
import { bannerLabel, bannerTone, type LoopView } from "../../selfImproveView";
import { changeApplied, changeHeadline, changesSummary, changeTone, changeWord, pendingRows, whenIst } from "../../selfImproveControls";
import { type AutoStatus } from "../../calibrationApi";
import { Card, Chip, Msg } from "../calibration/ui";

export default function ChangesPanel({ loop, ac, acError }: { loop: LoopView; ac: AutoStatus | null; acError: string | null }) {
  const hist = ac?.history ?? loop.autocalib.recent_changes.map((c) => ({ ...c, key: c.title, service: "", applied_live: false, restart_required: !!c.restart_required, trigger: "" }));
  const pend = ac ? pendingRows(ac.pending, ac.rules) : [];
  const last = ac?.last ?? loop.autocalib.last;
  return (
    <div className="space-y-3" data-testid="si-changes">
      {acError && <Msg kind="err">{acError}</Msg>}
      <Card title={`Auto-calibration: ${bannerLabel(loop.banner)}`} tone={bannerTone(loop.banner)}>
        <p className="text-mist">{loop.banner.text}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Chip tone={bannerTone(loop.banner)}>{bannerLabel(loop.banner)}</Chip>
          <Chip>last evaluation: {last ? `${whenIst(last.at)} · changed ${last.changed ?? 0} · waiting ${last.waiting ?? 0}${last.dry_run ? " (dry-run)" : ""}` : "none yet"}</Chip>
        </div>
        {loop.banner.state === "dry_run" && <p className="mt-1 text-[11px] text-mist">In dry-run nothing is applied, so the history below holds only changes made before dry-run was switched on.</p>}
      </Card>

      <Card title="Waiting for a second test run" tone={pend.length ? "warn" : undefined}>
        {!ac && !acError && <p className="text-mist">Loading…</p>}
        {ac && pend.length === 0 && <p className="text-mist">No loosening move is waiting for a second agreeing run.</p>}
        <div className="space-y-1">
          {pend.map((p) => (
            <div key={p.key} className="rounded-lg border border-slate px-2 py-1">
              <div className="flex flex-wrap items-center gap-2"><Chip tone="warn">… WAITING {p.votes}/2</Chip><b>{p.title}</b><span className="text-mist">{p.key} → {p.to}</span></div>
              <div className="text-[11px] text-mist">Loosening a rule needs two different test runs in a row to say the same. Nothing changed yet.</div>
            </div>
          ))}
        </div>
      </Card>

      <Card title="What was changed">
        <p className="text-mist">{changesSummary(hist)}</p>
        <div className="mt-2 space-y-1">
          {hist.map((c, i) => (
            <div key={`${c.at}-${c.key}-${i}`} className="rounded-lg border border-slate px-2 py-1">
              <div className="flex flex-wrap items-center gap-2"><Chip tone={changeTone(c.change)}>{changeWord(c.change)}</Chip><b>{changeHeadline(c)}</b></div>
              <div className="text-[11px] text-mist">{c.reason}</div>
              <div className="text-[11px] text-mist">{changeApplied(c)}{c.trigger ? ` · trigger: ${c.trigger}` : ""}</div>
            </div>
          ))}
        </div>
        {!ac && !acError && <p className="mt-1 text-[11px] text-mist">Showing the last five changes from the loop; the full list is loading.</p>}
      </Card>
    </div>
  );
}
