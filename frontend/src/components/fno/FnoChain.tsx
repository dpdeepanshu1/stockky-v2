// F&O Options > Chain: live option chain around the money, with the OI / PCR / max-pain summary and the signal's reasoning.
import { useState } from "react";
import { fnoApi } from "../../fnoApi";
import { pcrReading } from "../../fnoView";
import { Card, Chip, Msg } from "../calibration/ui";
import { SubTabs } from "../ui/SubTabs";
import { useFno } from "./useFno";

const UNDS = [{ id: "NIFTY", label: "Nifty" }, { id: "SENSEX", label: "Sensex" }] as const;
const n0 = (v: number | null | undefined) => (v == null ? "-" : Math.round(v).toLocaleString("en-IN"));
const n2 = (v: number | null | undefined) => (v == null ? "-" : v.toFixed(2));

export default function FnoChain() {
  const [und, setUnd] = useState<"NIFTY" | "SENSEX">("NIFTY");
  const [expiry, setExpiry] = useState("");
  const c = useFno(() => fnoApi.chain(und, expiry), 8000, [und, expiry]);
  const d = c.data;
  const spot = d?.chain.spot ?? 0;
  const atm = d?.chain.strikes.reduce<number | null>((best, r) => (best == null || Math.abs(r.strike - spot) < Math.abs(best - spot) ? r.strike : best), null) ?? null;
  const rows = d ? d.chain.strikes.filter((r) => atm != null && Math.abs(r.strike - atm) <= (und === "NIFTY" ? 600 : 2000)) : [];
  const s = d?.summary || {};
  return (
    <div className="space-y-3">
      <SubTabs items={UNDS as any} value={und} onChange={(v) => { setUnd(v as any); setExpiry(""); }} label="Index" />
      {c.error && <Msg kind="err">{c.error}</Msg>}
      <Card title={`${und} chain`} right={<>
        <select value={expiry || d?.chain.expiry || ""} onChange={(e) => setExpiry(e.target.value)} className="rounded-lg border border-slate bg-transparent px-2 py-1 text-[11px]">
          {(d?.expiries || []).map((x) => <option key={x} value={x}>{x}</option>)}
        </select>
        {d && <Chip tone={d.age_s > 30 ? "warn" : "good"}>{Math.round(d.age_s)}s old</Chip>}
      </>}>
        {d ? (
          <>
            <div className="mb-2 flex flex-wrap gap-x-4 gap-y-1">
              <span>Spot <b>{n2(spot)}</b></span>
              <span>PCR <b>{n2(s.pcr_oi)}</b> <span className="text-mist">{pcrReading(s.pcr_oi)}</span></span>
              <span>Max pain <b>{n0(s.max_pain)}</b></span>
              <span>Call wall <b>{n0(s.call_wall)}</b></span>
              <span>Put wall <b>{n0(s.put_wall)}</b></span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-right text-[11px]">
                <thead className="text-mist"><tr><th>OI</th><th>Vol</th><th>IV</th><th>Delta</th><th>CE LTP</th><th className="text-center">Strike</th><th>PE LTP</th><th>Delta</th><th>IV</th><th>Vol</th><th>OI</th></tr></thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.strike} className={r.strike === atm ? "bg-white/10 font-bold" : ""}>
                      <td>{n0(r.ce?.oi)}</td><td>{n0(r.ce?.volume)}</td><td>{n2(r.ce?.iv)}</td><td>{n2(r.ce?.delta)}</td><td>{n2(r.ce?.ltp)}</td>
                      <td className="text-center">{r.strike}</td>
                      <td>{n2(r.pe?.ltp)}</td><td>{n2(r.pe?.delta)}</td><td>{n2(r.pe?.iv)}</td><td>{n0(r.pe?.volume)}</td><td>{n0(r.pe?.oi)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        ) : <p className="text-mist">{c.loading ? "Loading…" : "No chain."}</p>}
      </Card>
      {d && (
        <Card title="What the signal sees" right={<Chip tone={d.bias.direction === "UP" ? "good" : d.bias.direction === "DOWN" ? "bad" : undefined}>{d.bias.direction === "UP" ? "UP (CE)" : d.bias.direction === "DOWN" ? "DOWN (PE)" : "FLAT (no trade)"} · score {Math.round(d.bias.score)}</Chip>}>
          <ul className="list-disc space-y-0.5 pl-4">{(d.bias.reasons || []).map((r, i) => <li key={i}>{r}</li>)}</ul>
          {!d.bias.reasons?.length && <p className="text-mist">No reasons reported.</p>}
        </Card>
      )}
    </div>
  );
}
