// E271: per-symbol part of the "Order-flow tape" section. READ-ONLY.
// Pick a symbol (type one, or tap an open position) and see the six checks of the order-flow checklist for a long or a short, with the levels behind
// them (VAH / VAL / POC, nearest HVN). Wording and tones come from tapeSymbolView.ts (tapeSymbol.test.mjs). Polls every 15 s while a symbol is shown
// and the tab is visible.
import { useCallback, useEffect, useState } from "react";
import type { Svc } from "../../calibrationApi";
import { positionStocksApi } from "../../positionStocksApi";
import { realTradeApi } from "../../realTradeApi";
import { sideOf, symbolTapeView, type Side, type SymbolTapeDoc } from "../../tapeSymbolView";
import { setVisibleInterval } from "../../visibleInterval";
import { Btn, Card, Chip, Msg } from "./ui";

interface OpenTrade { symbol: string; side: Side }

export default function TapeSymbolCard({ svc }: { svc: Svc }) {
  const [text, setText] = useState("");
  const [symbol, setSymbol] = useState<string | null>(null);
  const [side, setSide] = useState<Side>("LONG");
  const [doc, setDoc] = useState<SymbolTapeDoc | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [open, setOpen] = useState<OpenTrade[]>([]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const rows = svc === "ps" ? await positionStocksApi.positions() : await realTradeApi.positions("REAL");
        const seen = new Set<string>(), out: OpenTrade[] = [];
        for (const r of rows as any[]) {
          if (String(r?.status ?? "").toUpperCase() !== "OPEN" || !r?.symbol || seen.has(r.symbol)) continue;
          seen.add(r.symbol); out.push({ symbol: String(r.symbol), side: sideOf(r.side) });
        }
        if (alive) setOpen(out.slice(0, 12));
      } catch { if (alive) setOpen([]); }
    })();
    return () => { alive = false; };
  }, [svc]);

  const load = useCallback(async () => {
    if (!symbol) return;
    try {
      setDoc(svc === "ps" ? await positionStocksApi.flowSymbol(symbol) : await realTradeApi.flowSymbol(symbol));
      setErr(null);
    } catch (e: any) { setDoc(null); setErr(`tape for ${symbol}: ${e?.message || e}`); }
  }, [svc, symbol]);
  useEffect(() => { setDoc(null); if (!symbol) return; void load(); return setVisibleInterval(() => void load(), 15000); }, [symbol, load]);

  const show = (sym: string, s?: Side) => { const v = sym.trim().toUpperCase(); if (!v) return; if (s) setSide(s); setSymbol(v); setText(v); };
  const v = symbolTapeView(doc, side);
  const tone = v.tone === "bad" ? "bad" : v.tone === "warn" ? "warn" : v.tone === "good" ? "good" : undefined;
  return (
    <Card title="One symbol: the six checks" tone={symbol && doc ? tone : undefined} right={<Chip title="Reads market-data's tape. Never changes a switch or a trade.">read-only</Chip>}>
      <div className="flex flex-wrap items-center gap-2" data-testid="tape-symbol-pick">
        <input className="w-36 rounded-lg border border-slate bg-transparent px-2 py-1 text-[13px] uppercase" placeholder="Symbol, e.g. SBIN" value={text} maxLength={20}
          aria-label="Symbol" onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") show(text); }} />
        <Btn small title="Shows the tape for this symbol." onClick={() => show(text)}>Show</Btn>
        <Btn small title="Judge the checks for a long." onClick={() => setSide("LONG")}>{side === "LONG" ? "● Long" : "Long"}</Btn>
        {svc === "ps" && <Btn small title="Judge the checks for a short." onClick={() => setSide("SHORT")}>{side === "SHORT" ? "● Short" : "Short"}</Btn>}
      </div>
      {open.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-2" data-testid="tape-open-trades">
          <span className="text-[11px] text-mist">Open trades:</span>
          {open.map((o) => <Btn key={o.symbol} small title={`Show the tape for ${o.symbol} as a ${o.side.toLowerCase()}.`} onClick={() => show(o.symbol, o.side)}>{o.symbol}{o.side === "SHORT" ? " (short)" : ""}</Btn>)}
        </div>
      )}
      {err && <div className="mt-2"><Msg kind="err">{err}</Msg></div>}
      {symbol && doc && (
        <div className="mt-3" data-testid="tape-symbol-body">
          <p className="text-[13px]" data-testid="tape-symbol-headline"><b>{symbol}</b> as a {side.toLowerCase()}: {v.headline}</p>
          {v.rows.length > 0 && (
            <ol className="mt-2 space-y-1" data-testid="tape-symbol-checks">
              {v.rows.map((r, i) => (
                <li key={r.key} className="rounded-lg border border-slate px-2 py-1 text-[12px]">
                  <div className="flex items-center justify-between gap-2">
                    <span><span className="text-mist">{i + 1}</span> {r.question}</span>
                    <Chip tone={r.state === "for" ? "good" : r.state === "against" ? "bad" : undefined}>{r.state}</Chip>
                  </div>
                  <div className="text-mist">{r.detail}</div>
                </li>
              ))}
            </ol>
          )}
          {v.rows.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-2 text-[12px]" data-testid="tape-symbol-levels">
              <Chip>price {v.levels.price}</Chip><Chip>VAH {v.levels.vah}</Chip><Chip>POC {v.levels.poc}</Chip><Chip>VAL {v.levels.val}</Chip>
              <Chip>HVN above {v.levels.hvnAbove}</Chip><Chip>HVN below {v.levels.hvnBelow}</Chip>
            </div>
          )}
          {v.facts.length > 0 && <p className="mt-2 text-[11px] text-mist">{v.facts.join(" · ")}. Estimated from quote snapshots, not a real trade feed.</p>}
        </div>
      )}
      {!symbol && <p className="mt-2 text-[12px] text-mist">Type a symbol or tap an open trade to see the checklist for it.</p>}
    </Card>
  );
}
