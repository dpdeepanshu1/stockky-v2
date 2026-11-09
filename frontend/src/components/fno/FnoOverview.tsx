// F&O Options > Overview: the four locks, today's numbers, open positions, the pipeline check, and the manual buttons.
import { useState } from "react";
import { fnoApi, type FnoPosition } from "../../fnoApi";
import { inr, istHm, lockSummary, positionProgress } from "../../fnoView";
import { Btn, Card, Chip, Msg, pnlClass } from "../calibration/ui";
import { useFno } from "./useFno";
import FnoControlCard from "./FnoControlCard";
import FnoProbeCard from "./FnoProbeCard";
import FnoReadinessCard from "./FnoReadinessCard";
import FnoStagesRow from "./FnoStagesRow";

export function PositionCard({ p }: { p: FnoPosition }) {
  const { pct, pnl } = positionProgress(p);
  return (
    <div className="rounded-xl border border-slate p-2 text-[12px]">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <b>{p.label}</b>{p.strategy && p.strategy !== "CORE" && <Chip>{p.strategy}</Chip>}
        <span className="text-mist">{p.lots} lot x {p.lot_size} · {p.mode} · in {istHm(p.entry_ts)}</span>
      </div>
      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1">
        <span>entry {p.entry_premium.toFixed(2)}</span>
        <span>now {p.premium != null ? p.premium.toFixed(2) : "-"}</span>
        <span>SL {p.sl_price.toFixed(2)}</span>
        <span>target {p.target_price.toFixed(2)}</span>
        {p.trail_armed && <Chip tone="good">trail {p.trail_stop?.toFixed(2)}</Chip>}
        <span className={pnlClass(pnl)}>P&amp;L {inr(pnl)}</span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded bg-white/10" title="stop ... target"><div className="h-1.5 rounded bg-signal-buy" style={{ width: `${pct}%` }} /></div>
    </div>
  );
}

