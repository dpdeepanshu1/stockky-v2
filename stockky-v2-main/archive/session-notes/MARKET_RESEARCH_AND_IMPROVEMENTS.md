# Market Research & Trading Logic Improvements
**Date:** 28-Aug-2026 | Independent analysis — not from existing code

---

## Part 1: Market Research (What I Found)

### Nifty 50 — Multi-Timeframe Picture

| Timeframe | Return | Signal |
|-----------|--------|--------|
| 1 Day     | −0.48% (broke 24,150 trendline) | 🔴 BEARISH |
| 1 Week    | Negative (2 consecutive down sessions) | 🔴 BEARISH |
| 1 Month   | Range-bound 24,000–24,500 | 🟡 CHOPPY |
| 3 Month   | ~25,200 → 24,090 = **−4.4%** | 🔴 WEAK |
| 6 Month   | ~26,000 → 24,090 = **−7.3%** | 🔴 CORRECTION |
| 1 Year    | Nifty50 **−1.08%** YoY (as of Aug 14) | 🔴 UNDERPERFORMING |
| 2 Year    | ~19,500 → 24,090 = **+23.5%** | 🟢 SOLID LONG BASE |

**52-Week Range:** High 26,373 (Jan 5) → Low 22,182 → Current 24,090
- Position: 43% of annual range — lower half, **not overbought**
- Critical support: **₹24,000** (DII floor). Break → 23,800–23,650
- Resistance: 24,380 → 24,500–24,600

### Institutional Flows (27-Aug-2026)
- **FII:** NET SELLER −₹298 Cr cash. Short **1,97,792 futures contracts**.
  Buying puts (+1,09,678), selling calls (+98,129) = fully hedged/defensive.
  FII ownership fallen from 22.5% → below 17% (decade-low).
- **DII:** NET BUYER +₹4,977 Cr on a single day.
  ₹8.92 lakh Cr injected in 1 year vs FII outflows ₹4.84 lakh Cr.
- **Conclusion:** FII short + DII long = floor exists at 24,000 but no
  strong upside catalyst. Choppy range with a downside bias.

### Macroeconomic Context
- RBI repo rate: **5.25%** (NEUTRAL — paused after 125 bps cuts in 2025)
- FY27 GDP: **6.7%** (raised — fundamentally healthy economy)
- Inflation: **4.38%** Jun'26 (above 4% target; within 2–6% band)
- Middle East conflict: uncertainty on crude → inflation path unclear

### Sector Performance — What Is Actually Making Money

| Sector | YTD/1Y | Verdict |
|--------|--------|---------|
| PSU Banks (SBI, PNB, Union Bank) | **+29% YTD** | ✅ BUY dips |
| Auto | **+22% YTD** | ✅ Rate cuts fuelling demand |
| Private Banks | **+15% YTD** | ✅ Credit growth |
| **Midcap100** | **+12.88% 1Y** vs Nifty −1.08% | ✅ OUTPERFORMING |
| **Smallcap100** | **+12.49% 1Y** | ✅ DII money rotating here |
| Metals | Outperforming in Aug sessions | ✅ Selective |
| IT/Tech | **−12% YTD** | ❌ Trump tariffs, export headwinds |
| Pharma | **−4% YTD** | ❌ Avoid |
| Energy | **−3% YTD** | ❌ Crude uncertainty |

### My Trading Conclusions (If I Were Buying Right Now)

1. **Do NOT buy large-cap Nifty50 IT/pharma/energy** — FIIs are net short,
   index trend is down. Buying against institutional flow loses more often.

2. **DO focus on midcap/smallcap PSU banks, auto, metals** — that's where
   DII money is going and 12–29% returns are being made.

3. **Raise the conviction bar** — in a choppy market, borderline signals
   (score 45–55) lose more than they win. Only take high-conviction (≥55).

4. **Require multi-TF strength** — a stock must be bullish on 4+ of 7
   timeframes. One-day events in a weak market reverse quickly.

5. **Volume must confirm** — low-volume moves in choppy markets are fake.
   Institutional absence = no follow-through.

6. **Don't buy near resistance** — in a ranging market, stocks that reach
   recent highs face immediate selling. Poor R:R.

7. **Protect profits faster** — in a choppy market, open gains evaporate.
   Take more at first target (60%), trail the rest tightly.

8. **Tighten trail as trade ages** — a position that hasn't performed in
   8+ days in this market is dead capital. Tighten the trail and move on.

9. **Break-even stop is essential** — once up 1×ATR, move stop to entry.
   Creates a free-ride floor. Critical in a market prone to sharp reversals.

10. **Emergency gap exit** — FIIs are short, gap-downs happen. If price
    gaps 1.5× below original stop, exit immediately. Don't wait.

---

## Part 2: What Changed in the Code

### File 1: `candidate_engine/candidates.py` — Complete Rewrite

**Original:** No quality filter at all. Any stock with "BUY NOW" or "PREPARE
TO BUY" from api-gateway got inserted as a candidate. No score floor. No
timeframe analysis. This is why your PARADEEP, DEVYANI, SUZLON positions
are in loss — they were admitted without any quality gate.

**New logic:**
| Gate | Old | New | Why |
|------|-----|-----|-----|
| Min conviction score | None | **≥55** | Choppy market — only high-conviction |
| Timeframes required | None | **≥4 of 7 bullish** | Need multi-TF alignment |
| 6m downtrend block | None | **< −10% rejected** | Broken macro trend |
| 52w overextension | None | **Top 12% rejected** | Poor R:R at yearly highs |
| ATR cap | None | **>7% rejected** | Too volatile for safe sizing |
| Min stock price | None | **<₹20 rejected** | Operator risk, illiquid exits |
| Volume health | None | **<80% of 20d avg = rejected** | Low vol moves reverse |
| Near resistance | None | **Within 2% of 20d high = rejected** | Choppy market |
| Surprise tab | Not wired | **Wired via cached endpoint** | 3rd signal source |

