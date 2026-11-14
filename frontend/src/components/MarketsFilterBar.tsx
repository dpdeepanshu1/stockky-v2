// E541: the shared filter bar of the Markets pages (search + sort). Presentation only: the page owns the state and does the filtering
// with marketsFilter.ts. The search box is 16px (no iOS zoom); the sort is the kit Tabs (arrow keys, selected state).
import { Tabs } from "./kit";
import { SORT_LABEL, countLine, type MarketSort } from "../marketsFilter";

export function MarketsFilterBar({ query, onQuery, sort, onSort, sorts, shown, total, label }: {
  query: string; onQuery: (q: string) => void; sort: MarketSort; onSort: (s: MarketSort) => void; sorts: MarketSort[]; shown: number; total: number; label: string;
}) {
  if (total === 0 && !query) return null;   // nothing loaded yet: no bar over an empty page
  return (
    <div className="mk-filter" role="search" aria-label={label}>
      <input
        className="mk-search" type="search" value={query} placeholder="Search symbol or name" aria-label={`${label}: search`}
        onChange={(e) => onQuery(e.target.value)} spellCheck={false} autoComplete="off"
      />
      <Tabs label={`${label}: sort`} items={sorts.map((s) => ({ id: s, label: SORT_LABEL[s] }))} value={sort} onChange={onSort} />
      <span className="mk-count" role="status">{countLine(shown, total)}</span>
    </div>
  );
}

/** What a page shows in place of its list when the filter hides every row. */
export function NoMatch({ query, onClear }: { query: string; onClear: () => void }) {
  return (
    <p className="mk-nomatch" role="status">
      No match{query.trim() ? ` for "${query.trim()}"` : ""}. <button type="button" onClick={onClear}>Clear search</button>
    </p>
  );
}
