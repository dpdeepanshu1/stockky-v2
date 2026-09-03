import { useState } from "react";

interface Props {
  symbols: string[];
  onChange: (symbols: string[]) => void;
  onAnalyse: (symbol: string) => void;
  onScanWatchlist?: () => void;
}

export default function WatchlistManager({ symbols, onChange, onAnalyse, onScanWatchlist }: Props) {
  const [input, setInput] = useState("");

  function add() {
    const sym = input.trim().toUpperCase().replace(/\.NS$/i, "").replace(/\.BO$/i, "");
    if (!sym || symbols.includes(sym)) {
      setInput("");
      return;
    }
    onChange([...symbols, sym]);
    setInput("");
  }

  function remove(sym: string) {
    onChange(symbols.filter((s) => s !== sym));
  }

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
              onChange={(e) => setInput(e.target.value.toUpperCase())}
              onKeyDown={(e) => e.key === "Enter" && add()}
              placeholder="Add symbol…"
              className="bg-transparent outline-none flex-1 font-mono text-xs placeholder:text-mist/30"
              spellCheck={false}
              autoComplete="off"
            />
          </div>
          <button type="button" onClick={add} className="btn-terminal">
            Add
          </button>
          {onScanWatchlist && (
            <button
              type="button"
              onClick={onScanWatchlist}
              className="btn-terminal"
              disabled={symbols.length === 0}
            >
              Scan watchlist
            </button>
          )}
        </div>

        {symbols.length === 0 ? (
          <p className="mono text-xs text-mist/50 py-6 text-center">
            Empty — add NSE symbols to prioritize in market scans.
          </p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {symbols.map((s) => (
              <div
                key={s}
                className="flex items-center justify-between gap-2 border border-slate/50 rounded-lg px-3 py-2 bg-ink/40 hover:border-signal-prepare/30 transition"
              >
                <button
                  type="button"
                  onClick={() => onAnalyse(s)}
                  className="font-mono text-sm text-paper hover:text-signal-prepare transition"
                >
                  {s}
                </button>
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
