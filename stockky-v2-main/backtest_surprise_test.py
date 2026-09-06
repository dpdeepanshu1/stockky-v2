"""
backtest_surprise_test.py — replicates score_stock() from
services/api-gateway/surprise_scanner.py against 1y NSE EOD bhavcopy data,
using symbols that Groww's own "Volume shockers" screen surfaced on
31-Aug-2026 (see uploaded screenshots).

Runs the scoring logic TWICE per stock's biggest recent move day:
  - "BEFORE" : the original buggy defaults (vwap=current_price,
               orb_high=day_high) — reproduces the bug exactly.
  - "AFTER"  : the fixed proxies now in surprise_scanner.py.

This proves the ORB/VWAP bucket was always 0 before, and is now genuinely
reachable, without changing anything else about the scoring model.

NOTE: the bhavcopy file only has EOD data through 2026-08-28 (previous
trading session) — today's (31-Aug) live intraday move for these names
isn't in an EOD file yet. So this validates the FIX on each stock's most
recent real high-RVOL move in the data, not literally today's tick.
"""
import pandas as pd
import numpy as np

df = pd.read_csv('/mnt/user-data/uploads/nse_bhavdata_delivery_1y.csv')
df.columns = [c.strip() for c in df.columns]
df = df[df['SERIES'].str.strip() == 'EQ'].copy()
df['SESSION_DATE'] = pd.to_datetime(df['SESSION_DATE'])
df = df.sort_values(['SYMBOL', 'SESSION_DATE'])

# Symbols from Groww's "Volume shockers" screen (31-Aug-2026 screenshots)
candidates = {
    'INDGN': 'Indegene', 'JAIBALAJI': 'Jai Balaji Industries', 'RELIGARE': 'Religare Enterprises',
    'ASTRAL': 'Astral', 'SHIPROCKET': 'Shiprocket', 'TTKHLTCARE': 'TTK Healthcare', 'UTIAMC': 'UTI AMC',
    'KRT': 'Knowledge Realty Trust', 'SHRIRAMPPS': 'Shriram Properties', 'QLINE': 'Q-Line Biotech',
    'MODISONLTD': 'Modison', 'KAJARIACER': 'Kajaria Ceramics', 'IRB': 'IRB Infrastructure Developers',
    'CREATIVEYE': 'Creative Peripherals & Distribution', 'STOVEKRAFT': 'Stove Kraft',
    'STLTECH': 'STL (Sterlite Tech)', 'PARACABLES': 'Paramount Communications', 'DOMS': 'DOMS Industries',
    'TMB': 'Tamilnad Mercantile Bank', 'ICIL': 'Indo Count Industries', 'COCKERILL': 'John Cockerill India',
    'KAPSTON': 'Kapston Services', 'ABLBL': 'Aditya Birla Lifestyle Brands', 'RPGLIFE': 'RPG Life Sciences',
    'LTTS': 'L&T Technology Services', 'VERANDA': 'Veranda Learning Solutions', 'KPRMILL': 'K.P.R. Mill',
    'AUROPHARMA': 'Aurobindo Pharma', 'CRAFTSMAN': 'Craftsman Automation', 'ASHOKA': 'Ashoka Buildcon',
    'DIFFNKG': 'Diffusion Engineers', 'AYMSYNTEX': 'Aym Syntex', 'MANALIPETC': 'Manali Petrochemicals',
    'ALIVUS': 'Alivus Life Sciences', 'VIMTALABS': 'Vimta Labs', 'NIACL': 'New India Assurance',
    'VENKEYS': "Venky's India", 'ANONDITA': 'Anondita Medicare', 'ASIANHOTNR': 'Asian Hotels (North)',
    'BAJAJHCARE': 'Bajaj Healthcare', 'BODALCHEM': 'Bodal Chemicals', 'BALRAMCHIN': 'Balrampur Chini Mills',
    'INDORAMA': 'Indo Rama Synthetics', 'TARIL': 'Transformers & Rectifiers (India)',
    'APCOTEXIND': 'Apcotex Industries', 'AVANTEL': 'Avantel', 'SUNDARAM': 'Sundaram-Clayton',
    'ULTRAMAR': 'Ultramarine & Pigments', 'ECLERX': 'eClerx Services', 'LMW': 'LMW',
    'CARRARO': 'Carraro India', 'JUBLINGREA': 'Jubilant Agri And Consumer Products',
}