Also added: symbol deduplication (keeps highest conviction), open-position
pre-filter (skips expensive MTF fetch for already-held stocks), concurrent
multi-symbol fetching (all symbols analysed in parallel, not sequentially).

### File 2: `entry_engine/entry.py` — Full Rewrite

**Original:** No market regime check. Always entered at mid-zone regardless
of whether price had moved away from signal. No R:R enforcement. No
conviction-based sizing.

**New logic:**
| Gate | Old | New | Why |
|------|-----|-----|-----|
| Market regime (REAL) | None | **Score <38 blocks entries** | Don't fight FII shorts |
| Entry drift check | None | **>0.75×ATR from signal = skip** | No chasing in choppy market |
| Reward:risk floor | Not enforced | **<2.0:1 rejected** | Only high-quality setups |
| Conviction sizing | Flat risk% | **±25% based on score** | More capital on strong signals |
| Notification detail | Basic | **Includes R:R, market score, conviction** | Full audit trail |

**Entry drift logic:** If price has run >0.75×ATR above signal → chasing,
skip. If dropped >0.75×ATR below signal → move may be done, skip. Only
enter when price is still close to where the signal fired.

**Conviction sizing formula:**
- Score 90 → +25% more capital (e.g. 1% risk → 1.25%)
- Score 65 → no change (1% → 1%)
- Score 55 → −25% less capital (1% → 0.75%)

### File 3: `risk_engine/engine.py` — Full Rewrite

**Original:** Missing two critical caps. Check order was suboptimal.

**New checks added:**
| Check | What it does |
|-------|-------------|
| **Position concentration cap (new)** | Single position ≤25% of equity. Forces diversification. Prevents one gap-down from destroying account. |
| **Min price floor (new)** | Stocks <₹20 rejected. Sub-₹20 = operator risk + wide spreads + illiquid exits in India. |
| **Check order optimized** | Global pause → market closed → daily loss → concurrent positions. Cheap checks first, expensive aggregations last. |

SELL side still bypasses all BUY-only checks — exits can never be blocked
by entry-sizing guards.

### File 4: `exit_engine/exit.py` — Full Rewrite

**Original:** Fixed 50% partial exit, fixed 1.5×ATR trail regardless of
age, no breakeven stop, no emergency gap handler, 10-day time stop with no
early warning.

**New logic:**
| Feature | Old | New | Why |
|---------|-----|-----|-----|
| Partial exit fraction | 50% | **60%** | Lock in more in choppy market |
| Trail multiplier | Fixed 1.5×ATR | **Age-aware: 2.0→1.5→1.0×ATR** | Protect gains as trade matures |
| Breakeven stop | None | **Auto at 1×ATR gain** | Free-ride floor in choppy conditions |
| Emergency gap exit | None | **1.5× loss mult triggers exit** | Catch gap-through-stop scenarios |
| Time stop warning | None | **Day 6 early warning logged** | Visibility before day-10 exit |
| Target after partial | Stays | **Nullified → pure trail** | No stale re-trigger |

**Age-aware trail schedule:**
- Day 0–3: 2.0×ATR — let the trade breathe, filter out noise stops
- Day 4–7: 1.5×ATR — standard, same as original
- Day 8+: 1.0×ATR — very tight, protect profit on slow-moving trade

### File 5: `config.py` — New Constants Appended

All 20 new thresholds are added as named constants at the bottom of config.py,
all readable from environment variables. Admin can tune any parameter via
Render env without code changes.

---

## Files Changed
```
services/real-trade-service/candidate_engine/candidates.py  ← full rewrite
services/real-trade-service/entry_engine/entry.py           ← full rewrite
services/real-trade-service/risk_engine/engine.py           ← full rewrite
services/real-trade-service/exit_engine/exit.py             ← full rewrite
services/real-trade-service/config.py                       ← constants appended
MARKET_RESEARCH_AND_IMPROVEMENTS.md                         ← this file
```

**All other files (execution/, auth/, db.py, models.py, portfolio/,
market_feed/, notifier.py, main.py, cycle_runner.py, auto_pilot.py,
manual_engine.py, frontend/, other services) are identical to the original.**

---

## Recommended Env Vars to Set on Render

```bash
# Tighten conviction bar (current choppy market)
CANDIDATE_MIN_CONVICTION=55
CANDIDATE_MIN_BULLISH_TF=4

# R:R floor — 2:1 minimum in weak market
ENTRY_MIN_REWARD_RISK=2.0

# Regime gate — block entries when Nifty score < 38
ENTRY_REGIME_MIN_SCORE=38

# Concentration cap — no single position > 25% of portfolio
RISK_MAX_POSITION_CONCENTRATION_PCT=25.0

# Lock in 60% at first target — protect profits in choppy conditions
EXIT_PARTIAL_FRACTION=0.60

# Emergency exit at 1.5× stop loss distance (gap-down protection)
EXIT_EMERGENCY_LOSS_MULT=1.5
```

When market conditions improve (Nifty above 25,500, FII flows turn positive,
VIX below 13), loosen these:
- `CANDIDATE_MIN_BULLISH_TF=3`
- `ENTRY_MIN_REWARD_RISK=1.8`
- `ENTRY_REGIME_MIN_SCORE=30`
- `EXIT_PARTIAL_FRACTION=0.50`
