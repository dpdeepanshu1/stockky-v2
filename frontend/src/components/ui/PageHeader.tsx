// Page title + one plain-language line saying what the page is for, so each
// tab explains itself instead of relying on the user remembering what
// "Surprise" or "Data Feed" means.
// E148 (design v7 on every tab): when a page passes `icon` and `kicker`, the header is drawn as the shared v6/v7 hero (same as Auto Trade,
// Position Stocks and Calibration, including its "Animations: On/Off" switch). Without them it stays the plain header, so nothing else changes.
// The hero draws its title as an h2, so the page keeps one h1 for screen readers (visually hidden, same words).
import { PageHero } from "./PageHero";
import type { IconName } from "./NavIcon";

export function PageHeader({ title, desc, icon, kicker }: { title: string; desc?: string; icon?: IconName; kicker?: string }) {
  if (icon && kicker) {
    return (
      <>
        <h1 className="sr-only">{title}</h1>
        <PageHero icon={icon} title={title} kicker={kicker} subtitle={desc} />
      </>
    );
  }
  return (
    <header className="page-header">
      <h1 className="page-title">{title}</h1>
      {desc && <p className="page-desc">{desc}</p>}
    </header>
  );
}
