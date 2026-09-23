import { useEffect, useMemo, useRef, useState } from "react";

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
};

export default function CommandPalette({ open, onClose, actions }: Props) {
  const [q, setQ] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return actions;
    return actions.filter((a) => {
      const hay = `${a.label} ${a.hint || ""} ${a.group || ""} ${a.keywords || ""}`.toLowerCase();
      return hay.includes(needle);
    });
  }, [actions, q]);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setActive(0);
    const t = setTimeout(() => inputRef.current?.focus(), 30);
    return () => clearTimeout(t);
  }, [open]);

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
      } else if (e.key === "Enter") {
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
    <div className="cmdk-overlay" onClick={onClose} role="dialog" aria-modal="true">
      <div className="cmdk-panel" onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="cmdk-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Jump to page, stock, or action…"
          autoComplete="off"
          spellCheck={false}
        />
        <div className="cmdk-list">
          {filtered.length === 0 && (
            <div className="cmdk-item" style={{ cursor: "default", opacity: 0.6 }}>
              No matches
            </div>
          )}
          {filtered.map((a, i) => (
            <div
              key={a.id}
              className="cmdk-item"
              data-active={i === active ? "true" : "false"}
              onMouseEnter={() => setActive(i)}
              onClick={() => {
                onClose();
                a.run();
              }}
            >
              <span>
                {a.group ? <span style={{ opacity: 0.55 }}>{a.group} · </span> : null}
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
