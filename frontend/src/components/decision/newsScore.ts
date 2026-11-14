// E536 (redesign round 4): the pure news helpers of the decision card, moved out of DecisionCard.tsx (code unchanged except `export`).
import type { Decision } from "../../api";

// ── Types & helper for structured news (from v2) ──
export interface NewsItem {
  title: string;
  publisher: string;
  published: string;
  url: string;
}

export function getNewsItems(data: Decision): NewsItem[] {
  const items: NewsItem[] = [];

  if (data.event_data && typeof data.event_data === 'object') {
    const newsArray = data.event_data['news'] || data.event_data['recent_news'];
    if (Array.isArray(newsArray)) {
      for (const n of newsArray) {
        if (n.title) {
          items.push({
            title: n.title,
            publisher: n.publisher || '',
            published: n.published || '',
            url: n.url || '',
          });
        }
      }
    }
  }

  if (items.length === 0 && data.reasons.news) {
    for (const title of data.reasons.news) {
      items.push({ title, publisher: '', published: '', url: '' });
    }
  }

  return items;
}

// ── Sentiment scoring (from v2) ──
export function computeNewsSentimentScore(newsItems: NewsItem[]): number {
  if (!newsItems.length) return 50;

  const positiveWords = new Set([
    'beat', 'surpass', 'growth', 'strong', 'record', 'outperform', 'positive',
    'upbeat', 'rally', 'surge', 'jump', 'gain', 'profit', 'upgrade', 'buy',
    'bullish', 'recovery', 'breakthrough', 'exceed', 'rose', 'higher',
    'earnings', 'revenue', 'income', 'profit', 'margin', 'expansion'
  ]);
  const negativeWords = new Set([
    'miss', 'decline', 'drop', 'fall', 'warning', 'cut', 'downgrade',
    'loss', 'sell', 'bearish', 'slump', 'plunge', 'collapse', 'debt',
    'default', 'investigation', 'lawsuit', 'bankruptcy', 'layoff',
    'deficit', 'deteriorate'
  ]);

  const positivePhrases = [
    'earnings call', 'strong revenue', 'revenue growth', 'profit beat',
    'outperform', 'record high', 'positive outlook'
  ];
  const negativePhrases = [
    'earnings miss', 'revenue miss', 'profit warning', 'downgrade',
    'debt warning', 'losses'
  ];

  let score = 50;
  let totalWeight = 0;

  for (const item of newsItems) {
    const title = item.title.toLowerCase();
    const words = title.split(/\s+/);
    let pos = 0, neg = 0;

    for (const w of words) {
      const clean = w.replace(/[^a-z]/g, '');
      if (positiveWords.has(clean)) pos++;
      if (negativeWords.has(clean)) neg++;
    }

    let phraseBonus = 0;
    for (const phrase of positivePhrases) {
      if (title.includes(phrase)) phraseBonus += 3;
    }
    for (const phrase of negativePhrases) {
      if (title.includes(phrase)) phraseBonus -= 3;
    }

    const weight = Math.min(1, words.length / 4);
    const impact = (pos - neg) * 4 + phraseBonus;
    score += impact * weight;
    totalWeight += weight;
  }

  const avgImpact = totalWeight > 0 ? (score - 50) / totalWeight : 0;
  const clampedAvg = Math.min(30, Math.max(-30, avgImpact));
  const finalScore = 50 + clampedAvg;
  return Math.min(100, Math.max(0, Math.round(finalScore)));
}


export function formatWhen(iso?: string | null): string {
  if (!iso) return "";
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso);
    return d.toLocaleString("en-IN", {
      timeZone: "Asia/Kolkata",
      day: "2-digit",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true,
    }) + " IST";
  } catch {
    return String(iso);
  }
}

