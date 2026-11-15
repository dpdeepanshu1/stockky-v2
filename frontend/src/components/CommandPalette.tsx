import { useEffect, useId, useMemo, useRef, useState } from "react";

export type CommandAction = {
  id: string;
  label: string;
  hint?: string;
  group?: string;
  keywords?: string;
  run: () => void;
};

type Props = {
  open: boolean;
  onClose: () => void;
  actions: CommandAction[];
  /** group311: when the query looks like a stock symbol, offer one extra action built from it (e.g. open the Market Board popup). */
  symbolAction?: (symbol: string) => CommandAction;
};

export default function CommandPalette({ open, onClose, actions, symbolAction }: Props) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const uid = useId();
  const listId = `${uid}-list`;
  const optId = (i: number) => `${uid}-o${i}`;

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return actions;
    const base = actions.filter((a) => {
      const hay = `${a.label} ${a.hint || ""} ${a.group || ""} ${a.keywords || ""}`.toLowerCase();
      return hay.includes(needle);
    });
    const sym = q.trim().toUpperCase().replace(/\.(NS|BO)$/, "");
    if (symbolAction && /^[A-Z0-9][A-Z0-9&-]{1,19}$/.test(sym)) return [...base, symbolAction(sym)];
    return base;
  }, [actions, q, symbolAction]);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setActive(0);
    const opener = (typeof document !== "undefined" ? document.activeElement : null) as HTMLElement | null;
    const t = setTimeout(() => inputRef.current?.focus(), 30);
    return () => {
      clearTimeout(t);
      // give focus back to what opened the palette, unless the chosen action already moved it somewhere else (a popup, a page)
      try { const a = document.activeElement; if ((!a || a === document.body || a === inputRef.current) && opener && opener.isConnected) opener.focus(); } catch { /* no document */ }
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    try { document.getElementById(optId(active))?.scrollIntoView?.({ block: "nearest" }); } catch { /* no document */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, active]);

  useEffect(() => {
    setActive(0);
  }, [q]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setActive((i) => Math.min(i + 1, Math.max(0, filtered.length - 1)));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setActive((i) => Math.max(i - 1, 0));
      } else if (e.key === "Tab") {
        e.preventDefault();   // one field in the dialog: keep focus on it
      } else if (e.key === "Enter") {
        if (e.isComposing) return;
        e.preventDefault();
        const item = filtered[active];
        if (item) {
          onClose();
          item.run();
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, filtered, active, onClose]);

  if (!open) return null;

  return (
    <div className="cmdk-overlay" onClick={onClose} role="dialog" aria-modal="true" aria-label="Command palette">
      <div className="cmdk-panel" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="cmdk-input"
          role="combobox"
          aria-label="Jump to page, stock, or action"
          aria-expanded="true"
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={filtered[active] ? optId(active) : undefined}
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Jump to page, stock, or action…"
          autoComplete="off"
          spellCheck={false}
        />
        <div className="cmdk-list" id={listId} role="listbox" aria-label="Results">
          {filtered.length === 0 && <div className="cmdk-empty" role="status">No matches</div>}
          {filtered.map((a, i) => (
            <div
              key={a.id}
              id={optId(i)}
              role="option"
              aria-selected={i === active}
              className="cmdk-item"
              data-active={i === active ? "true" : "false"}
              onMouseEnter={() => setActive(i)}
              onClick={() => {
                onClose();
                a.run();
              }}
            >
              <span>
                {a.group ? <span className="cmdk-group">{a.group} · </span> : null}
                {a.label}
              </span>
              {a.hint ? <span className="cmdk-hint">{a.hint}</span> : null}
            </div>
          ))}
        </div>
        <div className="cmdk-footer">
          <span className="kbd">↑↓</span> navigate · <span className="kbd">↵</span> run ·{" "}
          <span className="kbd">esc</span> close
        </div>
      </div>
    </div>
  );
}
