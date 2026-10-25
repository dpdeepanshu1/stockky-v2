// Short selling step 6: "Short selling (shadow)". Every row is a short Stockky WOULD have taken; NO order exists behind this card (SHORT_MODE has no live value
// yet). It shows the plan's S2 gate (100 would-be shorts over about 4 weeks, positive after charges), why the gates said no, and the latest rows.
// Read-only. Polls only while its tab is visible. All numbers come from shortShadowView() (shortShadow.test.mjs).
import { useEffect, useState } from "react";
import { setVisibleInterval } from "../../visibleInterval";
import { Card } from "./boardCard";
import { rupees, shortShadowView, statusTone, type ShortShadowDoc } from "../../shortShadowView";

const TONE: Record<string, string> = { good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold" };

export default function ShortShadowCard({ active, fetcher }: { active: boolean; fetcher: () => Promise<ShortShadowDoc> }) {
  const [doc, setDoc] = useState<ShortShadowDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = async () => {
      try { const d = await fetcher(); if (alive) { setDoc(d); setErr(null); } }
      catch (e: any) { if (alive) setErr(e?.message || "short shadow unavailable"); }
    };
    void load();
    const stop = setVisibleInterval(load, 15000);
    return () => { alive = false; stop(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const v = shortShadowView(doc);
  return (
    <Card title={doc?.mode === "live" ? "Short selling · LIVE (shadow records)" : "Short selling · shadow only"} right={<span className="text-[9px] uppercase tracking-widest text-signal-hold">{doc?.mode === "live" ? "live orders: see Positions" : "no order is placed"}</span>}>
      <div className="space-y-2 text-[11px]" data-testid="short-shadow">
        {err && <div className="text-signal-hold">{err}</div>}
        {!doc && !err && <div className="text-mist">Loading…</div>}
        {doc && <div className="text-mist" data-testid="short-shadow-headline">{v.headline}</div>}

        {doc && v.kind !== "off" && (
          <div data-testid="short-shadow-s2">
            <div className="flex items-center justify-between text-[10px] text-mist">
              <span>Gate S2: {v.progress.done} of {v.progress.needed} would-be shorts · {v.progress.days} of {v.progress.daysNeeded} trading days · {rupees(doc.net_pnl)} after charges</span>
              <span className={v.s2Ready ? "text-signal-buy font-bold" : "text-mist"}>{v.s2Ready ? "numbers pass" : "not yet"}</span>
            </div>
            <div className="mt-1 h-1.5 w-full overflow-hidden rounded bg-ink" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={v.progress.pct}>
              <div className="h-full bg-signal-prepare" style={{ width: `${v.progress.pct}%` }} />
            </div>
            {!v.s2Ready && v.s2Missing.length > 0 && <ul className="mt-1 list-disc pl-4 text-[10px] text-mist">{v.s2Missing.map((m, i) => <li key={i}>{m}</li>)}</ul>}
            {v.s2Ready && <div className="mt-1 text-[10px] text-signal-hold">The three numbers pass. Before trusting them, check the profit is not from one or two days, and remember shadow fills have no spread or slippage (optimistic).</div>}
          </div>
        )}

        {doc && v.kind === "active" && (
          <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
            {[["Target", doc.target], ["Stop loss", doc.stop], ["Square-off", doc.eod], ["Win rate", doc.win_rate_pct == null ? "-" : `${doc.win_rate_pct}%`]].map(([k, val]) => (
              <div key={String(k)} className="rounded-lg border border-slate bg-ink px-2 py-1">
                <div className="text-[9px] uppercase tracking-widest text-mist">{k}</div>
                <div className="font-display font-bold tabular-nums text-paper">{val}</div>
              </div>
            ))}
          </div>
        )}

        {v.refusals.length > 0 && (
          <div data-testid="short-shadow-refusals">
            <div className="text-[10px] uppercase tracking-widest text-mist">Why gates said no (since the service started)</div>
            <div className="mt-1 flex flex-wrap gap-1">
              {v.refusals.map((r) => <span key={r.code} className="rounded border border-slate bg-ink px-1.5 py-0.5 tabular-nums text-mist">{r.code.replace(/^SHORT_/, "")} · {r.count}</span>)}
            </div>
          </div>
        )}

        {v.rows.length > 0 && (
          <div className="space-y-1" data-testid="short-shadow-rows">
            {v.rows.slice(0, 10).map((r) => (
              <div key={r.id} className="flex items-center justify-between gap-2 rounded-lg border border-slate bg-ink px-2 py-1.5">
                <div className="min-w-0">
                  <span className="font-display font-bold text-paper">{r.symbol}</span>
                  <span className="ml-1.5 rounded bg-signal-sell/20 px-1 text-[9px] font-bold uppercase tracking-widest text-signal-sell">SHORT</span>
                  <div className="truncate text-[10px] text-mist tabular-nums">
                    {r.day} · {r.quantity ?? "-"} @ {r.entry_price?.toFixed(2) ?? "-"} · stop {r.stop_price?.toFixed(2) ?? "-"} · target {r.target_price?.toFixed(2) ?? "-"}
                    {r.market_state ? ` · ${r.market_state}` : ""}
                  </div>
                </div>
                <div className="shrink-0 text-right tabular-nums">
                  <div className={`font-bold ${r.net_pnl == null ? "text-mist" : r.net_pnl >= 0 ? "text-signal-buy" : "text-signal-sell"}`}>{rupees(r.net_pnl)}</div>
                  <div className={`text-[10px] ${TONE[statusTone(r.status) ?? ""] ?? "text-mist"}`}>{r.status}</div>
                </div>
              </div>
            ))}
          </div>
        )}
        <div className="pt-1 text-[9px] text-mist">Evidence only. A short sells first and buys back later, so the profit is entry minus exit. Switch it on with SHORT_MODE = shadow on the Switches page. Real Trade stays long-only.</div>
      </div>
    </Card>
  );
}
