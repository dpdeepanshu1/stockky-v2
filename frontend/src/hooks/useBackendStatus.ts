import { useEffect, useState } from "react";
import { api, getApiUrl, wakeService } from "../api";

// E70 - E31 step 14: the backend up / down check and the wake button, moved out of App.tsx (code unchanged).
// Pings once on mount; a successful ping also reloads the watchlist (setWatchlist comes from useWatchlist).
export function useBackendStatus(setWatchlist: (s: string[]) => void) {
  const [backendUp, setBackendUp] = useState<"checking" | "up" | "down">("checking");
  const [isWaking, setIsWaking] = useState(false);

  function checkBackend() {
    setBackendUp("checking");
    api
      .ping()
      .then(() => {
        setBackendUp("up");
        api.getWatchlist().then((r) => setWatchlist(r.symbols)).catch(() => {});
      })
      .catch(() => setBackendUp("down"));
  }

  async function handleWakeBackend() {
    setIsWaking(true);
    try {
      await wakeService(getApiUrl());
      setTimeout(() => {
        checkBackend();
        setIsWaking(false);
      }, 3000);
    } catch {
      setIsWaking(false);
      checkBackend();
    }
  }

  useEffect(() => {
    checkBackend();
  }, []);

  return { backendUp, isWaking, checkBackend, handleWakeBackend };
}
