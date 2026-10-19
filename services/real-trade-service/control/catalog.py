"""control/catalog.py (real-trade-service) - the switches the Real Money Calibration page may change. Add one by adding a Flag here; then run
`python3 scripts/check_control_catalog.py` (CI runs it): it proves a `live=True` switch is read at call time everywhere, and that `shipped` matches config.py.

`recommended` is only set where the repo's own guidance names a value (docs/GROUP415, GROUP434, GROUP440, REMAINING_TASKS); a threshold the evidence has not settled
has no recommendation, so the page never nudges you towards a number nobody has proven."""
from __future__ import annotations

from control.kit import Flag

SERVICE = "real-trade-service"
_C = "config"
_CAND = "candidate_engine.candidates"

CATALOG = (
    # ---------------- structure gate at entry (G2 / G4 decide whether these stay) ----------------
    Flag("STRUCTURE_GATE_MODE", "Structure gate (REAL)", "Structure gate at entry", "enum", choices=("enforce", "off"),
         help="enforce = a bad daily structure makes the entry WAIT; off = kill switch. The gate only ever waits, it never creates an entry.",
         targets=((_C, "STRUCTURE_GATE_MODE"),), derived=((_C, "STRUCTURE_GATE_DEMO_MODE", "STRUCTURE_GATE_DEMO_MODE"),), import_time_ok_files=("config.py",),
         live=True, shipped="enforce", recommended="enforce", looser="neutral", evidence="G2 benefit / G4 live (structure_gate_check.py)"),
    Flag("STRUCTURE_GATE_DEMO_MODE", "Structure gate (DEMO)", "Structure gate at entry", "enum", choices=("enforce", "off"),
         help="Same switch for DEMO mode; when unset it follows the REAL value.", targets=((_C, "STRUCTURE_GATE_DEMO_MODE"),), live=True,
         shipped="enforce", looser="neutral"),
    Flag("STRUCTURE_REQUIRE_DATA", "Refuse when no structure data", "Structure gate at entry", "bool", looser="off",
         help="true = an entry with no usable daily structure is refused (STRUCTURE_NODATA); false = no data means no opinion and the entry goes ahead.",
         targets=((_C, "STRUCTURE_REQUIRE_DATA"),), live=True, shipped="true", recommended="true", evidence="group377 live-money default"),
    Flag("STRUCTURE_BEARISH_BLOCK", "Block bearish daily structure", "Structure gate at entry", "bool", looser="off",
         help="Bearish daily trend or a fresh bearish BOS blocks the entry.", targets=((_C, "STRUCTURE_BEARISH_BLOCK"),), live=True, shipped="true", recommended="true"),
    Flag("STRUCTURE_REJECT_STOP_WIDE", "Reject too-wide structural stop", "Structure gate at entry", "bool", looser="off",
         help="Refuse when the structural stop is wider than the allowed multiple of the ATR stop.", targets=((_C, "STRUCTURE_REJECT_STOP_WIDE"),), live=True, shipped="true",
         evidence="seed 13 summary.txt decides chase / stop_wide (REMAINING_TASKS)"),
    Flag("STRUCTURE_MAX_CHASE_ATR", "Max chase above zone (ATR)", "Structure gate at entry", "float", lo=0, hi=10, looser="higher", zero_off=True,
         help="Live price may be at most this many ATR above the nearest zone top. 0 switches the test off.", targets=((_C, "STRUCTURE_MAX_CHASE_ATR"),), live=True, shipped="1.5",
         evidence="calibration_pack.py walk-forward + seed 13"),
    Flag("STRUCTURE_MAX_STRETCH_ATR", "Max stretch above EMA20 (ATR)", "Structure gate at entry", "float", lo=0, hi=12, looser="higher", zero_off=True,
         help="Live price may be at most this many ATR above the daily EMA20. 0 = off.", targets=((_C, "STRUCTURE_MAX_STRETCH_ATR"),), live=True, shipped="3.0"),
    Flag("STRUCTURE_MIN_ROOM_ATR", "Min room to resistance (ATR)", "Structure gate at entry", "float", lo=0, hi=5, looser="lower",
         help="Room to the next daily resistance must be at least this many ATR. 0 = off.", targets=((_C, "STRUCTURE_MIN_ROOM_ATR"),), live=True, shipped="0.5"),
    Flag("STRUCTURE_MIN_RR", "Min structure reward:risk", "Structure gate at entry", "float", lo=0, hi=5, looser="lower",
         help="Structure R:R floor at the live entry. 0 = off (group424b: the replay edge of this rule flipped sign between samples).",
         targets=((_C, "STRUCTURE_MIN_RR"),), live=True, shipped="0", recommended="0", evidence="group424b"),
    Flag("STRUCTURE_MAX_AGE_DAYS", "Structure stale after (days)", "Structure gate at entry", "float", lo=1, hi=14, looser="higher",
         help="A structure whose last bar is older than this is stale = no opinion. 5 clears every 2026 trading day; 3 would stale every Monday.",
         targets=((_C, "STRUCTURE_MAX_AGE_DAYS"),), live=True, shipped="5", recommended="5", evidence="group415; re-check in December (2027 holidays)"),
    Flag("STRUCTURE_FETCH_ENABLED", "Fetch daily structure", "Structure gate at entry", "bool", looser="off",
         help="false = the entry path never fetches structure (every entry is 'no opinion').", targets=((_C, "STRUCTURE_FETCH_ENABLED"),), live=True, shipped="true", recommended="true"),
    # ---------------- stop / exit / evidence ----------------
    Flag("STRUCTURE_STOP_MODE", "Structural stop", "Structure stop, exit, evidence", "enum", choices=("on", "off"),
         help="on = use the structural stop when sane. Stays off until gate G3 (stability) has a result.", targets=((_C, "STRUCTURE_STOP_MODE"),), live=True,
         shipped="off", recommended="off", looser="neutral", evidence="G3 stability (structure_gate_check.py, >= 2 complete seeds)"),
    Flag("STRUCTURE_EXIT_MODE", "Structure exit", "Structure stop, exit, evidence", "enum", choices=("exit", "tighten", "log", "off"),
         help="log = record only (default since group415); tighten / exit act on a structure break. Stays log until G2 passes.", targets=((_C, "STRUCTURE_EXIT_MODE"),),
         live=True, shipped="log", recommended="log", evidence="G2 benefit"),
    Flag("STRUCTURE_TRAIL_ENABLED", "Structure trailing", "Structure stop, exit, evidence", "bool", looser="neutral",
         help="Trail the stop under confirmed swing lows.", targets=((_C, "STRUCTURE_TRAIL_ENABLED"),), live=True, shipped="true"),
    Flag("STRUCTURE_OBS_ENABLED", "Record structure observations", "Structure stop, exit, evidence", "bool", looser="neutral",
         help="Evidence only: stores what the gate saw and what the stock did next (feeds G4). Keep ON.", targets=((_C, "STRUCTURE_OBS_ENABLED"),), live=True,
         shipped="true", recommended="true", evidence="G4 live"),
    # ---------------- forecast gates (Kronos 4f, TimesFM 4g) ----------------
    Flag("KRONOS_GATE_MODE", "Kronos gate 4f (REAL)", "Forecast gates", "enum", choices=("enforce", "log", "off"), same_rank=("log", "off"),
         help="off = nothing is fetched; log = fetch, count and store the verdict, never block; enforce = a veto makes the entry WAIT. Unproven: run kronos_backtest.py first.",
         targets=((_C, "KRONOS_GATE_MODE"),), derived=((_C, "KRONOS_GATE_DEMO_MODE", "KRONOS_GATE_DEMO_MODE"),), import_time_ok_files=("config.py",),
         live=True, shipped="off", looser="neutral", evidence="kronos_backtest.py (group425)"),
    Flag("KRONOS_GATE_DEMO_MODE", "Kronos gate 4f (DEMO)", "Forecast gates", "enum", choices=("enforce", "log", "off"), same_rank=("log", "off"),
         help="Same for DEMO; follows the REAL value when unset.", targets=((_C, "KRONOS_GATE_DEMO_MODE"),), live=True, shipped="off", looser="neutral"),
    Flag("TIMESFM_GATE_MODE", "TimesFM gate 4g (REAL)", "Forecast gates", "enum", choices=("log", "off"), same_rank=("log", "off"),
         help="off (default) or log. There is NO enforce mode on purpose: Gate A (tsfm_probe.py on seeds 7/11/13 + --combine) then Gate B must pass first.",
         targets=((_C, "TIMESFM_GATE_MODE"),), derived=((_C, "TIMESFM_GATE_DEMO_MODE", "TIMESFM_GATE_DEMO_MODE"),), import_time_ok_files=("config.py",),
         live=True, shipped="off", looser="neutral", evidence="Gate A / Gate B (docs/GROUP434E)"),
    Flag("TIMESFM_GATE_DEMO_MODE", "TimesFM gate 4g (DEMO)", "Forecast gates", "enum", choices=("log", "off"), same_rank=("log", "off"),
         help="Same for DEMO; follows the REAL value when unset.", targets=((_C, "TIMESFM_GATE_DEMO_MODE"),), live=True, shipped="off", looser="neutral"),
    # ---------------- candidate rules ----------------
    Flag("CANDIDATE_52W_RULE_ENABLED", "52-week range rule", "Candidate rules", "bool", looser="off",
         help="false = the rule is hard off (nothing rejected AND nothing counted). Prefer LOG ONLY below, which keeps the evidence.",
         targets=((_CAND, "RANGE_52W_ENABLED"),), live=True, shipped="true", recommended="true", evidence="legacy_gate_backtest.py regime split (group439)"),
    Flag("CANDIDATE_52W_LOG_ONLY", "52-week rule LOG ONLY", "Candidate rules", "bool", looser="on",
         help="true = the rule is evaluated and counted (GET /range52w/status) but does not reject. LOOSENS an entry rule; the 18 Oct hist400 split read 'hurts' in weak AND strong markets.",
         targets=((_CAND, "RANGE_52W_LOG_ONLY"),), live=True, shipped="false", recommended="true", evidence="group440; legacy_gate_backtest.py"),
    # ---------------- entry guards ----------------
    Flag("ENTRY_VWAP_GATE_ENABLED", "VWAP entry gate", "Entry guards", "bool", looser="off", help="Refuse an entry priced below VWAP (minus tolerance).",
         targets=((_C, "ENTRY_VWAP_GATE_ENABLED"),), live=True, shipped="true", evidence="intraday_gate_backtest.py (needs recorded 1-minute bars)"),
    Flag("ENTRY_VWAP_TOLERANCE_PCT", "VWAP tolerance (%)", "Entry guards", "float", lo=0, hi=2, looser="higher", help="How far below VWAP an entry may be.",
         targets=((_C, "ENTRY_VWAP_TOLERANCE_PCT"),), live=True, shipped="0.15"),
    Flag("OPENING_GATE_ENABLED", "Opening quality gate", "Entry guards", "bool", looser="off", help="Gap, range-position and previous-day checks for entries in the opening window.",
         targets=((_C, "OPENING_GATE_ENABLED"),), live=True, shipped="true", evidence="intraday_gate_backtest.py, opening_replay.py"),
    Flag("OPENING_PROTECT_ENABLED", "Opening protection (size / pause)", "Entry guards", "bool", looser="off", help="Smaller size and a pause after stop-outs in the opening minutes.",
         targets=((_C, "OPENING_PROTECT_ENABLED"),), live=True, shipped="true", evidence="opening_replay.py"),
    Flag("OPENING_ENTRY_GUARD_ENABLED", "Opening entry guard", "Entry guards", "bool", looser="off", help="Holds candidates until OPENING_ENTRY_NOT_BEFORE_IST.",
         targets=((_C, "OPENING_ENTRY_GUARD_ENABLED"),), live=True, shipped="true", evidence="strategy_readout.py"),
    Flag("ENTRY_DEPTH_RECHECK", "Depth re-check before order", "Entry guards", "bool", looser="off", help="Re-reads market depth right before the order.",
         targets=((_C, "ENTRY_DEPTH_RECHECK"),), live=True, shipped="true"),
    Flag("ORDER_FLOW_RECORD", "Record order flow", "Evidence recording", "bool", looser="neutral",
         help="Stores best-5 imbalance and spread per entry (group427). Needs ~2 weeks, then records_report.py reads the buckets.", targets=((_C, "ORDER_FLOW_RECORD"),), live=True,
         shipped="true", recommended="true", evidence="records_report.py order-flow buckets"),
    Flag("ORDER_FLOW_DEPTH20", "Record 20-level depth", "Evidence recording", "bool", looser="neutral", help="Also records the 20-level imbalance.",
         targets=((_C, "ORDER_FLOW_DEPTH20"),), live=True, shipped="true"),
    # ---------------- money safety ----------------
    Flag("BROKER_STOP_ENABLED", "Resting broker-side stop", "Money safety", "bool", looser="off",
         help="Keeps a stop order at the broker for held CNC positions (survives a crash of this service). Turning it OFF removes that protection.",
         targets=((_C, "BROKER_STOP_ENABLED"),), live=True, shipped="true", recommended="true", evidence="group377"),
    Flag("OVERNIGHT_HOLD_ENABLED", "Overnight hold allowed", "Money safety", "bool", looser="neutral",
         help="Lets selected profitable positions carry overnight instead of squaring off at ~15:00. Decided by hold_bucket_check.py.", targets=((_C, "OVERNIGHT_HOLD_ENABLED"),),
         live=True, shipped="true", evidence="hold_bucket_check.py"),
    Flag("IMPORT_SKIP_PENDING_BUY_MIN", "Skip broker import while own BUY pending (min)", "Money safety", "float", lo=0, hi=240, looser="neutral",
         help="group438: a just-filled own BUY is not imported as a broker holding for this many minutes. 0 = off.", targets=((_C, "IMPORT_SKIP_PENDING_BUY_MIN"),), live=True, shipped="45"),
)
