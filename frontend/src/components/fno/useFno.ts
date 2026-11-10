// polling hook shared by the F&O panels: calls `fn` now and every `everyMs`, keeps the last good value, never throws into render
// Round 339 (E341): an answer is shown only if it belongs to the CURRENT inputs (`deps`) and is not older than one already shown (see fnoPoll.ts);
// when the inputs change (another index, expiry, mode, day range, file) what was shown for the old inputs is cleared at once instead of staying on
// screen under the new label until the new answer arrives (or for good if it fails).
import { useCallback, useEffect, useRef, useState } from "react";
import { createPollGate, sameInputs } from "../../fnoPoll";

export function useFno<T>(fn: () => Promise<T>, everyMs: number, deps: unknown[] = []): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const [gate] = useState(createPollGate);
  const lastDeps = useRef<unknown[]>(deps);
  const load = useCallback(async () => {
    const ticket = gate.start();
    let current = false;
    try { const d = await fnRef.current(); current = gate.accept(ticket); if (current) { setData(d); setError(null); } }
    catch (e: any) { current = gate.accept(ticket); if (current) setError(String(e?.message || e)); }
    if (current) setLoading(false);
  }, [gate]);
  useEffect(() => {
    if (!sameInputs(lastDeps.current, deps)) { lastDeps.current = deps; setData(null); setError(null); }
    setLoading(true);
    void load();
    const id = everyMs > 0 ? setInterval(() => { if (!document.hidden) void load(); }, everyMs) : null;
    return () => { gate.reset(); if (id) clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, everyMs, ...deps]);
  return { data, error, loading, reload: () => void load() };
}
