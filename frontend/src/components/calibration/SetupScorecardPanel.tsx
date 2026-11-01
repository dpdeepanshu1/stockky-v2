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

const fmt = (v: number | null, suffix = "") => (v == null ? "-" : `${v}${suffix}`);

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
        <ol className="v8-setups mt-2" data-testid="setup-ladder">
          {lad.cards.map((c) => (
            <li key={c.id} className={`v8-setup ${c.stage}`} title={c.why}>
              <span className="v8-setup-id">{c.short}</span>
              <span className="v8-setup-body">
                <b>{c.name}</b>
                <small>{c.side} · from {c.from} · {c.start}</small>
                <small className="v8-setup-stage">{c.stageText}</small>
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
          <div className="mt-2 overflow-x-auto">
            <table className="w-full text-[11px] tabular-nums" data-testid="setup-table">
              <thead>
                <tr className="border-b border-slate text-left text-mist">
                  <th className="py-1 pr-3">Setup</th>
                  <th className="py-1 pr-3">Slot</th>
                  <th className="py-1 pr-3" title="Settled trades carrying this stamp. A cell needs the minimum before it can be judged.">Trades</th>
                  <th className="py-1 pr-3">Win %</th>
                  <th className="py-1 pr-3" title="Average R: net P&L divided by the rupees at risk at the stop.">Avg R</th>
                  <th className="py-1 pr-3" title="90% lower bound of the average R. Only a bound above 0 counts as positive.">Lower R</th>
                  <th className="py-1">Verdict</th>
                </tr>
              </thead>
              <tbody>
                {v.rows.map((r) => (
                  <tr key={r.key} className={`border-b border-slate/50 ${r.isAll ? "font-bold" : ""}`} data-testid="setup-row">
                    <td className="py-1 pr-3 text-paper">{r.isAll ? setupName(r.setup) : ""}</td>
                    <td className="py-1 pr-3 text-mist">{r.slotLabel}</td>
                    <td className="py-1 pr-3" title={`${r.n} of ${v.minTrades} trades`}>{r.n}<span className="ml-1 text-[9px] text-mist">{r.progressPct >= 100 ? "" : `${r.progressPct}%`}</span></td>
                    <td className="py-1 pr-3">{fmt(r.winPct, "%")}</td>
                    <td className={`py-1 pr-3 ${r.avgR != null && r.avgR < 0 ? "text-signal-sell" : ""}`}>{fmt(r.avgR)}</td>
                    <td className="py-1 pr-3">{fmt(r.lowerR)}</td>
                    <td className="py-1">{r.tone ? <Chip tone={r.tone}>{r.verdictText}</Chip> : <span className="text-mist">{r.verdictText}</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {v.warnings.length > 0 && <ul className="mt-2 list-disc space-y-1 pl-5 text-[12px]" data-testid="setup-warnings">{v.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>}
        <div className="mt-2 flex flex-wrap items-center gap-2"><Btn small title="Reads the numbers again." onClick={() => void load()}>Refresh</Btn></div>
        <p className="mt-2 text-[11px] text-mist">The row marked Shortlist (old rule) is how entries were taken before the new setups; every new setup is compared with it. A setup is called positive only when its 90% lower bound of the average R is above 0 with at least {v.minTrades} trades; the standard error is taken across session days when 5 or more days exist. Position rows are kept only a few days, so save these numbers if you want a cell to grow.</p>
      </Card>
    </div>
  );
}