export default function FnoOverview() {
  const ov = useFno(fnoApi.overview, 5000);
  const pipe = useFno(fnoApi.pipeline, 0);
  const rec = useFno(fnoApi.reconcile, 0);
  const strat = useFno(fnoApi.strategies, 30000);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const act = async (fn: () => Promise<unknown>, okText: string, confirmText?: string) => {
    if (confirmText && !window.confirm(confirmText)) return;
    setBusy(true);
    try { await fn(); if (okText) setMsg({ kind: "ok", text: okText }); ov.reload(); } catch (e: any) { setMsg({ kind: "err", text: String(e?.message || e) }); } finally { setBusy(false); }
  };
  const d = ov.data;
  const ls = lockSummary(d?.locks);
  return (
    <div className="space-y-3">
      {ov.error && <Msg kind="err">{ov.error}</Msg>}
      {msg && <Msg kind={msg.kind}>{msg.text}</Msg>}
      <Card title="State" tone={ls.tone === "real" ? "bad" : ls.tone === "real-blocked" ? "warn" : undefined}
        right={<><Chip tone={d?.market_open ? "good" : undefined}>{d?.market_open ? "Market open" : "Market closed"}</Chip><Chip tone={d?.runner.alive ? "good" : "warn"}>{d?.runner.alive ? "runner alive" : "runner stopped"}</Chip></>}>
        <p className="text-[13px] font-bold">{ls.text}</p>
        {d && (
          <ul className="mt-2 grid grid-cols-2 gap-1 text-[11px] text-mist sm:grid-cols-4">
            <li>Engine: <b>{d.locks.engine_enabled ? "ON" : "OFF"}</b></li>
            <li>Mode: <b>{d.locks.mode}</b></li>
            <li>REAL orders allowed: <b>{d.locks.real_enabled ? "yes" : "no"}</b></li>
            <li>Armed today: <b>{d.locks.armed_today ? "yes" : "no"}</b></li>
          </ul>
        )}
        <p className="mt-2 text-[11px] text-mist">The locks and strategies are switched in the Control card below; risk, entry window and exits on Real Money Calibration &gt; F&amp;O. Ships OFF and DEMO.</p>
        {d?.runner.error && <div className="mt-2"><Msg kind="warn">Runner error: {d.runner.error}</Msg></div>}
      </Card>

      <FnoControlCard onChanged={() => { ov.reload(); strat.reload(); }} />

      <Card title="Today" right={<Btn small disabled={busy || !d?.open_positions.length} onClick={() => void act(fnoApi.squareoff, "Square-off sent.", "Close ALL open F&O positions now?")}>Square off all</Btn>}>
        {d ? (
          <ul className="grid grid-cols-2 gap-2 sm:grid-cols-5">
            <li>Trades <b>{d.day.trades}</b></li><li>Open <b>{d.day.open}</b></li>
            <li>Wins / losses <b>{d.day.wins} / {d.day.losses}</b></li>
            <li>Charges <b>{inr(d.day.charges)}</b></li>
            <li>Net <b className={pnlClass(d.day.realized_net)}>{inr(d.day.realized_net)}</b></li>
          </ul>
        ) : <p className="text-mist">Loading…</p>}
      </Card>

      <Card title="Open positions">
        {d && d.open_positions.length === 0 && <p className="text-mist">No open F&amp;O position.</p>}
        <div className="space-y-2">{d?.open_positions.map((p) => <PositionCard key={p.id ?? p.label} p={p} />)}</div>
      </Card>

      <FnoReadinessCard onChanged={ov.reload} />

      <FnoProbeCard />

      <Card title="REAL arm (daily)" tone="warn">
        <p className="mb-2 text-mist">REAL orders need all four: engine ON, mode REAL, REAL orders ON and this per-day arm, plus Dhan credentials. The arm expires at midnight IST. Leave this alone until DEMO evidence exists.</p>
        <div className="flex gap-2">
          <Btn small disabled={busy} onClick={() => void act(async () => { const r = await fnoApi.arm(); setMsg({ kind: r.credentials?.startsWith("no Dhan") ? "err" : "ok", text: `Armed for today. Dhan credentials: ${r.credentials}` }); }, "", "Arm REAL orders for today? Real money can be used if every other lock is open.")}>Arm today</Btn>
          <Btn small disabled={busy} onClick={() => void act(fnoApi.disarm, "Disarmed.")}>Disarm</Btn>
        </div>
      </Card>

      <Card title="Strategies" right={<Chip>{strat.data?.mode || "DEMO"}</Chip>}>
        {strat.error && <Msg kind="err">{strat.error}</Msg>}
        <p className="mb-2 text-mist">{strat.data?.note}</p>
        {strat.data?.expiry_rules && <p className="mb-2 text-mist">Expiry-day strategies: entry until {strat.data.expiry_rules.entry_end}, out by {strat.data.expiry_rules.squareoff_at}, {strat.data.expiry_rules.risk_mult}x the normal risk, one lot; REAL {strat.data.expiry_rules.real_allowed ? "allowed (env switch is ON)" : "not allowed"}.</p>}
        <ul className="space-y-1">
          {strat.data?.strategies.map((s) => (
            <li key={s.key} className="rounded-lg border border-slate p-2 text-[12px]">
              <div className="flex flex-wrap items-center gap-2"><Chip tone={s.enabled ? "good" : undefined}>{s.enabled ? "ON" : "OFF"}</Chip><b>{s.label}</b>{s.expiry_day_only && <Chip tone="warn">EXPIRY DAY · DEMO only</Chip>}<span className="text-mist">{s.sides}{s.ad ? ` · name from ${s.ad}` : ""}</span></div>
              <div className="text-mist">{s.rule}</div>
              <div className="mt-1 flex flex-wrap gap-x-4">
                <span>today: {s.today.entries} entries, net <b className={pnlClass(s.today.net)}>{inr(s.today.net)}</b></span>
                <span>last {s.recent.days}d: {s.recent.trades} trades, win {s.recent.win_rate_pct}%, net <b className={pnlClass(s.recent.net)}>{inr(s.recent.net)}</b></span>
              </div>
            </li>
          ))}
        </ul>
        {!!strat.data?.not_built.length && (
          <details className="mt-2 text-mist"><summary className="cursor-pointer">Not built ({strat.data.not_built.length}) and why</summary>
            <ul className="mt-1 list-disc pl-4">{strat.data.not_built.map((n) => <li key={n.ad}><b>{n.ad}</b>: {n.why}</li>)}</ul>
          </details>
        )}
      </Card>

      <Card title="Reconcile with Dhan (REAL only)" right={<Btn small onClick={() => void fnoApi.reconcile(true).then(rec.reload, rec.reload)}>Check now</Btn>}>
        {rec.data?.ok === true && <Msg kind="ok">Our open REAL positions match Dhan's net positions.</Msg>}
        {rec.data?.ok === false && <Msg kind="err">Mismatch: {rec.data.differences?.map((x) => `${x.security_id}: ours ${x.ours}, Dhan ${x.broker}`).join(" | ")}. Check the Dhan app; nothing is traded automatically.</Msg>}
        {rec.data?.ok == null && <p className="text-mist">{rec.data?.note || rec.error || "Not checked yet (needs REAL mode with Dhan credentials)."}</p>}
      </Card>

      <Card title="Pipeline check" right={<Btn small onClick={pipe.reload}>Re-check</Btn>}>
        {pipe.error && <Msg kind="err">{pipe.error}</Msg>}
        <FnoStagesRow stages={pipe.data?.stages} />
        <p className="mb-1 text-[11px] font-bold uppercase tracking-widest text-mist">Flat checks</p>
        <ul className="space-y-1">
          {pipe.data?.steps.map((s) => (
            <li key={s.step} className="flex flex-wrap gap-2"><Chip tone={s.ok === true ? "good" : s.ok === false ? "bad" : undefined}>{s.ok === true ? "OK" : s.ok === false ? "PROBLEM" : "info"}</Chip><span>{s.step}</span><span className="text-mist">{s.detail}</span></li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
