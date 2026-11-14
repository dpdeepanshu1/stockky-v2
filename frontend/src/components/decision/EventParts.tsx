// E536 (redesign round 4): the event and news-with-fundamentals panels of the decision card, moved out of DecisionCard.tsx (code unchanged except `export`).
import { useState, useEffect } from "react";
import { eventChips } from "../../eventChips";
import { api, FundamentalMetrics, CategorizedEvents } from "../../api";
import { formatWhen, type NewsItem } from "./newsScore";

// ── EventSection (v1) ──
export function EventSection({ symbol, compact }: { symbol: string; compact?: boolean }) {
  const [events, setEvents] = useState<CategorizedEvents | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    api
      .getSymbolEvents(symbol)
      .then((r) => { if (!cancelled) setEvents(r); })
      .catch(() => { if (!cancelled) setError(true); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [symbol]);

  if (loading) {
    if (compact) return null;
    return (
      <div className="rounded-2xl border border-slate/40 bg-graphite/30 p-4 text-xs text-mist/40 font-display tabular-nums flex items-center gap-2">
        <span className="inline-block w-3 h-3 rounded-full border-2 border-t-transparent border-mist/40 animate-spin" />
        Loading events...
      </div>
    );
  }
  if (error || !events) return null;
  const hasAnything = events.upcoming.length > 0 || events.recent.length > 0 || events.recent_changes.length > 0;
  if (!hasAnything) return null;

  return (
    <div className={compact ? "mt-2" : "rounded-2xl border border-slate/60 bg-graphite/50 p-5"}>
      {!compact && (
        <h4 className="font-display tabular-nums text-xs text-mist uppercase tracking-widest mb-3">📅 Event Update</h4>
      )}

      {events.recent_changes.length > 0 && (
        <div className="mb-4 bg-signal-prepare/10 border border-signal-prepare/30 rounded-xl p-3">
          <div className="font-display tabular-nums text-[10px] text-signal-prepare uppercase tracking-wider mb-2">
            Newly Detected
          </div>
          <ul className="space-y-1.5">
            {events.recent_changes.map((c, i) => (
              <li key={i} className="text-sm text-paper flex gap-2">
                <span className="text-signal-prepare shrink-0">●</span>
                <span>{c}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {events.upcoming.length > 0 && (
        <div className="mb-3">
          <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider mb-2">Upcoming</div>
          <ul className="space-y-1.5">
            {events.upcoming.map((e, i) => (
              <li key={i} className="text-sm text-mist/80 flex gap-2">
                <span className="text-slate mt-1 shrink-0">–</span>
                <span>
                  {e.description}
                  {(e.date || (e as any).datetime) && (
                    <span className="block font-display tabular-nums text-[10px] text-mist/45 mt-0.5">
                      {formatWhen(e.date || (e as any).datetime)}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {events.recent.length > 0 && (
        <div>
          <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider mb-2">Previous</div>
          <ul className="space-y-1.5">
            {events.recent.slice(0, 5).map((e, i) => (
              <li key={i} className="text-sm text-mist/70 flex gap-2">
                <span className="text-slate mt-1 shrink-0">–</span>
                <span>
                  {e.description}
                  {(e.date || (e as any).datetime) && (
                    <span className="block font-display tabular-nums text-[10px] text-mist/45 mt-0.5">
                      {formatWhen(e.date || (e as any).datetime)}
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}

// ── EventDataView (v2) — clean, readable event cards ──
function EventDataView({ data }: { data: Record<string, unknown> }) {
  const skipKeys = new Set([
    "news",
    "recent_news",
    "symbol",
    "raw",
    "raw_response",
    "debug",
  ]);

  const formatVal = (v: unknown): string => {
    if (v == null) return "";
    if (typeof v === "boolean") return v ? "Yes" : "No";
    if (typeof v === "number") return String(v);
    if (typeof v === "string") {
      const s = v.trim();
      // ISO-ish timestamps
      if (/^\d{4}-\d{2}-\d{2}T/.test(s)) {
        try {
          return new Date(s).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" });
        } catch {
          return s;
        }
      }
      // Hide mega Google News redirect URLs in plain text dumps
      if (s.length > 160 && (s.includes("http") || s.includes("news.google"))) {
        return s.slice(0, 120) + "…";
      }
      return s;
    }
    if (Array.isArray(v)) return v.map(formatVal).filter(Boolean).join("; ");
    if (typeof v === "object") {
      const o = v as Record<string, unknown>;
      const title = o.title || o.name || o.description || o.headline;
      if (typeof title === "string" && title.trim()) return title.trim();
      try {
        return JSON.stringify(o);
      } catch {
        return String(v);
      }
    }
    return String(v);
  };

  const shortUrl = (url: string) => {
    try {
      const u = new URL(url);
      return u.hostname.replace(/^www\./, "");
    } catch {
      return "link";
    }
  };

  const renderNewsish = (items: unknown[], key: string) => {
    if (!items.length) return null;
    return (
      <div key={key} className="space-y-2">
        <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">
          {key.replace(/_/g, " ")}
        </div>
        <ul className="space-y-2">
          {items.slice(0, 8).map((raw, idx) => {
            if (typeof raw === "string") {
              // Parse "Title - publisher · published: … · url: …"
              let title = raw;
              let meta = "";
              const urlMatch = raw.match(/https?:\/\/\S+/);
              const url = urlMatch ? urlMatch[0].replace(/[),.;]+$/, "") : "";
              title = raw
                .replace(/\s*[·•]\s*url:\s*https?:\/\/\S+/gi, "")
                .replace(/\s*url:\s*https?:\/\/\S+/gi, "")
                .replace(/\s*https?:\/\/\S+/g, "")
                .trim();
              const pubMatch = title.match(/\b(?:publisher|published):\s*([^·•]+)/i);
              if (pubMatch) meta = pubMatch[1].trim();
              title = title
                .replace(/\bpublisher:\s*[^·•]+/gi, "")
                .replace(/\bpublished:\s*[^·•]+/gi, "")
                .replace(/\s*[·•]\s*/g, " · ")
                .replace(/\s{2,}/g, " ")
                .trim();
              return (
                <li
                  key={idx}
                  className="rounded-xl border border-slate/40 bg-ink/40 px-3 py-2 text-sm"
                >
                  <div className="text-paper leading-snug font-medium">{title || "Event"}</div>
                  <div className="mt-1 flex flex-wrap gap-2 text-[10px] font-display tabular-nums text-mist/50">
                    {meta && <span>{meta}</span>}
                    {url && (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-signal-prepare hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {shortUrl(url)} ↗
                      </a>
                    )}
                  </div>
                </li>
              );
            }
            if (raw && typeof raw === "object") {
              const o = raw as Record<string, any>;
              const title = String(o.title || o.name || o.description || o.event || "Item");
              const publisher = o.publisher || o.source || "";
              const published = o.published || o.published_at || o.date || o.datetime || "";
              const url = typeof o.url === "string" ? o.url : "";
              let pubLabel = "";
              if (published) {
                try {
                  pubLabel = new Date(String(published)).toLocaleString("en-IN", {
                    dateStyle: "medium",
                    timeStyle: "short",
                  });
                } catch {
                  pubLabel = String(published);
                }
              }
              return (
                <li
                  key={idx}
                  className="rounded-xl border border-slate/40 bg-ink/40 px-3 py-2 text-sm"
                >
                  <div className="text-paper leading-snug font-medium">{title}</div>
                  <div className="mt-1 flex flex-wrap gap-2 text-[10px] font-display tabular-nums text-mist/50">
                    {publisher && <span>{String(publisher)}</span>}
                    {pubLabel && <span>{pubLabel}</span>}
                    {eventChips(o).map((c, ci) => (
                      <span key={ci} className={c.tone === "buy" ? "text-signal-buy" : c.tone === "sell" ? "text-signal-sell" : "uppercase tracking-wider"}>
                        {c.text}
                      </span>
                    ))}
                    {url && (
                      <a
                        href={url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-signal-prepare hover:underline"
                        onClick={(e) => e.stopPropagation()}
                      >
                        {shortUrl(url)} ↗
                      </a>
                    )}
                  </div>
                </li>
              );
            }
            return null;
          })}
        </ul>
      </div>
    );
  };

  const entries = Object.entries(data).filter(([key]) => !skipKeys.has(key));
  if (entries.length === 0) {
    return <p className="text-sm text-mist/40 italic">No additional event data.</p>;
  }

  // Prefer structured news-like arrays
  const preferredOrder = [
    "classified_events",
    "events",
    "results",
    "upcoming",
    "recent",
    "recent_changes",
    "summary",
    "checked_at",
    "cached",
  ];
  entries.sort((a, b) => {
    const ia = preferredOrder.indexOf(a[0]);
    const ib = preferredOrder.indexOf(b[0]);
    return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
  });

  return (
    <div className="space-y-3">
      {entries.map(([key, value]) => {
        if (value == null || value === "") return null;
        const label = key.replace(/_/g, " ").replace(/\b\w/g, (l) => l.toUpperCase());

        if (Array.isArray(value)) {
          if (value.length === 0) return null;
          // News / event objects or messy strings
          if (
            value.every(
              (item) =>
                typeof item === "string" ||
                (typeof item === "object" && item !== null)
            )
          ) {
            return renderNewsish(value, label);
          }
          return (
            <div key={key}>
              <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider mb-1">
                {label}
              </div>
              <p className="text-sm text-mist/80">{formatVal(value)}</p>
            </div>
          );
        }

        if (typeof value === "object") {
          const o = value as Record<string, unknown>;
          // Nested list inside object
          for (const nestedKey of ["items", "events", "results", "news"]) {
            if (Array.isArray(o[nestedKey])) {
              return renderNewsish(o[nestedKey] as unknown[], label);
            }
          }
          const title = formatVal(o.title || o.summary || o.description);
          return (
            <div key={key} className="rounded-xl border border-slate/40 bg-ink/40 px-3 py-2">
              <div className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider mb-1">
                {label}
              </div>
              {title ? (
                <p className="text-sm text-paper leading-snug">{title}</p>
              ) : (
                <p className="text-xs text-mist/60">{formatVal(value)}</p>
              )}
            </div>
          );
        }

        // Scalar: checked_at, cached, summary, etc.
        const display = formatVal(value);
        if (!display) return null;
        const isMeta = ["checked_at", "cached", "event_type", "type"].includes(key);
        return (
          <div
            key={key}
            className={
              isMeta
                ? "flex justify-between gap-4 font-display tabular-nums text-[10px] text-mist/50"
                : "rounded-xl border border-slate/40 bg-ink/40 px-3 py-2"
            }
          >
            <span className={isMeta ? "uppercase tracking-wider" : "font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider block mb-1"}>
              {label}
            </span>
            <span className={isMeta ? "text-right text-mist/70" : "text-sm text-paper leading-snug"}>
              {display}
            </span>
          </div>
        );
      })}
    </div>
  );
}


// ── NewsItemWithFundamentals (v2) ──
export function NewsItemWithFundamentals({
  item,
  isExpanded,
  onToggle,
  fundamentalMetrics,
  fundamentalScore,
  impact,
  impactDescription,
}: {
  item: NewsItem;
  isExpanded: boolean;
  onToggle: () => void;
  fundamentalMetrics?: FundamentalMetrics;
  fundamentalScore?: number;
  impact: 'positive' | 'negative' | 'neutral';
  impactDescription: string;
}) {
  const date = item.published ? new Date(item.published) : null;
  const formattedDate = date
    ? date.toLocaleDateString('en-IN', {
        day: '2-digit',
        month: 'short',
        year: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
        timeZone: 'UTC',
      }) + ' UTC'
    : 'Date not available';

  const impactColor = impact === 'positive' ? 'text-signal-buy' :
                      impact === 'negative' ? 'text-signal-sell' :
                      'text-signal-hold';
  const impactEmoji = impact === 'positive' ? '📈' :
                      impact === 'negative' ? '📉' :
                      '⚪';

  return (
    <div className="border-b border-paper dark:border-slate py-2 last:border-0">
      <div className="flex items-start gap-2">
        <span className="text-sm mt-0.5">{impactEmoji}</span>
        <div className="flex-1">
          {item.url ? (
            <a
              href={item.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-signal-prepare dark:text-signal-prepare hover:underline font-medium"
            >
              {item.title}
            </a>
          ) : (
            <span className="font-medium text-ink dark:text-paper">{item.title}</span>
          )}
          <div className="flex items-center gap-3 mt-0.5 flex-wrap">
            <div className="text-xs text-mist dark:text-mist">
              {item.publisher && <span className="mr-2">{item.publisher}</span>}
              <span>{formattedDate}</span>
            </div>
            <span className={`text-[10px] font-medium ${impactColor}`}>
              {impact.toUpperCase()} impact
            </span>
            <button
              onClick={onToggle}
              className="text-[10px] font-display tabular-nums text-mist/50 hover:text-mist transition border border-slate/30 rounded px-2 py-0.5"
            >
              {isExpanded ? 'Hide Fundamentals' : 'Show Fundamentals'}
            </button>
          </div>
        </div>
      </div>

      {isExpanded && fundamentalMetrics && (
        <div className="mt-2 ml-6 p-3 bg-ink/30 rounded-xl border border-slate/40">
          <div className="flex items-center justify-between mb-2">
            <span className="font-display tabular-nums text-[10px] text-mist/50 uppercase tracking-wider">
              📊 Fundamental Impact
            </span>
            <span className="text-xs text-mist/60">
              Score: <span className="font-display tabular-nums font-medium text-paper">{fundamentalScore ?? 'N/A'}</span>
            </span>
          </div>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
            {fundamentalMetrics.revenue_growth !== null && fundamentalMetrics.revenue_growth !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Revenue Growth</div>
                <div className={`text-sm font-display tabular-nums ${fundamentalMetrics.revenue_growth > 0 ? 'text-signal-buy' : 'text-signal-sell'}`}>
                  {fundamentalMetrics.revenue_growth.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.earnings_growth !== null && fundamentalMetrics.earnings_growth !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Earnings Growth</div>
                <div className={`text-sm font-display tabular-nums ${fundamentalMetrics.earnings_growth > 0 ? 'text-signal-buy' : 'text-signal-sell'}`}>
                  {fundamentalMetrics.earnings_growth.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.roe !== null && fundamentalMetrics.roe !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">ROE</div>
                <div className={`text-sm font-display tabular-nums ${fundamentalMetrics.roe > 15 ? 'text-signal-buy' : fundamentalMetrics.roe > 0 ? 'text-signal-hold' : 'text-signal-sell'}`}>
                  {fundamentalMetrics.roe.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.debt_to_equity !== null && fundamentalMetrics.debt_to_equity !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Debt/Equity</div>
                <div className={`text-sm font-display tabular-nums ${fundamentalMetrics.debt_to_equity < 1 ? 'text-signal-buy' : fundamentalMetrics.debt_to_equity < 2 ? 'text-signal-hold' : 'text-signal-sell'}`}>
                  {Number(fundamentalMetrics.debt_to_equity).toFixed(2)}x
                </div>
              </div>
            )}
            {fundamentalMetrics.profit_margins !== null && fundamentalMetrics.profit_margins !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">Net Margin</div>
                <div className={`text-sm font-display tabular-nums ${fundamentalMetrics.profit_margins > 10 ? 'text-signal-buy' : fundamentalMetrics.profit_margins > 0 ? 'text-signal-hold' : 'text-signal-sell'}`}>
                  {fundamentalMetrics.profit_margins.toFixed(1)}%
                </div>
              </div>
            )}
            {fundamentalMetrics.pe_ratio !== null && fundamentalMetrics.pe_ratio !== undefined && (
              <div className="bg-ink/40 rounded px-2 py-1">
                <div className="text-[9px] text-mist/50 uppercase">P/E Ratio</div>
                <div className={`text-sm font-display tabular-nums ${fundamentalMetrics.pe_ratio < 20 ? 'text-signal-buy' : fundamentalMetrics.pe_ratio < 30 ? 'text-signal-hold' : 'text-signal-sell'}`}>
                  {fundamentalMetrics.pe_ratio.toFixed(1)}
                </div>
              </div>
            )}
          </div>
          <div className="mt-2 text-xs text-mist/70 border-t border-slate/30 pt-2">
            {impactDescription}
          </div>
          <div className="mt-1 text-[10px] text-mist/40">
            💡 News sentiment computed from headlines — score reflects overall tone.
          </div>
        </div>
      )}
    </div>
  );
}

