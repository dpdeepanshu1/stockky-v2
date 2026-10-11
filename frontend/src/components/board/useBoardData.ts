// group306: polls GET /board with the visible-tab helper (2 s while the market is open, 30 s when closed). A failed poll keeps the
// last good payload on screen and reports the error; it never blanks the board.
import { useCallback, useEffect, useRef, useState } from "react";
import { setVisibleInterval } from "../../visibleInterval";
import { positionStocksApi, getPositionStocksApiUrl } from "../../positionStocksApi";
import type { BoardPayload } from "./boardTypes";

export function useBoardData(universe: string, active: boolean) {
  const [data, setData] = useState<BoardPayload | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const open = useRef(true);

  const load = useCallback(async () => {
    if (!getPositionStocksApiUrl()) { setError("Position Stocks service URL isn't set (Position Stocks → Settings)."); return; }
    setLoading(true);
    try {
      const j = (await positionStocksApi.board(universe, 130)) as BoardPayload;
      open.current = !!j.market_open;
      setData(j); setError(null);
    } catch (e: any) {
      setError(e?.message || "board request failed");
    } finally { setLoading(false); }
  }, [universe]);

  useEffect(() => {
    if (!active) return;
    void load();
    let stop = setVisibleInterval(load, open.current ? 2000 : 30000);
    let last = open.current;
    const watch = setInterval(() => {            // re-time the poll when the market opens / closes
      if (open.current !== last) { last = open.current; stop(); stop = setVisibleInterval(load, last ? 2000 : 30000); }
    }, 5000);
    return () => { stop(); clearInterval(watch); };
  }, [active, load]);

  return { data, error, loading, reload: load };
}

export function usePrefersReducedMotion(): boolean {
  const [r, setR] = useState(false);
  useEffect(() => {
    if (typeof window === "undefined" || !window.matchMedia) return;
    const m = window.matchMedia("(prefers-reduced-motion: reduce)");
    setR(m.matches);
    const f = () => setR(m.matches);
    m.addEventListener?.("change", f);
    return () => m.removeEventListener?.("change", f);
  }, []);
  return r;
}
