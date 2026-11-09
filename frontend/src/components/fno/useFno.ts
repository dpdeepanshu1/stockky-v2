// polling hook shared by the F&O panels: calls `fn` now and every `everyMs`, keeps the last good value, never throws into render
import { useCallback, useEffect, useRef, useState } from "react";

export function useFno<T>(fn: () => Promise<T>, everyMs: number, deps: unknown[] = []): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const alive = useRef(true);
  const load = useCallback(async () => {
    try { const d = await fnRef.current(); if (alive.current) { setData(d); setError(null); } }
    catch (e: any) { if (alive.current) setError(String(e?.message || e)); }
    finally { if (alive.current) setLoading(false); }
  }, []);
  useEffect(() => {
    alive.current = true;
    setLoading(true);
    void load();
    const id = everyMs > 0 ? setInterval(() => { if (!document.hidden) void load(); }, everyMs) : null;
    return () => { alive.current = false; if (id) clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, everyMs, ...deps]);
  return { data, error, loading, reload: () => void load() };
}
