// E243: "SELL orders" card on the Position Stocks Positions tab. Every SELL Stockky sent to Dhan (live shorts and SELL probe runs) with a SELL tag and Dhan's own
// view of the order, so a SELL that rests unfilled is visible instead of silent. Read-only. Polls only while its tab is visible. Numbers come from sellOrdersView().
import { useEffect, useState } from "react";
import { setVisibleInterval } from "../../visibleInterval";
import { Card } from "./boardCard";
import { sellOrdersView, type SellOrdersDoc } from "../../sellOrdersView";

const TONE: Record<string, string> = { good: "text-signal-buy", bad: "text-signal-sell", warn: "text-signal-hold", mute: "text-mist" };

export default function SellOrdersCard({ active, fetcher }: { active: boolean; fetcher: () => Promise<SellOrdersDoc> }) {
  const [doc, setDoc] = useState<SellOrdersDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (!active) return;
    let alive = true;
    const load = async () => {
      try { const d = await fetcher(); if (alive) { setDoc(d); setErr(null); } }
      catch (e: any) { if (alive) setErr(e?.message || "SELL orders unavailable"); }
    };
    void load();
    const stop = setVisibleInterval(load, 10000);
    return () => { alive = false; stop(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);

  const v = sellOrdersView(doc);
  return (
    <Card title="SELL orders · shorts and SELL probe" right={<span className="text-[9px] uppercase tracking-widest text-signal-sell">{doc?.short_mode ? `short mode: ${doc.short_mode}` : "SELL"}</span>}>
      <div className="space-y-2 text-[11px]" data-testid="sell-orders">
        {err && <div className="text-signal-hold">{err}</div>}
        {!doc && !err && <div className="text-mist">Loading…</div>}
        {doc && <div className={v.waiting > 0 || (doc?.summary?.dhan_shorts_untracked ?? 0) > 0 ? "text-signal-hold" : "text-mist"} data-testid="sell-orders-headline">{v.headline}</div>}
        {v.errors.map((e, i) => <div key={i} className="text-signal-hold">{e}</div>)}
        {doc?.entry_style && (
          <div className="text-[10px] text-mist">
            SELL entry: {doc.entry_style.marketable_limit ? `limit ${doc.entry_style.cushion_pct}% under the last price (fills at the best bid)` : "MARKET order"}
          </div>
        )}
        {v.lines.length > 0 && (
          <div className="space-y-1" data-testid="sell-orders-rows">
            {v.lines.slice(0, 20).map((l) => (
              <div key={l.key} className="flex items-start justify-between gap-2 rounded-lg border border-slate bg-ink px-2 py-1.5">
                <div className="min-w-0">
                  <span className="font-display font-bold text-paper">{l.symbol}</span>
                  <span className="ml-1.5 rounded bg-signal-sell/20 px-1 text-[9px] font-bold uppercase tracking-widest text-signal-sell">SELL</span>
                  <span className="ml-1 rounded border border-slate px-1 text-[9px] uppercase tracking-widest text-mist">{l.tag === "SELL PROBE" ? "probe" : l.tag === "AT DHAN" ? "at Dhan" : "short"}</span>
                  <div className="text-[10px] text-mist tabular-nums break-words">{l.detail}</div>
                  <div className="text-[9px] text-mist tabular-nums">{l.when ? new Date(l.when).toLocaleString() : "-"}{l.orderId ? ` · Dhan order ${l.orderId}` : ""}</div>
                </div>
                <div className={`shrink-0 text-right text-[10px] font-bold uppercase ${TONE[l.tone]}`}>{l.state}</div>
              </div>
            ))}
          </div>
        )}
        <div className="pt-1 text-[9px] text-mist">Read-only. A short sells first and buys back later. A SELL that Dhan holds as RESTING has not filled: no position exists for it until it does.</div>
      </div>
    </Card>
  );
}
