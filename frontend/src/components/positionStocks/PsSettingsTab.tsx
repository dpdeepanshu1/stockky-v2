// E538 (redesign round 5b): the "settings" sub-tab of Position Stocks, moved out of PositionStocksTab.tsx (JSX unchanged).
// All state and handlers come from usePositionStocks() through `s`, so the ten sub-tabs still share one set of state.
import type { PsState } from "./usePositionStocks";

export function PsSettingsTab({ s }: { s: PsState }) {
  const { apiUrlInput, setApiUrlInput, saveApiUrl } = s;
  return (
      <>
      {/* ── Settings ── */}
      <details open className="bg-graphite border border-slate rounded-2xl p-4">
        <summary className="font-display tabular-nums text-xs text-mist cursor-pointer">▶ Settings</summary>
        <p className="font-display tabular-nums text-[10px] text-mist mt-2 mb-1">Position Stocks service URL</p>
        <div className="flex gap-2">
          <input
            className="flex-1 bg-ink border border-slate rounded-xl px-3 py-2 font-display tabular-nums text-xs text-paper focus:outline-none"
            value={apiUrlInput}
            onChange={e => setApiUrlInput(e.target.value)}
          />
          <button onClick={saveApiUrl} className="px-4 py-2 rounded-xl bg-signal-buy/20 border border-signal-buy/40 font-display tabular-nums text-xs text-signal-buy">Save</button>
        </div>
      </details>
      </>
      );
}
