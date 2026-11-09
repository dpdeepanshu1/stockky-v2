"""control/catalog.py (fno-options-service) - the switches the Real Money Calibration page may change for F&O options.

Same engine as real-trade-service and position-stocks-service (control/kit.py, byte-identical). Add a switch by adding a Flag here, then run
`python3 scripts/check_control_catalog.py`. Every value is a module attribute of `config`, read at call time by its consumers, so `live=True` switches apply at once.

Order of safety: the engine, REAL mode and REAL orders are three separate locks and ship OFF / DEMO. `looser` says which direction takes more risk:
the page asks for a confirmation when a change LOOSENS a rule.
"""
from __future__ import annotations

from control.kit import Flag

SERVICE = "fno-options-service"
_C = "config"

CATALOG = (
    # ---------------- locks ----------------
    Flag("FNO_ENGINE_ENABLED", "Run the F&O engine", "Locks", "bool", looser="on",
         help="ON = the engine loop polls the Nifty / Sensex chains, builds signals and (in DEMO) opens virtual trades. OFF = only the data views work. Ships OFF.",
         targets=((_C, "FNO_ENGINE_ENABLED"),), live=True, shipped="false", recommended="false", evidence="DEMO days of evidence (fno_eod_backtest + DEMO journal)"),
    Flag("FNO_MODE", "Mode (DEMO / REAL)", "Locks", "enum", choices=("demo", "real"), looser="neutral",
         help="DEMO = virtual fills at the live premium with slippage and charges. REAL = orders go to Dhan, but only when FNO_REAL_ENABLED is also ON and the session is armed.",
         targets=((_C, "FNO_MODE"),), live=True, shipped="demo", recommended="demo"),
    Flag("FNO_REAL_ENABLED", "Allow REAL orders", "Locks", "bool", looser="on",
         help="Second lock for REAL mode. OFF = a REAL-mode engine only logs what it would have sent. Needs the weeks of DEMO evidence first (see the plan, section 8).",
         targets=((_C, "FNO_REAL_ENABLED"),), live=True, shipped="false", recommended="false"),
    Flag("FNO_UNDERLYINGS", "Underlyings", "Locks", "enum", choices=("nifty", "sensex", "both"), looser="neutral",
         help="Which index options the engine trades. Weekly expiry: Nifty Tuesday (NSE), Sensex Thursday (BSE).",
         targets=((_C, "FNO_UNDERLYINGS"),), live=True, shipped="both"),

    # ---------------- signal ----------------
    Flag("FNO_SIGNAL_MIN_SCORE", "Min signal score", "Signal", "float", lo=10, hi=95, looser="lower",
         help="The combined score (index structure + trend + VWAP + chain confirmation) must reach this, up for a CE and down for a PE. Higher = fewer, cleaner trades.",
         targets=((_C, "FNO_SIGNAL_MIN_SCORE"),), live=True, shipped="45"),
    Flag("FNO_VIX_MAX", "Max India VIX", "Signal", "float", lo=8, hi=60, looser="higher",
         help="No new entry while India VIX is above this: premiums are expensive and a vol crush can lose money even on a right call.",
         targets=((_C, "FNO_VIX_MAX"),), live=True, shipped="25"),
    Flag("FNO_MAX_SPREAD_PCT", "Max bid-ask spread %", "Signal", "float", lo=0.2, hi=15, looser="higher",
         help="The chosen contract's spread (ask - bid) / mid must be at or below this.",
         targets=((_C, "FNO_MAX_SPREAD_PCT"),), live=True, shipped="3"),
    Flag("FNO_MIN_PREMIUM", "Min premium (Rs)", "Signal", "float", lo=1, hi=500, looser="lower",
         help="Contracts cheaper than this are skipped (far out-of-the-money lottery tickets).",
         targets=((_C, "FNO_MIN_PREMIUM"),), live=True, shipped="20"),
    Flag("FNO_MAX_DATA_AGE_S", "Max chain age (s)", "Signal", "int", lo=5, hi=300, looser="higher",
         help="A chain older than this is not traded on.", targets=((_C, "FNO_MAX_DATA_AGE_S"),), live=True, shipped="25"),

    # ---------------- strategies (round 232) ----------------
    Flag("FNO_STRAT_CORE", "Strategy: Core signal", "Strategies", "bool", looser="on",
         help="The original signal: daily structure + trend + intraday VWAP / opening range + option chain, summed. ON by default; OFF = this signal opens nothing (it is still computed, because it flips a held position).",
         targets=((_C, "FNO_STRAT_CORE"),), live=True, shipped="true", recommended="true"),
    Flag("FNO_STRAT_TREND_RIDER", "Strategy: Trend Rider", "Strategies", "bool", looser="on",
         help="Daily trend AND intraday must each lean the same way (no summing). Idea from the name of an ad's algo; the rule is ours and unproven.",
         targets=((_C, "FNO_STRAT_TREND_RIDER"),), live=True, shipped="false", recommended="false", evidence="SIM per-strategy backtest (--strategy ALL) + DEMO days"),
    Flag("FNO_STRAT_MOMENTUM_SURGE", "Strategy: Momentum Surge", "Strategies", "bool", looser="on",
         help="An expansion bar that breaks the opening range and the session high / low. Idea from an ad's algo name; the rule is ours and unproven.",
         targets=((_C, "FNO_STRAT_MOMENTUM_SURGE"),), live=True, shipped="false", recommended="false", evidence="SIM per-strategy backtest (--strategy ALL) + DEMO days"),
    Flag("FNO_STRAT_BEAR_SWEEP", "Strategy: Bear Sweep (PUT only)", "Strategies", "bool", looser="on",
         help="Stops swept above the prior-day / opening-range high, then a close below the sweep bar's low: buys a PUT. Rule is ours, unproven.",
         targets=((_C, "FNO_STRAT_BEAR_SWEEP"),), live=True, shipped="false", recommended="false", evidence="SIM per-strategy backtest (--strategy ALL) + DEMO days"),
    Flag("FNO_STRAT_BEAR_PIVOT", "Strategy: Bear Pivot (PUT only)", "Strategies", "bool", looser="on",
         help="First close below the prior-day floor pivot, under VWAP, daily not bullish: buys a PUT. Rule is ours, unproven.",
         targets=((_C, "FNO_STRAT_BEAR_PIVOT"),), live=True, shipped="false", recommended="false", evidence="SIM per-strategy backtest (--strategy ALL) + DEMO days"),
    Flag("FNO_STRAT_DRIFT_EDGE", "Strategy: Drift Edge", "Strategies", "bool", looser="on",
         help="Steady drift day: a pullback to a sloping VWAP that resumes. Rule is ours, unproven.",
         targets=((_C, "FNO_STRAT_DRIFT_EDGE"),), live=True, shipped="false", recommended="false", evidence="SIM per-strategy backtest (--strategy ALL) + DEMO days"),
    Flag("FNO_STRAT_WEEKLY_OPENER", "Strategy: Weekly Opener", "Strategies", "bool", looser="on",
         help="First trading day of the week only: first close beyond the 30-minute range, with the daily trend. Rule is ours, unproven.",
         targets=((_C, "FNO_STRAT_WEEKLY_OPENER"),), live=True, shipped="false", recommended="false", evidence="SIM per-strategy backtest (--strategy ALL) + DEMO days"),
    Flag("FNO_STRAT_EXPIRY_MOMENTUM", "Strategy: Expiry Momentum (expiry day, DEMO)", "Strategies", "bool", looser="on",
         help="Trades ONLY on the day the weekly contract expires, on that same-day contract: first close beyond the 30-minute range on an expansion bar. Half the normal risk, one lot, entry by 13:30, out by 14:45. DEMO only (REAL needs the env switch FNO_EXPIRY_REAL, which is not on this page). Rule is ours, unproven.",
         targets=((_C, "FNO_STRAT_EXPIRY_MOMENTUM"),), live=True, shipped="false", recommended="false", evidence="30+ DEMO expiry days (about 7 months of Nifty Tuesdays) + SIM per-strategy backtest"),
    Flag("FNO_STRAT_EXPIRY_ALPHA", "Strategy: Expiry Alpha (expiry day, DEMO)", "Strategies", "bool", looser="on",
         help="Trades ONLY on expiry day: after a big move and a tight 30-minute pause, a close beyond the session high / low. Half risk, one lot, entry by 13:30, out by 14:45. DEMO only. Rule is ours, unproven.",
         targets=((_C, "FNO_STRAT_EXPIRY_ALPHA"),), live=True, shipped="false", recommended="false", evidence="30+ DEMO expiry days (Sensex Thursdays) + SIM per-strategy backtest"),
    Flag("FNO_STRATEGY_MAX_PER_DAY", "Max entries per day, per named strategy", "Strategies", "int", lo=1, hi=5, looser="higher",
         help="Each named strategy (not CORE) may open at most this many positions a day. The shared limits (max trades per day, one position per side, no opposite side held) still apply to all of them together.",
         targets=((_C, "FNO_STRATEGY_MAX_PER_DAY"),), live=True, shipped="1"),

    # ---------------- gates (when) ----------------
    Flag("FNO_ENTRY_START", "Entry window opens (IST)", "Gates", "time", looser="earlier",
         help="No entry before this time (the first minutes after the open are noise).", targets=((_C, "FNO_ENTRY_START"),), live=True, shipped="09:30"),
    Flag("FNO_ENTRY_END", "Entry window closes (IST)", "Gates", "time", looser="later",
         help="No new entry after this time.", targets=((_C, "FNO_ENTRY_END"),), live=True, shipped="14:30"),
    Flag("FNO_SQUAREOFF_AT", "Square off all at (IST)", "Gates", "time", looser="neutral",
         help="Every open F&O position is closed at this time (product INTRADAY: nothing is carried overnight).", targets=((_C, "FNO_SQUAREOFF_AT"),), live=True, shipped="15:15"),
    Flag("FNO_BLOCK_EXPIRY_DAY", "Block entries on expiry day", "Gates", "bool", looser="off",
         help="ON = no new entry in a contract on the day it expires (theta and the expiry-day margin rules are at their worst).",
         targets=((_C, "FNO_BLOCK_EXPIRY_DAY"),), live=True, shipped="true", recommended="true"),
    Flag("FNO_MIN_DAYS_TO_EXPIRY", "Min days to expiry", "Gates", "int", lo=0, hi=30, looser="lower",
         help="The picked expiry must be at least this many calendar days away.", targets=((_C, "FNO_MIN_DAYS_TO_EXPIRY"),), live=True, shipped="1"),

    # ---------------- risk ----------------
    Flag("FNO_RISK_PER_TRADE_PCT", "Risk per trade (% of capital)", "Risk", "float", lo=0.1, hi=3, looser="higher",
         help="Lots are sized so that hitting the stop loses about this % of capital (premium x lot size x lots x stop %).",
         targets=((_C, "FNO_RISK_PER_TRADE_PCT"),), live=True, shipped="1"),
    Flag("FNO_MAX_LOTS", "Max lots per trade", "Risk", "int", lo=1, hi=50, looser="higher",
         help="Hard cap on lots, whatever the sizing says.", targets=((_C, "FNO_MAX_LOTS"),), live=True, shipped="2"),
    Flag("FNO_MAX_OPEN_POSITIONS", "Max open positions", "Risk", "int", lo=1, hi=10, looser="higher",
         help="At most this many F&O positions open at once.", targets=((_C, "FNO_MAX_OPEN_POSITIONS"),), live=True, shipped="2"),
    Flag("FNO_MAX_TRADES_PER_DAY", "Max trades per day", "Risk", "int", lo=1, hi=30, looser="higher",
         help="Entries per day, both indices together.", targets=((_C, "FNO_MAX_TRADES_PER_DAY"),), live=True, shipped="3"),
    Flag("FNO_MAX_DAILY_LOSS_PCT", "Max daily loss (% of capital)", "Risk", "float", lo=0.5, hi=20, looser="higher",
         help="Once the day's net loss reaches this, no new entry until tomorrow.", targets=((_C, "FNO_MAX_DAILY_LOSS_PCT"),), live=True, shipped="3"),

    # ---------------- exits ----------------
    Flag("FNO_SL_PCT", "Stop loss (% of premium)", "Exits", "float", lo=5, hi=60, looser="higher",
         help="The stop sits this % below the entry premium.", targets=((_C, "FNO_SL_PCT"),), live=True, shipped="25"),
    Flag("FNO_TARGET_PCT", "Target (% of premium)", "Exits", "float", lo=10, hi=300, looser="neutral",
         help="Take profit at this % above the entry premium (the trailing stop may exit earlier).", targets=((_C, "FNO_TARGET_PCT"),), live=True, shipped="50"),
    Flag("FNO_TRAIL_ARM_PCT", "Trailing stop arms at (% profit)", "Exits", "float", lo=5, hi=200, looser="higher",
         help="Once the premium is this % above entry, the trailing stop switches on.", targets=((_C, "FNO_TRAIL_ARM_PCT"),), live=True, shipped="30"),
    Flag("FNO_TRAIL_PCT", "Trailing distance (% below best)", "Exits", "float", lo=5, hi=60, looser="higher",
         help="While armed, exit when the premium falls this % below the best premium seen.", targets=((_C, "FNO_TRAIL_PCT"),), live=True, shipped="20"),

    # ---------------- housekeeping ----------------
    Flag("FNO_RECORD_SNAPSHOTS", "Record chain snapshots", "Data", "bool", looser="neutral",
         help="ON = a chain snapshot is saved every minute while the market is open: this builds the data for the recorded-chain replay backtest (the most trustworthy one).",
         targets=((_C, "FNO_RECORD_SNAPSHOTS"),), live=True, shipped="true", recommended="true"),
    Flag("FNO_SLIPPAGE_PCT", "DEMO slippage %", "Data", "float", lo=0, hi=5, looser="lower",
         help="DEMO fills are this % worse than the live price (buy higher, sell lower), so the paper result is not flattered.",
         targets=((_C, "FNO_SLIPPAGE_PCT"),), live=True, shipped="0.5"),
    Flag("FNO_DEMO_CAPITAL", "DEMO capital (Rs)", "Data", "float", lo=10000, hi=100000000, looser="neutral",
         help="Virtual capital for DEMO sizing.", targets=((_C, "FNO_DEMO_CAPITAL"),), live=True, shipped="500000"),
    # ---------------- three-way capital split (round 244) ----------------
    Flag("CAPITAL_ALLOC_MODE", "Capital split between the 3 services", "Capital split", "enum", choices=("off", "shadow", "live"), looser="neutral",
         help="How the ONE Dhan account is split between Real Trade, Position Stocks and F&O (25% each fixed, the last 25% to the best net return). off = nothing written or read | shadow = the board is written and shown (/capital/board), no sizing changes | live = Real Trade and Position Stocks use their board % instead of the fixed 50% and F&O's open premium counts as peer exposure. Set it the same on all three services. Real Trade decides the board; F&O REAL sizing needs shadow or live. Needs about a week of shadow first.",
         targets=((_C, "CAPITAL_ALLOC_MODE"),), live=True, shipped="shadow", recommended="shadow", evidence="GET /capital/board in shadow for about a week", import_time_ok_files=("config.py",)),
)
