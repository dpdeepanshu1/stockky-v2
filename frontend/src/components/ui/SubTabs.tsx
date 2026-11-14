// E549 (round 8d): SubTabs is now the kit Tabs (sliding underline, arrow keys, roving tab stop). The name and props stay so the F&O pages need no edit.
import { Tabs } from "../kit/Tabs";
import type { TabItem } from "../kit/Tabs";

export type SubTabItem<T extends string> = TabItem<T>;
export const SubTabs = Tabs;
