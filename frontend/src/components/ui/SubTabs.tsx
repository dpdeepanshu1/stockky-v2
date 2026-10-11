import type { ReactNode } from "react";

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
  return (
    <div className="subtabs" role="tablist" aria-label={label}>
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
    </div>
  );
}
