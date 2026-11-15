// round 555 (E580, Tredye plan P6): the order-flow section of the stock popup. READ ONLY and DISPLAY ONLY: it reads the admin route GET /flow/symbol/{symbol} (the tape the
// entry gate already uses) every 15 s while the popup is open and the tab is visible. Words and rules live in popupFlowView.ts (popupFlow.test.mjs): an estimate label, nothing
// at all when the tape is unavailable or unreliable, every number stamped with its read time. Without the admin login it shows one line saying so.
import { useEffect, useState } from "react";
import { getSessionToken, positionStocksApi } from "../../positionStocksApi";
import type { SymbolTapeDoc } from "../../tapeSymbolView";
import { popupFlowView } from "../../popupFlowView";
import { setVisibleInterval } from "../../visibleInterval";

export default function PopupFlow({ symbol }: { symbol: string }) {
  const [doc, setDoc] = useState<SymbolTapeDoc | null>(null);
  const [at, setAt] = useState<number | undefined>(undefined);
  const authed = !!getSessionToken();
  useEffect(() => {
    setDoc(null); setAt(undefined);
    if (!symbol || !getSessionToken()) return;
    let alive = true;
    const load = async () => {
      try {
        const d = await positionStocksApi.flowSymbol(symbol);
        if (alive) { setDoc(d); setAt(Date.now()); }
      } catch { if (alive) { setDoc(null); setAt(undefined); } }       // a failed read shows nothing, never the previous read as current
    };
    void load();
    const stop = setVisibleInterval(load, 15000);
    return () => { alive = false; stop(); };
  }, [symbol]);
  if (!authed) return <div className="ob-note" data-testid="popup-flow-login">Order flow estimate: log in as admin to see it.</div>;
  const v = popupFlowView(doc, at);
  if (v.kind !== "ok") return null;       // unavailable or unreliable tape: nothing is shown
  return (
    <div className="ob-mom" data-testid="popup-flow">
      <div className="ob-mom-head"><span>{v.title}</span><b>{v.asOf} IST</b></div>
      <div className="ob-mom-kv">
        {v.lines.map((l) => <div key={l.key}><span>{l.label}</span><b>{l.value}</b></div>)}
      </div>
      <div className="ob-note">{v.note}</div>
    </div>
  );
}
