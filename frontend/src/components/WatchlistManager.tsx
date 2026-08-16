import { useState } from "react";

interface Props {
  symbols: string[];
  onChange: (symbols: string[]) => void;
  onAnalyse: (symbol: string) => void;
  onScanWatchlist?: () => void; // NEW
}

export default function WatchlistManager({ symbols, onChange, onAnalyse, onScanWatchlist }: Props) {
  const [input, setInput] = useState("");

  function add() {
    const sym = input.trim().toUpperCase();
    if (!sym || symbols.includes(sym)) { setInput(""); return; }
    onChange([...symbols, sym]);
    setInput("");
  }

  function remove(sym: string) {
    onChange(symbols.filter((s) => s !== sym));
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <h2 className="font-mono text-xs text-mist uppercase tracking-widest">Watchlist</h2>
        <span className="font-mono text-[10px] text-mist/40">{symbols.length} symbols · saved to cloud</span>
      </div>

      {/* Add input */}
      <div className="flex gap-2 mb-5">
        <div className="flex items-center gap-2 border border-slate rounded-lg px-3 py-2 bg-ink/60 focus-within:border-signal-prepare/60 transition flex-1">
          <span className="font-mono text-mist text-xs">NSE:</span>
          <input
            value={input}
            onChange={(e) => setInput(e.target.value.toUpperCase())}
            onKeyDown={(e) => e.key === "Enter" && add()}
            placeholder="Add symbol…"
            className="bg-transparent outline-none flex-1 font-mono text-xs placeholder:text-mist/30"
            spellCheck={false}
          />
        </div>
        <button
          onClick={add}
          className="border border-slate rounded-lg px-4 py-2 font-mono text-xs text-mist hover:text-paper hover:border-mist transition"
        >
          Add
        </button>
      </div>

      {/* Action buttons */}
      <div className="flex gap-2 mb-4">
        {onScanWatchlist && (
          <button
            onClick={onScanWatchlist}
            className="font-mono text-xs border border-slate rounded-lg px-4 py-1.5 hover:border-signal-prepare hover:text-paper transition"
          >
            🔍 Scan Watchlist
          </button>
        )}
      </div>

      {/* Symbol grid */}
      {symbols.length === 0 ? (
        <p className="font-mono text-xs text-mist/40 text-center py-6">No symbols yet. Add one above.</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {symbols.map((s) => (
            <div
              key={s}
              className="flex items-center gap-1 border border-slate rounded-md bg-ink/40 pl-3 pr-1 py-1.5 group"
            >
              <button
                onClick={() => onAnalyse(s)}
                className="font-mono text-xs text-mist hover:text-paper transition"
              >
                {s}
              </button>
              <button
                onClick={() => remove(s)}
                className="ml-1 w-4 h-4 rounded flex items-center justify-center text-mist/30 hover:text-signal-sell hover:bg-signal-sell/10 transition font-mono text-[10px]"
              >
                ×
              </button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}