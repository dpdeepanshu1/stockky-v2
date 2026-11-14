// components/kit/DataTable.tsx (E534) - sticky header, right-aligned numbers, optional keyboard-friendly row click.
import type { KeyboardEvent, ReactNode, Ref } from "react";
import { Empty } from "./Skeleton";

export interface Column<T> { key: string; header: ReactNode; render: (row: T) => ReactNode; align?: "left" | "right"; /** E539: extra class on the cell (shading), by row */ cellClass?: (row: T) => string }

export function DataTable<T>({ columns, rows, rowKey, onRowClick, caption, empty, dense = false, rowClass, wrapRef }: {
  columns: Column<T>[]; rows: T[]; rowKey: (row: T) => string; onRowClick?: (row: T) => void; caption?: string; empty?: ReactNode; dense?: boolean;
  /** E539: extra class on a row (the option chain marks its ATM row) */ rowClass?: (row: T) => string;
  /** E539: the scrolling wrapper, so a page can scroll a row into view */ wrapRef?: Ref<HTMLDivElement>;
}) {
  if (!rows.length) return <>{empty ?? <Empty title="Nothing here yet" />}</>;
  const key = (row: T) => (e: KeyboardEvent<HTMLTableRowElement>) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onRowClick?.(row); }
  };
  return (
    <div className="k-table-wrap" ref={wrapRef}>
      <table className={`k-table ${dense ? "dense" : ""}`}>
        {caption && <caption>{caption}</caption>}
        <thead>
          <tr>{columns.map((c) => <th key={c.key} scope="col" className={c.align === "right" ? "r" : ""}>{c.header}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={rowKey(row)} className={`${onRowClick ? "click" : ""}${rowClass ? ` ${rowClass(row)}` : ""}`.trim()} tabIndex={onRowClick ? 0 : undefined} onClick={onRowClick ? () => onRowClick(row) : undefined} onKeyDown={onRowClick ? key(row) : undefined}>
              {columns.map((c) => <td key={c.key} className={`${c.align === "right" ? "r" : ""}${c.cellClass ? ` ${c.cellClass(row)}` : ""}`.trim()}>{c.render(row)}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
