// E537 (redesign round 5): two-level tabs for the trading pages. The top row is the group; a second row appears only when the group holds more than one page.
// It only changes which old tab id is active (the page's own setter), so every tab id and every page body stays as it was.
import { Tabs } from "../kit/Tabs";
import { groupOf, tabForGroup, type TabGroup } from "./tradingGroups";

export function GroupTabs<T extends string>({ groups, items, value, onChange, label }: {
  groups: TabGroup<T>[]; items: { id: T; label: string; count?: number }[]; value: T; onChange: (id: T) => void; label: string;
}) {
  const g = groupOf(groups, value);
  const sub = g.tabs.map((id) => items.find((i) => i.id === id)).filter((i): i is { id: T; label: string; count?: number } => !!i);
  return (
    <div className="tr-tabs">
      <Tabs<string> label={label} value={g.id} onChange={(id) => onChange(tabForGroup(groups, id, value))} items={groups.map((x) => ({ id: x.id, label: x.label }))} />
      {sub.length > 1 && <Tabs<T> label={`${g.label} pages`} value={value} onChange={onChange} items={sub} />}
    </div>
  );
}
