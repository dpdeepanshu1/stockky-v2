// Part 1 item 7a: the "Short selling" panel next to Market Weather. SHADOW ONLY: every row is a short Stockky WOULD have taken, no order exists.
// Shows the shadow counters and the S2 numbers (shortShadowView), the position-stocks "Short selling" switches, a one-click SHORT_MODE off, and a button for
// the short_shadow_report job. The numbers come from position-stocks GET /short/shadow; the switches from its /control/flags. Covered by shortShadow.test.mjs.
import { useCallback, useEffect, useState } from "react";
import { controlApi, type FlagRow } from "../../calibrationApi";
import { positionStocksApi } from "../../positionStocksApi";
import { canSwitchShortOff, rupees, sellProbeView, shortShadowView, shortSwitchRows, type ShortShadowDoc } from "../../shortShadowView";
import { shortModeView } from "../../positionSide";
import { useRunner } from "./RunnerContext";
import { Btn, Card, Chip, Msg } from "./ui";

export default function ShortSellingPanel() {
  const { start } = useRunner();
  const [doc, setDoc] = useState<ShortShadowDoc | null>(null);
  const [flags, setFlags] = useState<FlagRow[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [info, setInfo] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    const errs: string[] = [];
    try { setDoc(await positionStocksApi.shortShadow()); } catch (e: any) { errs.push(`shadow shorts: ${e?.message || e}`); }
    try { setFlags((await controlApi.flags("ps")).flags); } catch (e: any) { errs.push(`switches: ${e?.message || e}`); }
    setErr(errs.length ? errs.join(" · ") : null);
  }, []);
  useEffect(() => { void load(); const id = setInterval(() => void load(), 30000); return () => clearInterval(id); }, [load]);

  const v = shortShadowView(doc);
  const rows = shortSwitchRows(flags);
  const mv = shortModeView(rows.find((r) => r.key === "SHORT_MODE")?.value ?? doc?.mode);
  const off = async () => {
    if (!window.confirm(mv.orders ? "Set SHORT_MODE to off? No new short will be placed. A short already open keeps its broker stop and is still covered by 14:55; use Close all to cover it now." : "Set SHORT_MODE to off? The shadow shorts stop being recorded. No order exists either way.")) return;
    setBusy(true);
    try { const r = await controlApi.set("ps", "SHORT_MODE", "off", true, "calibration page: one-click short off"); setInfo(`SHORT_MODE: ${r.change}${r.applied_live ? " (applied now)" : ""}`); setErr(null); await load(); }
    catch (e: any) { setErr(e?.message || String(e)); } finally { setBusy(false); }
  };

  return (
    <div className="space-y-3" data-testid="short-selling">
      {err && <Msg kind="err">{err}</Msg>}
      <Card title={mv.title} tone={mv.tone === "bad" ? "bad" : "warn"} right={<Chip tone={mv.tone === "bad" ? "bad" : "warn"} title={mv.orders ? "Real short orders can be placed, capped by SHORT_LIVE_QTY_CAP. The rows below are older shadow records." : "Every row is a short Stockky WOULD have taken. No order is placed."}>{mv.orders ? "real orders" : "no order is placed"}</Chip>}>
        <p className="text-[13px]" data-testid="short-headline">{v.headline}</p>
        {doc && (
          <div className="mt-2 flex flex-wrap items-center gap-2" data-testid="short-counters">
            <Chip>mode {doc.mode}</Chip><Chip>{doc.open} open</Chip><Chip>{doc.closed} closed</Chip><Chip>{doc.target} target</Chip><Chip>{doc.stop} stop</Chip><Chip>{doc.eod} EOD</Chip>
            <Chip tone={doc.net_pnl > 0 ? "good" : doc.net_pnl < 0 ? "bad" : undefined} title="after charges">net {rupees(doc.net_pnl)}</Chip>
            {doc.win_rate_pct != null && <Chip>win rate {doc.win_rate_pct}%</Chip>}
          </div>
        )}
        {(() => { const sp = sellProbeView(doc?.sell_probe); return <div className="mt-2 text-[12px]" data-testid="short-probe"><Chip tone={sp.tone}>{sp.tone === "good" ? "probe PASS" : sp.tone === "bad" ? "probe FAIL" : "probe not run"}</Chip> {sp.text}</div>; })()}
        <div className="mt-2 text-[12px]" data-testid="short-s2">S2: {v.progress.done} of {v.progress.needed} would-be shorts, {v.progress.days} of {v.progress.daysNeeded} days.{" "}
          {v.s2Ready ? "The numbers pass; check the result is not from one or two days." : v.s2Missing.join("; ")}</div>
        {v.refusals.length > 0 && <div className="mt-1 text-[11px] text-mist" data-testid="short-refusals">Gate said no: {v.refusals.map((r) => `${r.code} ${r.count}`).join(", ")}</div>}
      </Card>

      <Card title="Short selling switches (Position Stocks)">
        {rows.length === 0 ? <p className="text-mist">No short switches loaded.</p> : (
          <div className="space-y-1" data-testid="short-switches">
            {rows.map((r) => (
              <div key={r.key} className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate px-2 py-1">
                <span>{r.title} <span className="text-[11px] text-mist">{r.key}</span></span>
                <span className="flex items-center gap-2"><b>{r.value}</b>{r.pending && <Chip tone="warn">restart pending</Chip>}</span>
              </div>
            ))}
          </div>
        )}
        <div className="mt-2 flex flex-wrap items-center gap-2">
          <Btn small disabled={busy || !canSwitchShortOff(rows)} title="Sets SHORT_MODE to off (asks to confirm). Never turns anything on." onClick={() => void off()}>SHORT_MODE off</Btn>
          <Btn small disabled={busy} title="Reads the shadow shorts and judges the numbers. Changes nothing." onClick={() => void start("job", "short_shadow_report", {})}>Run shadow report</Btn>
          <Btn small disabled={busy} title="Shows the last Dhan SELL probe result. Places nothing; the probe itself is started from the Run tab with its confirm box." onClick={() => void start("job", "dhan_sell_probe", { status_only: true })}>Show last probe</Btn>
        </div>
        {info && <div className="mt-2"><Msg kind="info">{info}</Msg></div>}
        <p className="mt-2 text-[11px] text-mist">There is no switch here that turns shorting on or up. Moving SHORT_MODE to shadow or live, and raising SHORT_LIVE_QTY_CAP, is done on the Switches tab (it asks to confirm).</p>
      </Card>
    </div>
  );
}
