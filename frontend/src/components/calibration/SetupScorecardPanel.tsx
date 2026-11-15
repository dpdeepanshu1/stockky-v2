// E272: the "Setup scorecard" section of the Real Money Calibration page. READ-ONLY.
// Shows, per entry setup and entry slot, how many settled trades carry that setup stamp, the win rate, the average R (net P&L / rupees at risk at the
// stop) and the 90 % lower bound of that average, from GET /setup/stats on Position Stocks. A cell under the minimum trade count says "not enough trades
// yet" and has no tone, so it can never look like a pass. Until setup_engine exists (E273) every trade reads Shortlist (old rule): that row is the
// baseline the new setups are compared with. Real Trade stamps its entries too (E273) but has no route that scores them, so the card does not call it. Wording and ordering come from
// setupView.ts (setupView.test.mjs). Same fetch / poll / layout pattern as TapePanel.
import { useCallback, useEffect, useState } from "react";
import type { Svc } from "../../calibrationApi";
import { positionStocksApi } from "../../positionStocksApi";
import { scorecardView, setupLadderView, setupName, type SetupStatsDoc } from "../../setupView";
import { setVisibleInterval } from "../../visibleInterval";
import { Btn, Card, Chip, Msg } from "./ui";
import { DataTable, type Column } from "../kit";   // E538 (F3 part 2): the scorecard table is the kit DataTable

const fmt = (v: number | null, suffix = "") => (v == null ? "-" : `${v}${suffix}`);

type ScoreRow = ReturnType<typeof scorecardView>["rows"][number];
const tip = (label: string, title: string) => <span title={title}>{label}</span>;
const num = (r: ScoreRow, k: "winPct" | "avgR" | "lowerR") => (k === "winPct" ? fmt(r.winPct, "%") : k === "avgR" ? fmt(r.avgR) : fmt(r.lowerR));
function scoreColumns(minTrades: number): Column<ScoreRow>[] {
  return [
    { key: "setup", header: "Setup", render: (r) => <span className="text-paper">{r.isAll ? setupName(r.setup) : ""}</span> },
    { key: "slot", header: "Slot", render: (r) => <span className="text-mist">{r.slotLabel}</span> },
    { key: "n", header: tip("Trades", "Settled trades carrying this stamp. A cell needs the minimum before it can be judged."), align: "right",
      render: (r) => <span title={`${r.n} of ${minTrades} trades`}>{r.n}<span className="ml-1 text-[9px] text-mist">{r.progressPct >= 100 ? "" : `${r.progressPct}%`}</span></span> },
    { key: "win", header: "Win %", align: "right", render: (r) => num(r, "winPct") },
    { key: "avgR", header: tip("Avg R", "Average R: net P&L divided by the rupees at risk at the stop."), align: "right",
      render: (r) => <span className={r.avgR != null && r.avgR < 0 ? "text-signal-sell" : ""}>{num(r, "avgR")}</span> },
    { key: "lowerR", header: tip("Lower R", "90% lower bound of the average R. Only a bound above 0 counts as positive."), align: "right", render: (r) => num(r, "lowerR") },
    { key: "verdict", header: "Verdict", render: (r) => (r.tone ? <Chip tone={r.tone}>{r.verdictText}</Chip> : <span className="text-mist">{r.verdictText}</span>) },
  ];
}

export default function SetupScorecardPanel({ svc }: { svc: Svc }) {
  const [doc, setDoc] = useState<SetupStatsDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const load = useCallback(async () => {
    if (svc !== "ps") { setLoaded(true); return; }
    try { setDoc(await positionStocksApi.setupStats()); setErr(null); }
    catch (e: any) { setDoc(null); setErr(`setup stats: ${e?.message || e}`); }
    setLoaded(true);
  }, [svc]);
  useEffect(() => { setLoaded(false); void load(); return setVisibleInterval(() => void load(), 60000); }, [load]);

  if (svc !== "ps") {
    return (
      <div className="space-y-3" data-testid="setup-panel">
        <Card title="Setup scorecard (Real Trade)">
          <p className="text-[13px]" data-testid="setup-rt-note">Real Trade stamps each entry with its setup too, but no route reads those stamps back yet, so the scorecard is Position Stocks only. Choose Position Stocks Service above.</p>
        </Card>
      </div>
    );
  }
  const v = scorecardView(doc);
  const lad = setupLadderView(doc);
  return (
    <div className="space-y-3" data-testid="setup-panel">
      {err && <Msg kind="err">{err}</Msg>}
      <Card title="Setup ladder" right={<Chip tone={lad.ladderOn === false ? "warn" : undefined} title="Display only. The ladder itself is the SLOT_LADDER_ENABLED switch.">{lad.ladderOn === false ? "ladder off" : "read-only"}</Chip>}>
        <p className="text-[12px] text-mist" data-testid="ladder-words">{lad.words}</p>
        <ol className="w-setups mt-2" data-testid="setup-ladder">
          {lad.cards.map((c) => (
            <li key={c.id} className={`w-setup ${c.stage}`} title={c.why}>
              <span className="w-setup-id">{c.short}</span>
              <span className="w-setup-body">
                <b>{c.name}</b>
                <small>{c.side} · from {c.from} · {c.start}</small>
                <small className="w-setup-stage">{c.stageText}</small>
              </span>
            </li>
          ))}
        </ol>
        {lad.legacyClosed > 0 && <p className="mt-2 text-[11px] text-mist">{lad.legacyClosed} settled trade{lad.legacyClosed === 1 ? "" : "s"} still carry the old shortlist rule; that row is the baseline the setups are compared with.</p>}
      </Card>
      <Card title="Setup scorecard (Position Stocks)" tone={v.tone === "good" ? "good" : v.tone === "bad" ? "bad" : v.tone === "warn" ? "warn" : undefined}
        right={<Chip title="This card only reads. It never changes a switch or a trade.">read-only</Chip>}>
        {!loaded && <p className="text-mist">Loading…</p>}
        {loaded && !doc && !err && <p className="text-mist">No numbers yet.</p>}
        {doc && <p className="text-[13px]" data-testid="setup-headline">{v.headline}</p>}
        {v.rows.length > 0 && (
          <div className="mt-2" data-testid="setup-table">
            <DataTable dense columns={scoreColumns(v.minTrades)} rows={v.rows} rowKey={(r) => r.key} rowClass={(r) => (r.isAll ? "font-bold" : "")} />
          </div>
        )}
        {v.warnings.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-[12px]" data-testid="setup-warnings">{v.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
        <div className="mt-2 flex flex-wrap items-center gap-2"><Btn small title="Reads the numbers again." onClick={() => void load()}>Refresh</Btn></div>
        <p className="mt-2 text-[11px] text-mist">The row marked Shortlist (old rule) is how entries were taken before the new setups; every new setup is compared with it. A setup is called positive only when its 90% lower bound of the average R is above 0 with at least {v.minTrades} trades; the standard error is taken across session days when 5 or more days exist. Position rows are kept only a few days, so save these numbers if you want a cell to grow.</p>
      </Card>
    </div>
  );
}
