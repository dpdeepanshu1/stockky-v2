// group 463 / F1: one friendly "nothing here yet" block (icon, one line of what it means, optional action) so empty lists look the same on every page.
// It is the kit's Empty with an icon from the app's icon set.
import type { ReactNode } from "react";
import { Empty } from "../kit/Skeleton";
import { NavIcon, type IconName } from "./NavIcon";

export function EmptyState({ title, hint, action, icon = "trend" }: { title: string; hint?: string; action?: ReactNode; icon?: IconName }) {
  return <Empty title={title} hint={hint} action={action} icon={<NavIcon name={icon} />} />;
}
