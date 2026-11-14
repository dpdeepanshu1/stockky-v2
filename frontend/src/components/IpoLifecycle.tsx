// frontend/src/components/IpoLifecycle.tsx  (E550 - IPO overview on the kit Card; styles are mk-ipo-* in styles/markets.css)
// Presentation only: counts and ranks the rows IpoTracker already holds. No request, no state of its own.
// Stages are labelled in words with a count; the current-stage highlight is never colour-only (counts + "now" tag).
import type { IpoAnalysis } from "../api";
import { AiOrb } from "./ui/AiOrb";
import { Card } from "./kit/Card";

const STEPS: { key: string; label: string; hint: string }[] = [
  { key: "upcoming", label: "Upcoming", hint: "not listed yet" },
  { key: "pre_listing", label: "Pre-open", hint: "lists today" },
  { key: "listing_day", label: "Listing day", hint: "trading live" },
  { key: "listed", label: "Listed", hint: "already trading" },
];

type Props = {
  list: IpoAnalysis[];
  scanning: boolean;
  progress?: { processed?: number; total?: number; message?: string } | null;
  onPick: (symbol: string) => void;
};

const pctText = (n: number) => `${n < 0 ? "▼" : "▲"} ${n > 0 ? "+" : ""}${n.toFixed(1)}%`;

export default function IpoLifecycle({ list, scanning, progress, onPick }: Props) {
  const count = (k: string) => list.filter((i) => i.stage === k).length;
  const waiting = list.filter((i) => i.stage === "no_data_yet" || i.stage === "error" || i.stage === "unknown").length;
  const pops = list
    .filter((i) => typeof i.listing_pop_pct === "number" && Number.isFinite(i.listing_pop_pct))
    .sort((a, b) => (b.listing_pop_pct as number) - (a.listing_pop_pct as number))
    .slice(0, 3);
  const total = progress?.total ?? 0;
  const pct = total > 0 ? Math.min(100, Math.round(((progress?.processed ?? 0) / total) * 100)) : 0;
  const live = count("pre_listing") + count("listing_day");

  return (
    <Card className="mk-ipo" aria-label="IPO overview">
      <div className="mk-ipo-head">
        <AiOrb state={scanning ? "thinking" : list.length ? "done" : "idle"} size={44} />
        <div className="mk-ipo-text">
          <div className="mk-ipo-eyebrow">IPO OVERVIEW</div>
          <b className="mk-ipo-title">
            {scanning ? `AI is scanning IPOs${pct ? `, ${pct}% done` : ""}` : live > 0 ? `${live} IPO${live === 1 ? "" : "s"} listing right now` : list.length ? `${list.length} IPOs tracked` : "No IPOs loaded yet"}
          </b>
          <span className="mk-ipo-sub">{scanning ? progress?.message || "Checking listing data and prices." : "Follow each IPO from announcement to trading."}</span>
        </div>
      </div>

      <ol className="mk-ipo-steps">
        {STEPS.map((s) => {
          const n = count(s.key);
          return (
            <li key={s.key} className={`mk-ipo-step${n > 0 ? " has" : ""}${(s.key === "pre_listing" || s.key === "listing_day") && n > 0 ? " now" : ""}`}>
              <span className="mk-ipo-dot" aria-hidden="true" />
              <b className="num">{n}</b>
              <span className="mk-ipo-label">{s.label}{(s.key === "pre_listing" || s.key === "listing_day") && n > 0 ? <em> now</em> : null}</span>
              <small>{s.hint}</small>
            </li>
          );
        })}
      </ol>
      {waiting > 0 && <p className="mk-ipo-note">{waiting} waiting for data. Fresh listings can take a day to appear.</p>}

      {pops.length > 0 && (
        <div className="mk-ipo-pops">
          <span className="mk-ipo-pops-title">Biggest listing moves</span>
          {pops.map((i) => (
            <button type="button" key={i.symbol} className="mk-ipo-pop" onClick={() => onPick(i.symbol)}>
              <b>{i.symbol}</b>
              <span className={`num ${(i.listing_pop_pct as number) < 0 ? "down" : "up"}`}>{pctText(i.listing_pop_pct as number)}</span>
            </button>
          ))}
        </div>
      )}
    </Card>
  );
}
