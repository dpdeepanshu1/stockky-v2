// E536 (redesign round 4): the two scan buttons (Quick scan, Full market scan) folded into one small menu next to Analyse.
// UI state only (open / closed): the click handlers are the ones the dashboard already had.
import { useEffect, useRef, useState } from "react";

export function ScanMenu({ onQuick, onFull }: { onQuick: () => unknown; onFull: () => unknown }) {
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);

  const pick = (fn: () => unknown) => () => { setOpen(false); void fn(); };

  return (
    <div className="hm-scan" ref={box}>
      <button type="button" className="k-btn soft" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        Scan the market <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <div className="hm-scan-menu" role="menu" aria-label="Scan the market">
          <button type="button" role="menuitem" onClick={pick(onQuick)}
            title="Quick scan: uses the stored data feed and instant scores, with minimal load on the services (recommended on the free tier)">
            <b>Quick scan</b>
            <small>Stored data and instant scores. Light on the services.</small>
          </button>
          <button type="button" role="menuitem" onClick={pick(onFull)}
            title="Full scan: runs every check (technical, fundamental, news, events, prediction). Slower.">
            <b>Full market scan</b>
            <small>Runs every check. Slower.</small>
          </button>
        </div>
      )}
    </div>
  );
}
