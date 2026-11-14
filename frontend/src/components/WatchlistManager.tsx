import { useEffect, useRef, useState } from "react";
import { EmptyState } from "./ui/EmptyState";   // group 463
import WatchlistPulse from "./WatchlistPulse";   // design v5
import { api } from "../api";
import { Button, Tabs } from "./kit";   // E540
import { WATCH_SORTS, dropSymbol, moveSymbol, sortWatchlist, type WatchSort } from "../watchlistOrder";   // E540
import { AddedMap, STORAGE_KEY, formatPct, isBigMove, label, parseAdded, pctSince, prune, tone, withBaseline, checkSymbol } from "../watchlistGain";   // E28

interface Props {
  symbols: string[];
  onChange: (symbols: string[]) => void;
  onAnalyse: (symbol: string) => void;
  onScanWatchlist?: () => void;
}

export default function WatchlistManager({ symbols, onChange, onAnalyse, onScanWatchlist }: Props) {
  const [input, setInput] = useState("");
  const [note, setNote] = useState("");   // E166: why an Add was refused
  // E28: "% since added" - baseline price kept in this browser (see watchlistGain.ts); live price from /quote, refreshed every 60 s.
  const [added, setAdded] = useState<AddedMap>(() => {
    try { return parseAdded(window.localStorage.getItem(STORAGE_KEY)); } catch { return {}; }
  });
  const [prices, setPrices] = useState<Record<string, number>>({});
  const [sort, setSort] = useState<WatchSort>("mine");   // E540: only changes what is shown, never the saved order
  const [dragging, setDragging] = useState<string | null>(null);
  const symbolsRef = useRef<string[]>(symbols);
  symbolsRef.current = symbols;

  function saveAdded(next: AddedMap) {
    try { window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); } catch { /* storage unavailable: the figure just is not remembered */ }
  }
  function quotePrice(q: { price?: number; close?: number } | null | undefined): number | undefined {
    const p = q ? (q.price ?? q.close) : undefined;
    return typeof p === "number" && Number.isFinite(p) && p > 0 ? p : undefined;
  }

  useEffect(() => {
    let cancelled = false;
    async function refresh() {
      const list = symbolsRef.current.slice(0, 40);
      const results = await Promise.allSettled(list.map((sym) => api.getQuote(sym)));
      if (cancelled) return;
      const nextPrices: Record<string, number> = {};
      results.forEach((r, i) => {
        const px = r.status === "fulfilled" ? quotePrice(r.value) : undefined;
        if (px !== undefined) nextPrices[list[i]] = px;
      });
      const now = Date.now();
      setAdded((prev) => {
        let m = prune(prev, symbolsRef.current);
        for (const [sym, px] of Object.entries(nextPrices)) m = withBaseline(m, sym, px, "first_seen", now);
        saveAdded(m);
        return m;
      });
      setPrices((old) => ({ ...old, ...nextPrices }));
    }
    refresh();
    const id = window.setInterval(refresh, 60000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [symbols.join(",")]);

  function add(symOverride?: string) {
    const chk = checkSymbol(symOverride ?? input, symbols);   // E166: refuse a name / punctuation / duplicate with a reason
    if (!chk.ok) {
      setNote(chk.message);
      if (chk.reason !== "invalid") setInput("");   // an invalid entry stays in the box so it can be fixed
      return;
    }
    const sym = chk.symbol;
    setNote("");
    onChange([...symbols, sym]);
    setInput("");
    // E28: remember the price at the moment of adding (best effort; a failed quote just means no baseline yet).
    api.getQuote(sym).then((q) => {
      const px = quotePrice(q);
      if (px !== undefined) {
        setPrices((old) => ({ ...old, [sym]: px }));
        setAdded((prev) => { const m = withBaseline(prev, sym, px, "added", Date.now()); saveAdded(m); return m; });
      }
    }).catch(() => { /* no baseline; the 60 s refresh will set a first-seen one */ });
  }

  function remove(sym: string) {
    onChange(symbols.filter((s) => s !== sym));
  }

  // E540: the list as shown. "My order" is the saved order (drag or the arrow buttons change it and save it through onChange);
  // the gainers / losers views are display-only, so nothing is saved while they are on.
  const shown = sortWatchlist(symbols, Object.fromEntries(symbols.map((x) => [x, pctSince(added[x], prices[x])])), sort);
  const canMove = sort === "mine";

  return (
    <div className="watchlist-terminal space-y-4">
      <header className="terminal-panel">
        <p className="dash-section-title">Watchlist</p>
        <h2 className="font-display text-lg text-signal-prepare/90 mb-1">Tracked symbols</h2>
        <p className="text-xs text-mist/70 max-w-xl">
          Priority universe for scans and alerts. Saved to cloud when backend is connected.
        </p>
        <div className="mono text-[10px] text-mist/50 mt-2">{symbols.length} symbols</div>
      </header>

      <div className="terminal-panel">
        <div className="flex flex-col sm:flex-row gap-2 mb-4">
          <div className="flex items-center gap-2 border border-slate/60 rounded-lg px-3 py-2 bg-ink/60 focus-within:border-signal-prepare/40 transition flex-1">
            <span className="font-mono text-mist text-xs">NSE:</span>
            <input
              value={input}
              onChange={(e) => { setInput(e.target.value.toUpperCase()); setNote(""); }}
              onKeyDown={(e) => e.key === "Enter" && add()}
              placeholder="Add symbol…"
              className="bg-transparent outline-none flex-1 font-mono text-xs placeholder:text-mist/30"
              spellCheck={false}
              autoComplete="off"
            />
          </div>
          <Button variant="ghost" onClick={() => add()}>
            Add
          </Button>
          {onScanWatchlist && (
            <Button variant="ghost" onClick={onScanWatchlist} disabled={symbols.length === 0}>
              Scan watchlist
            </Button>
          )}
        </div>

        {note && <p className="mk-wl-note" role="status">{note}</p>}

        {/* design v5: summary of the figures below (percentages only) */}
        {symbols.length > 0 && (
          <WatchlistPulse items={symbols.map((sym) => ({ symbol: sym, pct: pctSince(added[sym], prices[sym]) }))} onAnalyse={onAnalyse} />
        )}
        {/* design v5: one-tap suggestions while the list is short; uses the same add() as the Add button */}
        {symbols.length < 5 && (
          <div className="ai-suggest" aria-label="Quick add">
            <span>Quick add</span>
            {["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN"].filter((x) => !symbols.includes(x)).slice(0, 5).map((x) => (
              <button type="button" key={x} onClick={() => add(x)}>+ {x}</button>
            ))}
          </div>
        )}

        {symbols.length > 1 && (
          <div className="mk-sortbar">
            <Tabs label="Watchlist order" items={WATCH_SORTS.map((o) => ({ id: o.id, label: o.label }))} value={sort} onChange={setSort} />
            <span className="mk-sorthint">{canMove ? "Drag a card, or use the arrows, to reorder. The order is saved." : "Sorted by % since added. Switch to My order to reorder."}</span>
          </div>
        )}

        {symbols.length === 0 ? (
          <EmptyState title="Your watchlist is empty" hint="Add NSE symbols above to follow them and scan them together." />
        ) : (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {shown.map((s, idx) => (
              <div
                key={s}
                draggable={canMove}
                onDragStart={canMove ? (e) => { setDragging(s); e.dataTransfer.effectAllowed = "move"; try { e.dataTransfer.setData("text/plain", s); } catch { /* some browsers refuse: the state still holds the symbol */ } } : undefined}
                onDragOver={canMove && dragging ? (e) => e.preventDefault() : undefined}
                onDrop={canMove && dragging ? (e) => { e.preventDefault(); const next = dropSymbol(symbols, dragging, s); setDragging(null); if (next.some((x, i) => x !== symbols[i])) onChange(next); } : undefined}
                onDragEnd={() => setDragging(null)}
                data-dragging={dragging === s ? "true" : undefined}
                className="mk-wl-card flex items-center justify-between gap-2 border border-slate/50 rounded-lg px-3 py-2 bg-ink/40 hover:border-signal-prepare/30 transition w-lazy"
                data-tone={tone(pctSince(added[s], prices[s]))}
              >
                <span className="mk-wl-avatar" aria-hidden="true">{s.slice(0, 1)}</span>
                <div className="flex flex-col items-start min-w-0">
                  <button
                    type="button"
                    onClick={() => onAnalyse(s)}
                    className="font-mono text-sm text-paper hover:text-signal-prepare transition"
                  >
                    {s}
                  </button>
                  {(() => {
                    const pct = pctSince(added[s], prices[s]);
                    const t = tone(pct);
                    const cls = t === "up" ? "text-signal-buy" : t === "down" ? "text-signal-sell" : "text-mist/50";
                    return (
                      <span
                        className={`mono text-[10px] ${cls}${isBigMove(pct) ? " font-semibold" : ""}`}
                        title={added[s] ? `${label(added[s])}: baseline Rs ${added[s].price.toFixed(2)}` : "no price yet"}
                        data-testid={`since-added-${s}`}
                      >
                        {pct === null ? "no price yet" : <>{t === "up" ? <i className="mk-arrow" aria-hidden="true">▲</i> : t === "down" ? <i className="mk-arrow" aria-hidden="true">▼</i> : null}{`${formatPct(pct)} ${label(added[s])}`}</>}{isBigMove(pct) ? " ●" : ""}
                      </span>
                    );
                  })()}
                </div>
                {canMove && symbols.length > 1 && (
                  <span className="mk-move">
                    <button type="button" aria-label={`Move ${s} earlier`} disabled={idx === 0} onClick={() => onChange(moveSymbol(symbols, idx, idx - 1))}>▲</button>
                    <button type="button" aria-label={`Move ${s} later`} disabled={idx === shown.length - 1} onClick={() => onChange(moveSymbol(symbols, idx, idx + 1))}>▼</button>
                  </span>
                )}
                <button
                  type="button"
                  onClick={() => remove(s)}
                  className="font-mono text-[10px] text-mist/50 hover:text-signal-sell uppercase"
                >
                  Remove
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
