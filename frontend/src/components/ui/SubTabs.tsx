import { useLayoutEffect, useRef, type ReactNode } from "react";

// One tab control for every service page (Auto Trade, Position Stocks, …).
// Before this, each page hand-rolled its own bar — one pill-shaped, one
// uppercase-underlined — so the same kind of navigation looked and behaved
// differently on each page. Styling lives in index.css (.subtabs/.subtab).
export interface SubTabItem<T extends string> {
  id: T;
  label: ReactNode;
  /** optional count shown as a small badge, e.g. open positions */
  count?: number | null;
}

export function SubTabs<T extends string>({
  items,
  value,
  onChange,
  label,
}: {
  items: SubTabItem<T>[];
  value: T;
  onChange: (id: T) => void;
  label: string;
}) {
  // group 460: a 3px underline glides to the selected tab (transform only; CSS in design-v4.css turns the transition off for reduced-motion / TV)
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
  return (
    <div className="subtabs v4-slide" role="tablist" aria-label={label} ref={bar}>
      {items.map((t) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={value === t.id}
          className="subtab"
          onClick={() => onChange(t.id)}
        >
          {t.label}
          {t.count != null && t.count > 0 && <span className="count">{t.count}</span>}
        </button>
      ))}
      <span className="v4-ind" ref={ind} aria-hidden="true" />
    </div>
  );
}
