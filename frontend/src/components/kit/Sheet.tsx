// components/kit/Sheet.tsx (E534) - modal: bottom sheet on phones, side panel on desktop. Rendered in a portal on document.body so a transformed
// ancestor can never become its containing block (the old fixed-popup bug noted in design-v3.css). Esc and backdrop close; focus is moved in, kept
// inside, and returned to the opener on close; the page behind does not scroll.
import { useEffect, useRef, type ReactNode } from "react";
import { createPortal } from "react-dom";

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Sheet({ open, onClose, title, children, footer }: { open: boolean; onClose: () => void; title: string; children: ReactNode; footer?: ReactNode }) {
  const panel = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    panel.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") { closeRef.current(); return; }
      if (e.key !== "Tab" || !panel.current) return;
      const items = Array.from(panel.current.querySelectorAll<HTMLElement>(FOCUSABLE));
      if (!items.length) { e.preventDefault(); return; }
      const first = items[0], last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.current)) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); document.body.style.overflow = overflow; opener?.focus?.(); };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div className="k-sheet-root">
      <button type="button" className="k-sheet-backdrop" aria-label="Close" onClick={onClose} tabIndex={-1} />
      <div className="k-sheet" role="dialog" aria-modal="true" aria-label={title} tabIndex={-1} ref={panel}>
        <div className="k-sheet-head">
          <h2 className="k-sheet-title">{title}</h2>
          <button type="button" className="k-sheet-close" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="k-sheet-body">{children}</div>
        {footer != null && <div className="k-sheet-foot">{footer}</div>}
      </div>
    </div>,
    document.body,
  );
}