# --- constants, mirrored 1:1 from surprise_scanner.py ---
MIN_SCORE = 65
MIN_CHANGE_PCT = 1.5
BUILDING_MIN_SCORE = 35
BUILDING_MIN_CHANGE_PCT = 0.3
RVOL_SLOPE_MIN = 0.6
DIST_52W_BREAKOUT_PCT = 8.0
DIST_52W_NEAR_PCT = 15.0
SHOCKER_MIN_CHANGE_PCT = 5.0
SHOCKER_MIN_RVOL = 2.0
ORB_ATR_FRACTION = 0.3
ORB_FALLBACK_PCT = 0.005


def score_row(row, avg_vol, atr, high_52w, prev_rvol, fixed: bool):
    prev_close = row['PREV_CLOSE']
    current_price = row['CLOSE_PRICE']
    open_price = row['OPEN_PRICE']
    day_high = row['HIGH_PRICE']
    day_low = row['LOW_PRICE']
    vol = row['TTL_TRD_QNTY']

    price_change_pct = round(((current_price - prev_close) / prev_close) * 100, 2) if prev_close else 0
    rvol = round(vol / avg_vol, 2) if avg_vol else 0
    rvol_slope = round(rvol - prev_rvol, 2) if prev_rvol is not None else 0.0

    # no live order-book data available from EOD bhav feed -> production default
    buy_pct = 50.0

    if fixed:
        # AFTER: proxies that don't trivially equal/bound current_price
        vwap = (open_price + day_high + day_low) / 3.0
        orb_buffer = (atr * ORB_ATR_FRACTION) if atr and atr > 0 else (open_price * ORB_FALLBACK_PCT)
        orb_high = open_price + orb_buffer
    else:
        # BEFORE (the bug): vwap==current_price, orb_high==day_high
        # (in EOD terms day_high IS the session high, same as the old
        # tick.get("high") default) -- both structurally block the bucket.
        vwap = current_price
        orb_high = day_high

    score = 0
    trigger = "Consolidation"
    if rvol >= 3.5: score += 35
    elif rvol >= 2.0: score += 20
    elif rvol >= 1.5: score += 10

    if rvol_slope >= RVOL_SLOPE_MIN:
        score += 10
        if trigger == "Consolidation": trigger = "Volume Accelerating"

    if current_price > orb_high and current_price > vwap:
        score += 25; trigger = "Morning ORB Breakout"
    elif current_price > vwap:
        score += 13
        if trigger in ("Consolidation", "Volume Accelerating"): trigger = "Above VWAP"

    if buy_pct >= 75: score += 15
    elif buy_pct >= 65: score += 8
    elif buy_pct >= 60: score += 4

    dist = round(((high_52w - current_price) / high_52w) * 100, 2) if high_52w else 100.0
    if dist <= DIST_52W_BREAKOUT_PCT:
        score += 15
        if score >= 50: trigger = "Near 52W High"
    elif dist <= DIST_52W_NEAR_PCT:
        score += 7

    intraday_range = max(0.0, day_high - day_low)
    if atr and intraday_range >= (atr * 0.8):
        score += 10
        if trigger == "Consolidation": trigger = "Range Expansion"

    if score >= MIN_SCORE and price_change_pct > MIN_CHANGE_PCT:
        tier = "breakout"
    elif (price_change_pct >= SHOCKER_MIN_CHANGE_PCT and rvol >= SHOCKER_MIN_RVOL
          and current_price > prev_close):
        tier = "breakout"; trigger = "Volume Shocker (override)"
    elif score >= BUILDING_MIN_SCORE and price_change_pct > BUILDING_MIN_CHANGE_PCT:
        tier = "building"
        if trigger == "Consolidation": trigger = "Early Accumulation"
    else:
        tier = "none"

    return score, tier, trigger, price_change_pct, rvol, dist


