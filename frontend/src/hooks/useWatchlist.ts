import { useState } from "react";
import { api } from "../api";

// E70 - E31 step 14: the watchlist state and its three handlers, moved out of App.tsx (code unchanged).
export function useWatchlist(setStatusMessage: (m: string | null) => void) {
  const [watchlist, setWatchlist] = useState<string[]>([]);

  async function handleWatchlistUpdate(symbols: string[]) {
    await api.setWatchlist(symbols);
    setWatchlist(symbols);
  }

  async function handleAddToWatchlist(symbol: string) {
    try {
      await api.addToWatchlist(symbol);
      const wl = await api.getWatchlist();
      setWatchlist(wl.symbols);
      setStatusMessage(`✅ Added ${symbol} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    } catch (e) {
      console.error("Failed to add to watchlist", e);
      setStatusMessage(`❌ Failed to add ${symbol} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    }
  }

  async function handleAddManyToWatchlist(symbols: string[], label: string) {
    if (symbols.length === 0) {
      setStatusMessage("Nothing to add");
      setTimeout(() => setStatusMessage(null), 3000);
      return;
    }
    try {
      const before = new Set(watchlist);
      const newCount = symbols.filter((s) => !before.has(s.toUpperCase())).length;
      await api.addManyToWatchlist(symbols);
      const wl = await api.getWatchlist();
      setWatchlist(wl.symbols);
      setStatusMessage(
        newCount > 0
          ? `✅ Added ${newCount} new symbol(s) from ${label} (${symbols.length - newCount} already on watchlist)`
          : `All ${symbols.length} symbol(s) from ${label} were already on the watchlist`
      );
      setTimeout(() => setStatusMessage(null), 4000);
    } catch (e) {
      console.error(`Failed to add ${label} to watchlist`, e);
      setStatusMessage(`❌ Failed to add ${label} to watchlist`);
      setTimeout(() => setStatusMessage(null), 3000);
    }
  }

  return { watchlist, setWatchlist, handleWatchlistUpdate, handleAddToWatchlist, handleAddManyToWatchlist };
}
