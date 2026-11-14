// components/kit/Tabs.tsx (E534) - tab bar with a sliding underline, arrow-key / Home / End navigation and a roving tab stop.
import { useLayoutEffect, useRef, type KeyboardEvent, type ReactNode } from "react";

export interface TabItem<T extends string> { id: T; label: ReactNode; count?: number | null }

export function Tabs<T extends string>({ items, value, onChange, label }: {
  items: TabItem<T>[]; value: T; onChange: (id: T) => void; label: string;
}) {
  const bar = useRef<HTMLDivElement>(null);
  const ind = useRef<HTMLSpanElement>(null);

  useLayoutEffect(() => {
    const place = () => {
      const el = bar.current?.querySelector<HTMLElement>('[aria-selected="true"]');
      const i = ind.current;
      if (!el || !i) return;
      i.style.transform = `translateX(${el.offsetLeft + 10}px) scaleX(${Math.max(0, el.offsetWidth - 20) / 100})`;
      i.classList.add("ready");
    };
    place();
    const ro = typeof ResizeObserver !== "undefined" && bar.current ? new ResizeObserver(place) : null;
    if (ro && bar.current) ro.observe(bar.current);
    return () => ro?.disconnect();
  }, [value, items.length]);

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!items.length) return;
    const at = Math.max(0, items.findIndex((t) => t.id === value));
    let next = -1;
    if (e.key === "ArrowRight") next = (at + 1) % items.length;
    else if (e.key === "ArrowLeft") next = (at - 1 + items.length) % items.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = items.length - 1;
    if (next < 0) return;
    e.preventDefault();
    onChange(items[next].id);
    window.requestAnimationFrame(() => bar.current?.querySelectorAll<HTMLElement>('[role="tab"]')[next]?.focus());
  };

  return (
    <div className="k-tabs" role="tablist" aria-label={label} ref={bar} onKeyDown={onKey}>
      {items.map((t) => (
        <button key={t.id} type="button" role="tab" className="k-tab" aria-selected={value === t.id} tabIndex={value === t.id ? 0 : -1} onClick={() => onChange(t.id)}>
          {t.label}
          {t.count != null && t.count > 0 && <span className="k-tab-count">{t.count}</span>}
        </button>
      ))}
      <span className="k-tab-ind" ref={ind} aria-hidden="true" />
    </div>
  );
}
