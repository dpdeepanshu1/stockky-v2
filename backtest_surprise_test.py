import pandas as pd
import numpy as np

df = pd.read_csv('/mnt/user-data/uploads/nse_bhavdata_delivery_1y.csv')
df.columns = [c.strip() for c in df.columns]
df = df[df['SERIES'].str.strip() == 'EQ'].copy()
df['SESSION_DATE'] = pd.to_datetime(df['SESSION_DATE'])
df = df.sort_values(['SYMBOL', 'SESSION_DATE'])

candidates = {
    'ASIANHOTNR': 'Asian Hotels (North)',
    'NORTHARC': 'Northern Arc Capital',
    'BALRAMCHIN': 'Balrampur Chini Mills',
    'BODALCHEM': 'Bodal Chemicals',
    'INDORAMA': 'Indo Rama Synthetics',
    'TNPETRO': 'Tamilnadu Petroproducts',
    'ASHOKA': 'Ashoka Buildcon',
    'DIFFNKG': 'Diffusion Engineers',
    'AYMSYNTEX': 'Aym Syntex',
    'MANALIPETC': 'Manali Petrochemicals',
    'VIMTALABS': 'Vimta Labs',
    'ULTRAMAR': 'Ultramarine & Pigments',
    'JUBLINGREA': 'Jubilant Agri',
    'RELIGARE': 'Religare Enterprises',
    'SHIPROCKET': 'Shiprocket',
    'QLINE': 'Q-Line Biotech',
    'MODISONLTD': 'Modison',
    'STOVEKRAFT': 'Stove Kraft',
    'TMB': 'Tamilnad Mercantile Bank',
}

# --- replicate score_stock() scoring logic (buy_pct/vwap/orb unavailable -> defaults) ---
MIN_SCORE = 65
MIN_CHANGE_PCT = 1.5
BUILDING_MIN_SCORE = 35
BUILDING_MIN_CHANGE_PCT = 0.3
RVOL_SLOPE_MIN = 0.6
DIST_52W_BREAKOUT_PCT = 8.0
DIST_52W_NEAR_PCT = 15.0

def score_row(row, avg_vol, atr, high_52w, prev_rvol):
    prev_close = row['PREV_CLOSE']
    current_price = row['CLOSE_PRICE']
    open_price = row['OPEN_PRICE']
    day_high = row['HIGH_PRICE']
    day_low = row['LOW_PRICE']
    vol = row['TTL_TRD_QNTY']

    price_change_pct = round(((current_price - prev_close) / prev_close) * 100, 2) if prev_close else 0
    rvol = round(vol / avg_vol, 2) if avg_vol else 0
    rvol_slope = round(rvol - prev_rvol, 2) if prev_rvol is not None else 0.0

    # no live order-book / vwap / orb data available from EOD bhav feed -> production defaults
    buy_pct = 50.0
    vwap = current_price       # tick.get("vwap") or current_price  -> defaults to current price
    orb_high = day_high        # tick.get("orb_high") or tick.get("high") or open_price -> day high used if available

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
    elif score >= BUILDING_MIN_SCORE and price_change_pct > BUILDING_MIN_CHANGE_PCT:
        tier = "building"
    else:
        tier = "none"

    return score, tier, trigger, price_change_pct, rvol, dist

results = []
for sym, name in candidates.items():
    g = df[df['SYMBOL'] == sym].copy()
    if g.empty:
        results.append((sym, name, None, None, None, None, None, None, "NOT FOUND IN DATA"))
        continue
    g = g.reset_index(drop=True)
    g['pct_chg'] = ((g['CLOSE_PRICE'] - g['PREV_CLOSE']) / g['PREV_CLOSE']) * 100
    # find the biggest single-day pct move in the last ~90 sessions (recent, comparable to a "shocker" day)
    recent = g.tail(90).copy()
    if recent.empty:
        continue
    idx = recent['pct_chg'].idxmax()
    row = g.loc[idx]
    i = g.index.get_loc(idx)
    if i < 20:
        continue
    window = g.iloc[max(0, i-20):i]
    avg_vol = window['TTL_TRD_QNTY'].mean()
    win14 = g.iloc[max(0, i-14):i]
    atr = (win14['HIGH_PRICE'] - win14['LOW_PRICE']).mean()
    win252 = g.iloc[max(0, i-252):i]
    high_52w = win252['HIGH_PRICE'].max() if not win252.empty else row['HIGH_PRICE']
    prev_rvol = None
    if i >= 21:
        prev_row = g.iloc[i-1]
        prev_avg_vol = g.iloc[max(0, i-21):i-1]['TTL_TRD_QNTY'].mean()
        prev_rvol = round(prev_row['TTL_TRD_QNTY'] / prev_avg_vol, 2) if prev_avg_vol else None

    score, tier, trigger, pct, rvol, dist = score_row(row, avg_vol, atr, high_52w, prev_rvol)
    results.append((sym, name, row['SESSION_DATE'].date(), pct, rvol, dist, score, tier, trigger))

print(f"{'SYMBOL':<12}{'NAME':<24}{'DATE':<12}{'%CHG':>7}{'RVOL':>7}{'DIST52W%':>10}{'SCORE':>7}  TIER      TRIGGER")
for r in results:
    if r[2] is None:
        print(f"{r[0]:<12}{r[1]:<24}{'':12}{'':7}{'':7}{'':10}{'':7}  {r[-1]}")
        continue
    sym,name,date,pct,rvol,dist,score,tier,trigger = r
    print(f"{sym:<12}{name:<24}{str(date):<12}{pct:>7.2f}{rvol:>7.2f}{dist:>10.2f}{score:>7}  {tier:<9} {trigger}")
