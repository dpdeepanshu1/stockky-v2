// group355: how the daily market-structure gate is actually behaving - one card for both services. Shows the mode (enforcing / shadow / off),
// how many candidates were checked, how many it blocked (or would have, in shadow), how many had no structure read, why it said no, and what
// the stocks it turned away did afterwards. Read-only; polls only while the tab is visible. Collapsed by default (mobile-safe).
import { useCallback, useEffect, useState } from "react";
import { setVisibleInterval } from "../visibleInterval";
import {
  outcomeLine, sinceText, sinceTone, statusView, windowText,
  type StructureStatusDoc, type StructureStatusKind, type Tone,
} from "../structureView";

const TONE: Record<Tone, string> = { good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold", mute: "text-mist" };
const WINDOWS = [24, 168];

export default function StructureGateCard({ kind, load, title = "Daily structure gate", defaultOpen = false }:
  { kind: StructureStatusKind; load: (hours: number) => Promise<StructureStatusDoc>; title?: string; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const [hours, setHours] = useState(24);
  const [raw, setRaw] = useState<StructureStatusDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    setBusy(true);
    try { setRaw(await load(hours)); setErr(null); }
    catch (e: any) { setErr(e?.message || "Could not load structure gate status"); }
    finally { setBusy(false); }
  }, [load, hours]);

  useEffect(() => {
    void refresh();
    return setVisibleInterval(() => void refresh(), 60_000);
  }, [refresh]);

  const v = statusView(kind, raw);
  const outcome = outcomeLine(v);
  return (
    <div className="rounded-2xl border border-slate bg-graphite p-3 font-display tabular-nums text-[11px]" data-testid="structure-gate-card">
      <div className="flex items-start justify-between gap-2">
        <button type="button" onClick={() => setOpen(o => !o)} aria-expanded={open} className="min-w-0 flex-1 text-left">
          <span className="uppercase tracking-widest text-[10px] text-mist mr-2">{title}</span>
          {raw && <span className={`font-bold ${TONE[v.modeTone]}`}>{v.modeLabel}</span>}
          <span className="block text-mist">{err ? "" : raw ? v.headline : "Loading…"}</span>
          {err && <span className="block text-signal-sell">{err}</span>}
        </button>
        <div className="flex shrink-0 items-center gap-1">
          {WINDOWS.map(h => (
            <button key={h} type="button" onClick={() => setHours(h)} aria-pressed={hours === h}
              className={`px-2 py-0.5 rounded-lg border text-[10px] ${hours === h ? "border-signal-buy text-signal-buy" : "border-slate text-mist"}`}>
              {h === 24 ? "24h" : "7d"}
            </button>
          ))}
          <button type="button" onClick={() => void refresh()} disabled={busy} aria-label="Refresh structure gate status"
            className="px-2 py-0.5 rounded-lg border border-slate text-[10px] text-mist disabled:opacity-40">{busy ? "…" : "↻"}</button>
          <button type="button" onClick={() => setOpen(o => !o)} aria-label={open ? "Collapse" : "Expand"} className="px-1 text-mist">{open ? "▲" : "▼"}</button>
        </div>
      </div>

      {open && raw && (
        <div className="mt-3 space-y-3 border-t border-slate pt-3">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <Stat label="Checked" value={v.checked} />
            <Stat label={v.gateMode === "shadow" ? "Would block" : "Blocked"} value={v.gateMode === "shadow" ? v.shadow + v.stopped : v.stopped} tone={v.stopped || v.shadow ? "warn" : "mute"} />
            <Stat label="No structure read" value={v.noOpinion} />
            <Stat label="Coverage" value={v.coveragePct == null ? "—" : `${v.coveragePct}%`} tone={v.coveragePct != null && v.coveragePct < 50 ? "warn" : "good"} />
          </div>
          {v.coveragePct != null && v.coveragePct < 50 && (
            <p className="text-signal-hold">Under half of the candidates had a usable structure read, so the gate is judging only part of the field.</p>
          )}

          {v.reasons.length > 0 && (
            <Counts title={`Why it said no (last ${windowText(v.hours)})`} rows={v.reasons} />
          )}
          {v.nodata.length > 0 && <Counts title="Why there was no read" rows={v.nodata} />}

          {v.rejects.length > 0 && (
            <div>
              <p className="uppercase tracking-widest text-[9px] text-mist mb-1">Turned away{kind === "position" ? " (since restart)" : ""}</p>
              <ul className="space-y-1">
                {v.rejects.map((r, i) => {
                  const s = sinceText(r.sincePct);
                  return (
                    <li key={`${r.symbol}-${i}`} className="flex items-start justify-between gap-2">
                      <span className="min-w-0"><span className="font-bold text-paper">{r.symbol}</span>{r.enforced === false && <span className="text-signal-hold"> shadow</span>}
                        {r.text && <span className="block text-mist break-words">{r.text}</span>}</span>
                      {s && <span className={`shrink-0 ${TONE[sinceTone(r.sincePct)]}`}>{s}</span>}
                    </li>
                  );
                })}
              </ul>
              {outcome && <p className="mt-2 text-mist">{outcome}</p>}
            </div>
          )}

          <p className="text-[9px] text-mist">
            {v.sinceBoot} checked since the service restarted · a minus after "since" means the stock fell after the gate refused it.
          </p>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, tone = "mute" }: { label: string; value: number | string; tone?: Tone }) {
  return (
    <div className="rounded-xl border border-slate bg-ink p-2">
      <p className="uppercase tracking-widest text-[9px] text-mist">{label}</p>
      <p className={`text-base font-bold ${tone === "mute" ? "text-paper" : TONE[tone]}`}>{value}</p>
    </div>
  );
}

function Counts({ title, rows }: { title: string; rows: { key: string; label: string; n: number }[] }) {
  const max = Math.max(1, ...rows.map(r => r.n));
  return (
    <div>
      <p className="uppercase tracking-widest text-[9px] text-mist mb-1">{title}</p>
      <ul className="space-y-1">
        {rows.map(r => (
          <li key={r.key}>
            <div className="flex justify-between"><span className="text-mist">{r.label}</span><span className="text-paper">{r.n}</span></div>
            <div className="h-1 rounded bg-ink"><div className="h-1 rounded bg-signal-hold" style={{ width: `${Math.round((r.n / max) * 100)}%` }} /></div>
          </li>
        ))}
      </ul>
    </div>
  );
}
