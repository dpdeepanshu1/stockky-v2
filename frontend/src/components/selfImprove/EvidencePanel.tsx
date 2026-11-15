// Self Improve System > Evidence (round 556, E581). What the REAL closed trades say (the nightly evidence_report verdict) and where each strategy stands in the registry
// (testing / proven / watch / demoted / retired, with its drift reading). These are the same aggregates the AI agent reads as context (rounds 551 and 555): no rupee amount.
// Read only: nothing here sets a trading switch, and a "demoted" strategy is a statement about the evidence, not an order. Data: GET /agent/evidence. Pure helpers: selfImproveEvidence.ts.
import { Card, Chip, Msg } from "../calibration/ui";
import { attentionText, attentionTone, lifecycleTone, partNotice, pct, probText, registryChips, registryTotal, verdictCountsText, verdictLabel, verdictMeaning, verdictTone, weakText, type EvidenceView } from "../../selfImproveEvidence";

export default function EvidencePanel({ view, error }: { view: EvidenceView | null; error: string | null }) {
  const b = view?.book_evidence;
  const r = view?.strategy_status;
  const chips = registryChips(r?.counts);
  const att = r?.attention ?? [];
  return (
    <div className="space-y-3" data-testid="si-evidence">
      {error && <Msg kind="err">{error}</Msg>}
      {!view && !error && <div className="text-mist" role="status">Loading the evidence…</div>}

      {view && b && (
        <Card title="Real trades" tone={b.status === "ok" ? (verdictTone(b.verdict) === "bad" ? "bad" : verdictTone(b.verdict) === "good" ? "good" : undefined) : "warn"}>
          <p className="text-mist" data-testid="si-book-text">{b.text}</p>
          {b.status !== "ok" && <p className="mt-1 text-[11px] text-mist">{partNotice(b.status, "book")}</p>}
          {b.status === "ok" && (
            <>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                <Chip tone={verdictTone(b.verdict)}>{verdictLabel(b.verdict)}</Chip>
                <Chip>{b.trades ?? 0} closed trades</Chip>
                <Chip title="Average net return per trade, after charges">mean {pct(b.mean_net_ret_pct)}</Chip>
                <Chip title="90 % lower bound of that average">lower bound {pct(b.lower90_pct)}</Chip>
                <Chip title="Deflated Sharpe probability: how likely the edge is real after allowing for how many ideas were tried">DSR {probText(b.dsr_prob)}</Chip>
                <Chip title="Charges as a share of gross profit">charges {pct(b.charges_pct_of_gross, 1)} of gross</Chip>
              </div>
              <p className="mt-1 text-[11px] text-mist">{verdictMeaning(b.verdict)}</p>
              {verdictCountsText(b.verdict_counts) && <p className="mt-1 text-[11px] text-mist">Across all groups: {verdictCountsText(b.verdict_counts)}.</p>}
              {(b.weakest?.length ?? 0) > 0 && (
                <>
                  <div className="mt-2 text-[11px] font-bold uppercase tracking-widest text-mist">Weakest groups</div>
                  <div className="mt-1 space-y-1" role="list" aria-label="Weakest groups">
                    {b.weakest!.map((w, i) => <div key={`${w.group}${w.key}${i}`} role="listitem" className="rounded-lg border border-slate px-2 py-1 text-[11px]">{weakText(w)}</div>)}
                  </div>
                </>
              )}
            </>
          )}
        </Card>
      )}

      {view && r && (
        <Card title="Strategy registry" tone={r.status !== "ok" ? "warn" : att.some((a) => attentionTone(a) === "bad") ? "bad" : att.length ? "warn" : undefined}>
          <p className="text-mist" data-testid="si-registry-text">{r.text}</p>
          {r.status !== "ok" && <p className="mt-1 text-[11px] text-mist">{partNotice(r.status, "registry")}</p>}
          {r.status === "ok" && (
            <>
              <div className="mt-2 flex flex-wrap items-center gap-2">
                {chips.length === 0 && <Chip>no strategies yet</Chip>}
                {chips.map((c) => <Chip key={c.label} tone={c.tone}>{c.label}</Chip>)}
                {chips.length > 0 && <Chip>{registryTotal(r.counts)} in all</Chip>}
              </div>
              <div className="mt-2 text-[11px] font-bold uppercase tracking-widest text-mist">Needs a look</div>
              {att.length === 0 && <p className="text-mist">No strategy is on watch, demoted or showing drift.</p>}
              <div className="mt-1 space-y-1" role="list" aria-label="Strategies that need a look">
                {att.map((a, i) => (
                  <div key={`${a.strategy}${i}`} role="listitem" className="rounded-lg border border-slate px-2 py-1">
                    <div className="flex flex-wrap items-center gap-2"><Chip tone={lifecycleTone(a.status) ?? attentionTone(a)}>{a.status.toUpperCase()}</Chip><b>{a.strategy}</b></div>
                    <div className="text-[11px] text-mist">{attentionText(a)}</div>
                  </div>
                ))}
              </div>
              <p className="mt-2 text-[11px] text-mist">The list shows up to 6. A strategy is source tab plus decision label, so a change of its settings is blended into one record until a version stamp exists, and a deliberate change can look like drift for a few weeks.</p>
            </>
          )}
        </Card>
      )}

      <p className="text-[11px] text-mist">Read only. These numbers are context: they move no switch, rule or setting. “Demoted” means the evidence is clearly negative after costs; whether to switch a strategy off is your decision.</p>
    </div>
  );
}
