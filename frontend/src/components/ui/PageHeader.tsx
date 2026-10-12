// Page title + one plain-language line saying what the page is for, so each
// tab explains itself instead of relying on the user remembering what
// "Surprise" or "Data Feed" means.
export function PageHeader({ title, desc }: { title: string; desc?: string }) {
  return (
    <header className="page-header">
      <h1 className="page-title">{title}</h1>
      {desc && <p className="page-desc">{desc}</p>}
    </header>
  );
}
