// Round 265: F&O Overview > Real-order probe. The newest saved probe (scripts/fno_real_order_probe.py): what was sent, what Dhan said, what filled, gross / charges / net,
// and the six charge lines of OUR model next to the six numbers you copy from Dhan's contract note. It changes no rate and does not confirm the readiness item for you:
// MATCHED is evidence for one trade, and "Probe charges matched the contract note" in the readiness card stays your click.
import { useState } from "react";
import { fnoApi } from "../../fnoApi";
import { inr, parseNoteInputs, PROBE_LINES, probeHeadline, probeVerdictChip } from "../../fnoView";
import { Btn, Card, Chip, Msg } from "../calibration/ui";
import { useFno } from "./useFno";

export default function FnoProbeCard() {
  const [file, setFile] = useState("");
  const r = useFno(() => fnoApi.probe(file), 0, [file]);
  const [fields, setFields] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const d = r.data;
  const head = probeHeadline(d);
  const cmp = d?.comparison;
  const chip = probeVerdictChip(cmp?.verdict);

  const save = async () => {
    const { lines, errors } = parseNoteInputs(fields);
    if (errors.length) { setMsg({ kind: "err", text: errors.join("; ") }); return; }
    if (!Object.keys(lines).length) { setMsg({ kind: "err", text: "Type at least one amount from the contract note." }); return; }
    setBusy(true);
    try { await fnoApi.probeNote(d?.probe?.file || "", lines); setMsg({ kind: "ok", text: "Compared." }); r.reload(); } catch (e: any) { setMsg({ kind: "err", text: String(e?.message || e) }); } finally { setBusy(false); }
  };

  return (
    <Card title="Real-order probe" tone={head.tone === "bad" ? "bad" : head.tone === "warn" ? "warn" : undefined}
      right={<>{cmp && d?.probe?.pnl && <Chip tone={chip.tone === "neutral" ? undefined : chip.tone}>{chip.text}</Chip>}<Btn small onClick={r.reload}>Re-check</Btn></>}>
      {r.error && <Msg kind="err">{r.error}</Msg>}
      {msg && <div className="mb-2"><Msg kind={msg.kind}>{msg.text}</Msg></div>}
      {!d && !r.error && <p className="text-mist">Loading…</p>}
      {d && (
        <div className="space-y-3">
          {head.tone === "bad" ? <Msg kind="err">{head.text}</Msg> : <p className="text-[13px] font-bold">{head.text}</p>}
          {d.files.length > 1 && (
            <label className="flex items-center gap-2 text-[11px] text-mist">Probe file
              <select className="rounded border border-slate bg-transparent p-1" value={file || d.probe?.file || ""} onChange={(e) => setFile(e.target.value)}>{d.files.map((f) => <option key={f} value={f}>{f}</option>)}</select>
            </label>
          )}
          {d.probe && (
            <>
              <p className="text-[12px] text-mist">{d.probe.file} · {d.probe.live ? "LIVE" : "dry run"} · {d.probe.contract || `${d.probe.underlying} ${d.probe.side}`}</p>
              {d.probe.dhan_hint && <Msg kind="warn">{d.probe.dhan_hint}</Msg>}
              <ul className="space-y-1">
                {d.probe.orders.map((o, i) => (
                  <li key={i} className="flex flex-wrap gap-x-3 gap-y-1 rounded-lg border border-slate p-2 text-[12px]">
                    <b>{o.side}</b><span>limit {o.price} x {o.qty}</span><span>{o.status || "-"}</span><span>filled {o.filled_qty ?? 0}{o.avg_price ? ` at ${o.avg_price}` : ""}</span>
                    {o.order_id && <span className="text-mist">id {o.order_id}</span>}{o.message && <span className="text-mist">{o.message}</span>}
                  </li>
                ))}
              </ul>
              {d.probe.pnl && (
                <p className="text-[12px]">Gross <b>{inr(d.probe.pnl.gross)}</b> · charges (our model) <b>{inr(d.probe.pnl.charges)}</b> · net <b>{inr(d.probe.pnl.net)}</b>. Gross is what the Dhan Positions screen shows as realised.</p>
              )}
              {d.probe.pnl && cmp && (
                <div className="space-y-2">
                  <p className="text-[11px] font-bold uppercase tracking-widest text-mist">Charges: our model vs Dhan's contract note</p>
                  <div className="overflow-x-auto">
                    <table className="w-full text-[12px]">
                      <thead><tr className="text-left text-mist"><th className="pr-3">Line</th><th className="pr-3">Ours</th><th className="pr-3">Contract note</th><th className="pr-3">Check</th></tr></thead>
                      <tbody>
                        {cmp.lines.map((l) => {
                          const k = l.ok === true ? { tone: "good" as const, text: "MATCH" } : l.ok === false ? { tone: "bad" as const, text: "DIFFERS" } : { tone: undefined, text: "not entered" };
                          return (
                            <tr key={l.line} className="border-t border-slate/40 align-top">
                              <td className="py-1 pr-3">{l.label}</td><td className="pr-3">{l.model.toFixed(2)}</td>
                              <td className="pr-3"><input aria-label={`${l.label} from the contract note`} inputMode="decimal" className="w-24 rounded border border-slate bg-transparent p-1" placeholder={d.note?.lines[l.line] != null ? String(d.note.lines[l.line]) : "Rs"}
                                value={fields[l.line] ?? ""} onChange={(e) => setFields({ ...fields, [l.line]: e.target.value })} /></td>
                              <td className="pr-3"><Chip tone={k.tone}>{k.text}</Chip>{l.ok === false && <span className="ml-2 text-mist">{l.diff != null && `${l.diff > 0 ? "+" : ""}${l.diff.toFixed(2)}`} · {l.param}: now {l.current}, note implies about {l.implied}</span>}</td>
                            </tr>
                          );
                        })}
                        <tr className="border-t border-slate/40"><td className="py-1 pr-3">Total (optional)</td><td className="pr-3">{cmp.total ? cmp.total.model.toFixed(2) : "-"}</td>
                          <td className="pr-3"><input aria-label="Total from the contract note" inputMode="decimal" className="w-24 rounded border border-slate bg-transparent p-1" placeholder={d.note?.lines.total != null ? String(d.note.lines.total) : "Rs"}
                            value={fields.total ?? ""} onChange={(e) => setFields({ ...fields, total: e.target.value })} /></td><td>{cmp.total && <Chip tone={cmp.total.ok ? "good" : "bad"}>{cmp.total.ok ? "MATCH" : "DIFFERS"}</Chip>}</td></tr>
                      </tbody>
                    </table>
                  </div>
                  <div className="flex flex-wrap items-center gap-2"><Btn small primary disabled={busy} onClick={() => void save()}>Compare</Btn><span className="text-[11px] text-mist">{PROBE_LINES.length - 1} lines + optional total, from Dhan &gt; Reports &gt; P&amp;L / contract note.</span></div>
                  <p className="text-[12px]">{cmp.text}</p>
                  {cmp.verdict === "MATCHED" && <p className="text-[11px] text-mist">If you are satisfied, confirm "Probe charges matched the contract note" in the Readiness card yourself; nothing is confirmed for you.</p>}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </Card>
  );
}
