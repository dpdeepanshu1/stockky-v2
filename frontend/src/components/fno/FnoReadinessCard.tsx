// Round 262, slice 3: F&O Overview > Readiness. The checklist from GET /fno/readiness: what the service can prove (DEMO days and trades, net, rolling backtests, reconcile,
// capital board, recorded chains) and the four checks only you can make in Dhan. While a hard item is not done, REAL orders stay closed (switch "REAL waits for the readiness list").
// Green means "the evidence the list asked for exists", never "safe": the card says so.
import { useState } from "react";
import { fnoApi } from "../../fnoApi";
import { confirmQuestion, readinessChip, readinessGroups, readinessHeadline } from "../../fnoView";
import { Btn, Card, Chip, Msg } from "../calibration/ui";
import { useFno } from "./useFno";

export default function FnoReadinessCard({ onChanged }: { onChanged?: () => void }) {
  const r = useFno(fnoApi.readiness, 30000);
  const [busy, setBusy] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const d = r.data;
  const head = readinessHeadline(d);

  const confirm = async (key: string, label: string, ok: boolean) => {
    const item = d?.items.find((i) => i.key === key);
    if (!item || !window.confirm(confirmQuestion(item, ok))) return;
    setBusy(key);
    try {
      await fnoApi.readinessConfirm(key, ok);
      setMsg({ kind: "ok", text: ok ? `Confirmed: ${label}` : `Withdrawn: ${label}` });
      r.reload();
      onChanged?.();
    } catch (e: any) {
      setMsg({ kind: "err", text: String(e?.message || e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card title="Readiness for REAL" tone={head.tone === "good" ? "good" : head.tone === "warn" ? "warn" : undefined}
      right={<>
        {d && <Chip tone="good">{d.counts.green} done</Chip>}
        {d && d.counts.red > 0 && <Chip tone="bad">{d.counts.red} not met</Chip>}
        {d && d.counts.unknown > 0 && <Chip tone="warn">{d.counts.unknown} not shown yet</Chip>}
        <Btn small onClick={r.reload}>Re-check</Btn>
      </>}>
      {r.error && <Msg kind="err">{r.error}</Msg>}
      {msg && <div className="mb-2"><Msg kind={msg.kind}>{msg.text}</Msg></div>}
      {!d && !r.error && <p className="text-mist">Loading…</p>}
      {d && (
        <div className="space-y-3">
          <p className="text-[13px] font-bold">{head.text}</p>
          {readinessGroups(d.items).map((g) => (
            <div key={g.group}>
              <p className="mb-1 text-[11px] font-bold uppercase tracking-widest text-mist">{g.group}</p>
              <ul className="space-y-1">
                {g.items.map((it) => {
                  const chip = readinessChip(it.ok);
                  return (
                    <li key={it.key} className="flex flex-wrap items-center gap-2 rounded-lg border border-slate p-2 text-[12px]">
                      <Chip tone={chip.tone}>{chip.text}</Chip>
                      <b>{it.label}</b>
                      {!it.hard && <Chip title="Shown for information; it does not keep REAL closed">info only</Chip>}
                      <span className="text-mist">{it.detail}</span>
                      {it.manual && (
                        <span className="ml-auto">
                          {it.ok === true
                            ? <Btn small disabled={busy === it.key} onClick={() => void confirm(it.key, it.label, false)}>Withdraw</Btn>
                            : <Btn small disabled={busy === it.key} onClick={() => void confirm(it.key, it.label, true)}>I checked this</Btn>}
                        </span>
                      )}
                    </li>
                  );
                })}
              </ul>
            </div>
          ))}
          <p className="text-[11px] text-mist">
            Needed before REAL: {d.thresholds.demo_days} DEMO days, {d.thresholds.demo_trades} DEMO trades, {d.thresholds.backtest_trades}+ trades in each strategy's rolling backtest, chains recorded on {d.thresholds.snapshot_days}+ days.
            The service re-checks every 30 seconds; the REAL lock reads the same list. The wait can be switched off on Real Money Calibration &gt; F&amp;O &gt; Switches (&quot;REAL waits for the readiness list&quot;); that is a loosening and asks for a confirmation there.
          </p>
        </div>
      )}
    </Card>
  );
}
