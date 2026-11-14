// E543: the shared card face of the Markets pages. Presentation only: the page passes the already-formatted card data (marketCardView.ts)
// and its own price node (so a live price keeps rolling the way it does in the table). Clicking the symbol opens the stock, as the table does.
import type { ReactNode } from "react";
import { ScoreRing } from "./ui/ScoreRing";
import { changeArrow, type MarketCardData } from "../marketCardView";

export function MarketCard({ card, price, onSelect }: { card: MarketCardData; price: ReactNode; onSelect?: (symbol: string) => void }) {
  return (
    <article className="k-card mk-card">
      <header className="mk-card-top">
        <div className="mk-card-id">
          <button type="button" className="mk-card-sym" onClick={() => onSelect?.(card.symbol)}>{card.symbol}</button>
          <span className={`mk-badge ${card.badge.tone}`}><i aria-hidden="true">{card.badge.glyph}</i>{card.badge.label}</span>
        </div>
        {card.score != null && <ScoreRing score={card.score} size={56} />}
      </header>
      <div className="mk-card-px">
        <b className="mk-card-price">{price}</b>
        <span className={`mk-card-chg ${card.change.dir ?? "flat"}`}>{changeArrow(card.change.dir)}{card.change.text}</span>
      </div>
      <dl className="mk-card-facts">
        {card.facts.map((f) => (<div key={f.label}><dt>{f.label}</dt><dd>{f.value}</dd></div>))}
      </dl>
      {card.tag && <span className="mk-card-tag">{card.tag}</span>}
    </article>
  );
}
