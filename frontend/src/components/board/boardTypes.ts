// group306: shapes returned by position-stocks-service GET /board and GET /board/{symbol}.
export type Side = "bull" | "bear";

export interface BoardRow {
  symbol: string; name: string; sector: string; is_fno: boolean; side: Side;
  ltp: number; open: number | null; vwap: number | null; change_pct: number | null; strength: number;
  rank: number; rank_delta?: number | null; percentile: number; conviction: number; turnover: number;
  or_low?: number | null; or_high?: number | null; zone_status?: string | null; zone_valid?: boolean | null;
  chase_atr?: number | null; extended?: boolean; rr?: number | null; rule?: string;
  profile: number[]; spark: number[]; in_position: boolean;
}
export interface BoardSector { name: string; count: number; avg_change_pct: number; bull: number; bear: number }
export interface BoardPayload {
  as_of: number; universe: string; params_version: string; stale: boolean; stale_day?: string; market_open: boolean;
  feed: { source: string | null; connected: boolean; last_tick_at: number | null; tick_age_s: number | null };
  market: { up: number; down: number; flat: number; tracked: number; bull_count: number; bear_count: number;
            leading_sector: string | null; leading_change_pct: number | null; lagging_sector: string | null; lagging_change_pct: number | null };
  sectors: BoardSector[]; bullish: BoardRow[]; bearish: BoardRow[];
  coverage: { tracked: number; ranked: number; warming: number; no_day_open: number };
}
export interface Zone { side: Side; top: number; bottom: number; t: number; status: string; bos: boolean; fvg: boolean; valid: boolean }
export interface Level { price: number; label: string; source: string }
export interface SymbolView {
  symbol: string; sector: string; is_fno: boolean; ltp: number; side: Side; tf_min: number;
  candles: number[][]; vwap_series: (number | null)[][]; zones: Zone[]; zone: Zone | null;
  levels: { supports: Level[]; resistances: Level[] };
  rr: { rr: number; invalidation: number; target: number; target_label: string } | null;
  chase_atr: number | null; extended: boolean; or: number[] | null; vwap: number | null;
  position: { entry: number; stop: number; target: number; quantity: number } | null;
  strength: { score: number; move_pct: number } | null;
}