def run(fixed: bool):
    results = []
    for sym, name in candidates.items():
        g = df[df['SYMBOL'] == sym].copy()
        if g.empty:
            results.append((sym, name, None, None, None, None, None, None, None, "NOT FOUND IN DATA"))
            continue
        g = g.reset_index(drop=True)
        g['pct_chg'] = ((g['CLOSE_PRICE'] - g['PREV_CLOSE']) / g['PREV_CLOSE']) * 100
        recent = g.tail(90).copy()
        if recent.empty:
            continue
        idx = recent['pct_chg'].idxmax()
        row = g.loc[idx]
        i = g.index.get_loc(idx)
        if i < 20:
            continue
        window = g.iloc[max(0, i - 20):i]
        avg_vol = window['TTL_TRD_QNTY'].mean()
        win14 = g.iloc[max(0, i - 14):i]
        atr = (win14['HIGH_PRICE'] - win14['LOW_PRICE']).mean()
        win252 = g.iloc[max(0, i - 252):i]
        high_52w = win252['HIGH_PRICE'].max() if not win252.empty else row['HIGH_PRICE']
        prev_rvol = None
        if i >= 21:
            prev_row = g.iloc[i - 1]
            prev_avg_vol = g.iloc[max(0, i - 21):i - 1]['TTL_TRD_QNTY'].mean()
            prev_rvol = round(prev_row['TTL_TRD_QNTY'] / prev_avg_vol, 2) if prev_avg_vol else None

        score, tier, trigger, pct, rvol, dist = score_row(row, avg_vol, atr, high_52w, prev_rvol, fixed)
        results.append((sym, name, row['SESSION_DATE'].date(), pct, rvol, dist, score, tier, trigger, None))
    return results


def render(results, label):
    print(f"\n=== {label} ===")
    print(f"{'SYMBOL':<12}{'NAME':<26}{'DATE':<12}{'%CHG':>7}{'RVOL':>7}{'DIST52W%':>10}{'SCORE':>7}  TIER      TRIGGER")
    for r in results:
        sym, name, date, pct, rvol, dist, score, tier, trigger, note = r
        if date is None:
            print(f"{sym:<12}{name:<26}{'':12}{'':7}{'':7}{'':10}{'':7}  {note}")
            continue
        print(f"{sym:<12}{name:<26}{str(date):<12}{pct:>7.2f}{rvol:>7.2f}{dist:>10.2f}{score:>7}  {tier:<9} {trigger}")


before = run(fixed=False)
after = run(fixed=True)

render(before, "BEFORE FIX (vwap=current_price, orb_high=day_high) - reproduces the bug")
render(after, "AFTER FIX (typical-price vwap proxy, ATR-based orb_high proxy)")


def tally(results):
    counts = {"breakout": 0, "building": 0, "none": 0, "missing": 0}
    orb_vwap_hits = 0
    for r in results:
        if r[2] is None:
            counts["missing"] += 1
            continue
        counts[r[7]] += 1
        if "ORB Breakout" in r[8] or "Above VWAP" in r[8]:
            orb_vwap_hits += 1
    return counts, orb_vwap_hits


b_counts, b_orb = tally(before)
a_counts, a_orb = tally(after)

print("\n=== SUMMARY ===")
print(f"{'':20}{'breakout':>10}{'building':>10}{'none':>8}{'missing':>9}{'orb/vwap pts hit':>20}")
print(f"{'BEFORE (buggy)':20}{b_counts['breakout']:>10}{b_counts['building']:>10}{b_counts['none']:>8}{b_counts['missing']:>9}{b_orb:>20}")
print(f"{'AFTER (fixed)':20}{a_counts['breakout']:>10}{a_counts['building']:>10}{a_counts['none']:>8}{a_counts['missing']:>9}{a_orb:>20}")
