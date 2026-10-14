// group356 (WS-6): the measured answer to "is the daily structure gate right?" - what the stocks it turned away did over the next 1/5/10/20
// trading days compared with the ones it let through. Read-only, polls only while the tab is visible, collapsed by default (mobile-safe).
// Until enough observations have finished the card says so instead of drawing a conclusion.
import { useCallback, useEffect, useState } from "react";
import { setVisibleInterval } from "../visibleInterval";
import { edgeView, pctText, type StructureEdgeDoc, type Tone } from "../structureView";

const TONE: Record<Tone, string> = { good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold", mute: "text-mist" };

export default function StructureEdgeCard({ load, title = "Structure gate — what happened next", defaultOpen = false }:
  { load: (days: number) => Promise<StructureEdgeDoc>; title?: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [days, setDays] = useState(60);
  const [raw, setRaw] = useState<StructureEdgeDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    try { setRaw(await load(days)); setErr(null); }
    catch (e: any) { setErr(e?.message || "Could not load the structure edge report"); }
    finally { setBusy(false); }
  }, [load, days]);

  useEffect(() => {
    void refresh();
    return setVisibleInterval(() => void refresh(), 5 * 60_000);
  }, [refresh]);

  const v = edgeView(raw);
  return (
    <div className="rounded-2xl border border-slate bg-graphite p-3 font-display tabular-nums text-[11px]" data-testid="structure-edge-card">
      <div className="flex items-start justify-between gap-2">
        <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open} className="min-w-0 flex-1 text-left">
          <span className="uppercase tracking-widest text-[10px] text-mist mr-2">{title}</span>
          {raw && <span className={`font-bold ${TONE[v.tone]}`}>{v.conclusionLabel}</span>}
          <span className="block text-mist">{err ? "" : raw ? v.headline : "Loading…"}</span>
          {(err || v.error) && <span className="block text-signal-sell">{err || v.error}</span>}
        </button>
        <div className="flex shrink-0 items-center gap-1">
          {[30, 90].map(d => (
            <button key={d} type="button" onClick={() => setDays(d)} aria-pressed={days === d}
              className={`px-2 py-0.5 rounded-lg border text-[10px] ${days === d ? "border-signal-buy text-signal-buy" : "border-slate text-mist"}`}>{d}d</button>
          ))}
          <button type="button" onClick={() => void refresh()} disabled={busy} aria-label="Refresh structure edge report"
            className="px-2 py-0.5 rounded-lg border border-slate text-[10px] text-mist disabled:opacity-40">{busy ? "…" : "↻"}</button>
          <button type="button" onClick={() => setOpen(o => !o)} aria-label={open ? "Collapse" : "Expand"} className="px-1 text-mist">{open ? "▲" : "▼"}</button>
        </div>
      </div>

      {open && raw && (
        <div className="mt-3 space-y-3 border-t border-slate pt-3">
          <div className="overflow-x-auto">
            <table className="w-full text-left">
              <thead className="text-[9px] uppercase tracking-widest text-mist">
                <tr><th className="pr-2 font-normal">After</th><th className="pr-2 font-normal">Turned away</th><th className="pr-2 font-normal">Let through</th><th className="font-normal">Difference</th></tr>
              </thead>
              <tbody>
                {v.horizons.map(h => (
                  <tr key={h.h} className="border-t border-slate/40">
                    <td className="pr-2 py-1 text-mist">{h.h.replace("d", " day")}{h.h === "1d" ? "" : "s"}</td>
                    <td className="pr-2 text-paper">{pctText(h.rejected)} <span className="text-mist">({h.rejectedN})</span></td>
                    <td className="pr-2 text-paper">{pctText(h.passed)} <span className="text-mist">({h.passedN})</span></td>
                    <td className={h.edge == null ? "text-mist" : h.edge > 0 ? "text-signal-buy" : h.edge < 0 ? "text-signal-sell" : "text-mist"}>{h.edge == null ? "—" : pctText(h.edge)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[9px] text-mist">
            Average move after the verdict. A positive difference means the stocks the gate let through did better than the ones it turned away.
            {v.rejectedN > 0 && ` Of ${v.rejectedN} turned away: ${v.stopFirstRejected} hit the structural stop first, ${v.targetFirstRejected} the target.`}
          </p>
          {v.recent.length > 0 && (
            <ul className="space-y-1">
              {v.recent.map((r, i) => (
                <li key={`${r.symbol}-${r.day}-${i}`} className="flex items-start justify-between gap-2">
                  <span className="min-w-0"><span className="font-bold text-paper">{r.symbol}</span> <span className="text-mist">{r.day} · {r.verdictLabel}</span>
                    {r.text && <span className="block text-mist break-words">{r.text}</span>}</span>
                  <span className={`shrink-0 ${TONE[r.tone]}`}>{r.ret}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
