// frontend/src/components/IpoLifecycle.tsx  (design v5 - IPO overview; styles in design-v5.css)
// Presentation only: counts and ranks the rows IpoTracker already holds. No request, no state of its own.
// Stages are labelled in words with a count; the current-stage highlight is never colour-only (counts + "now" tag).
import type { IpoAnalysis } from "../api";
import { AiOrb } from "./ui/AiOrb";

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
    <section className="v5-ipo" aria-label="IPO overview">
      <div className="v5-ipo-head">
        <AiOrb state={scanning ? "thinking" : list.length ? "done" : "idle"} size={44} />
        <div className="v5-ipo-text">
          <div className="v5-eyebrow">IPO OVERVIEW</div>
          <b className="v5-ipo-title">
            {scanning ? `AI is scanning IPOs${pct ? `, ${pct}% done` : ""}` : live > 0 ? `${live} IPO${live === 1 ? "" : "s"} listing right now` : list.length ? `${list.length} IPOs tracked` : "No IPOs loaded yet"}
          </b>
          <span className="v5-ipo-sub">{scanning ? progress?.message || "Checking listing data and prices." : "Follow each IPO from announcement to trading."}</span>
        </div>
      </div>

      <ol className="v5-ipo-steps">
        {STEPS.map((s, i) => {
          const n = count(s.key);
          return (
            <li key={s.key} className={`v5-ipo-step${n > 0 ? " has" : ""}${(s.key === "pre_listing" || s.key === "listing_day") && n > 0 ? " now" : ""}`} style={{ animationDelay: `${i * 70}ms` }}>
              <span className="v5-ipo-dot" aria-hidden="true" />
              <b className="num">{n}</b>
              <span className="v5-ipo-label">{s.label}{(s.key === "pre_listing" || s.key === "listing_day") && n > 0 ? <em> now</em> : null}</span>
              <small>{s.hint}</small>
            </li>
          );
        })}
      </ol>
      {waiting > 0 && <p className="v5-ipo-note">{waiting} waiting for data. Fresh listings can take a day to appear.</p>}

      {pops.length > 0 && (
        <div className="v5-ipo-pops">
          <span className="v5-ipo-pops-title">Biggest listing moves</span>
          {pops.map((i) => (
            <button type="button" key={i.symbol} className="v5-ipo-pop" onClick={() => onPick(i.symbol)}>
              <b>{i.symbol}</b>
              <span className={`num ${(i.listing_pop_pct as number) < 0 ? "down" : "up"}`}>{pctText(i.listing_pop_pct as number)}</span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
