// group 463: one friendly "nothing here yet" block (icon, one line of what it means, optional action) so empty lists look the same on every page.
import type { ReactNode } from "react";
import { NavIcon, type IconName } from "./NavIcon";

export function EmptyState({ title, hint, action, icon = "trend" }: { title: string; hint?: string; action?: ReactNode; icon?: IconName }) {
  return (
    <div className="v4-empty" role="status">
      <span className="v4-empty-icon" aria-hidden="true"><NavIcon name={icon} /></span>
      <p className="v4-empty-title">{title}</p>
      {hint && <p className="v4-empty-hint">{hint}</p>}
      {action && <div className="v4-empty-action">{action}</div>}
    </div>
  );
}
