"""backtest/metrics.py - turn a list of closed positions into the numbers that decide go / no-go. Pure."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List

from fno.paper import Position

IST = timezone(timedelta(hours=5, minutes=30))


def _day(p: Position) -> str:
    return datetime.fromtimestamp(p.exit_ts or p.entry_ts, IST).strftime("%Y-%m-%d")


def max_drawdown(pnls: List[float]) -> Dict[str, float]:
    peak = cum = dd = 0.0
    for x in pnls:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"max_drawdown": round(dd, 2), "final": round(cum, 2)}


def longest_losing_streak(pnls: List[float]) -> int:
    best = cur = 0
    for x in pnls:
        cur = cur + 1 if x <= 0 else 0
        best = max(best, cur)
    return best


def _fam_key(p: Position) -> Any:
    """Round 310: the TRADE a closed row belongs to: its parent for a partial-exit child row, else itself (a simulated trade has no id, so it is its own trade)."""
    fid = p.parent_id if p.parent_id is not None else p.id
    if fid is None:
        return ("row", id(p))
    # round 311: ids restart at 1 in every store (the replay backtest runs one in-memory store per day and pools the trades), so the id alone would merge trades of different days.
    # A partial-exit child copies its parent's entry time and contract, so these fields are the same for the whole family.
    return (fid, p.entry_ts, p.underlying, p.opt_type, p.strike, p.expiry)


def _trade_nets(ps: List[Position]) -> List[float]:
    """Net P&L per TRADE, in the order the trades first appear: a parent and its partial-exit rows are one trade, as in Store.day_summary and fno/capital.stats."""
    fam: Dict[Any, float] = {}
    for p in ps:
        fam[_fam_key(p)] = fam.get(_fam_key(p), 0.0) + (p.net_pnl or 0.0)
    return list(fam.values())


def _block(ps: List[Position]) -> Dict[str, Any]:
    net = _trade_nets(ps)                                  # round 310: per trade, so a partial exit is not a second trade / a second win
    gross = [p.gross_pnl or 0.0 for p in ps]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    n = len(net)
    gw, gl = sum(wins), -sum(losses)
    return {"trades": n, "wins": len(wins), "win_rate_pct": round(len(wins) / n * 100.0, 1) if n else None, "gross": round(sum(gross), 2),
            "charges": round(sum(p.charges or 0.0 for p in ps), 2), "net": round(sum(net), 2), "avg_win": round(gw / len(wins), 2) if wins else None,
            "avg_loss": round(-gl / len(losses), 2) if losses else None, "profit_factor": round(gw / gl, 2) if gl > 0 else None,
            "expectancy": round(sum(net) / n, 2) if n else None}


def summarize(positions: Iterable[Position], capital: float = 0.0, k_folds: int = 3) -> Dict[str, Any]:
    ps = sorted([p for p in positions if p.status == "CLOSED" and p.exit_reason != "NOT_FILLED"], key=lambda p: p.exit_ts or 0)   # round 310: an entry that never filled is not a trade (it counted as a loss)
    out: Dict[str, Any] = {"overall": _block(ps)}
    pnls = [p.net_pnl or 0.0 for p in ps]
    out["overall"].update(max_drawdown(pnls))
    out["overall"]["longest_losing_streak"] = longest_losing_streak(_trade_nets(ps))
    if capital > 0 and ps:
        out["overall"]["return_pct"] = round(out["overall"]["net"] / capital * 100.0, 2)
        out["overall"]["max_drawdown_pct"] = round(out["overall"]["max_drawdown"] / capital * 100.0, 2)
    for name, key in (("by_underlying", lambda p: p.underlying), ("by_side", lambda p: p.opt_type), ("by_exit", lambda p: p.exit_reason),
                      ("by_dte", lambda p: "expiry_day" if p.expiry == _day(p) else ("1_2_days" if (datetime.fromisoformat(p.expiry) - datetime.fromisoformat(_day(p))).days <= 2 else "3plus_days"))):
        g: Dict[str, List[Position]] = defaultdict(list)
        for p in ps:
            g[str(key(p))].append(p)
        out[name] = {k: _block(v) for k, v in sorted(g.items())}
    # direction right but the premium still lost (theta / spread / charges): the trades where gross > 0 but net <= 0, and stop-outs after the premium first rose
    out["right_but_lost"] = {"gross_positive_net_negative": sum(1 for p in ps if (p.gross_pnl or 0) > 0 >= (p.net_pnl or 0)),
                             "stopped_after_trading_up": sum(1 for p in ps if p.exit_reason == "SL" and p.max_premium >= p.entry_premium * 1.1)}
    # chronological folds: is the edge stable or one lucky stretch?
    folds: List[Dict[str, Any]] = []
    if len(ps) >= k_folds * 3:
        size = len(ps) // k_folds
        for i in range(k_folds):
            chunk = ps[i * size:(i + 1) * size if i < k_folds - 1 else len(ps)]
            b = _block(chunk)
            folds.append({"fold": i + 1, "from": _day(chunk[0]), "to": _day(chunk[-1]), "trades": b["trades"], "net": b["net"], "win_rate_pct": b["win_rate_pct"]})
    out["folds"] = folds
    out["stable"] = (all(f["net"] > 0 for f in folds) if folds else None)
    days: Dict[str, float] = defaultdict(float)
    for p in ps:
        days[_day(p)] += p.net_pnl or 0.0
    out["days"] = {"traded_days": len(days), "green_days": sum(1 for v in days.values() if v > 0), "best_day": round(max(days.values()), 2) if days else None,
                   "worst_day": round(min(days.values()), 2) if days else None}
    out["equity_curve"] = [{"day": d, "net": round(v, 2)} for d, v in sorted(days.items())]
    return out


def verdict(m: Dict[str, Any], min_trades: int = 30) -> Dict[str, Any]:
    """A cautious go / no-go read of the metrics. It can only say 'not proven' or 'no edge'; it never says 'safe'."""
    o = m.get("overall", {})
    n = o.get("trades", 0)
    why: List[str] = []
    if n < min_trades:
        why.append(f"only {n} trades (< {min_trades}): too few to tell skill from luck")
    if (o.get("net") or 0) <= 0:
        why.append("net result after charges is not positive")
    if (o.get("profit_factor") or 0) and o["profit_factor"] < 1.2:
        why.append(f"profit factor {o['profit_factor']} < 1.2")
    if m.get("stable") is False:
        why.append("the result is not positive in every chronological fold")
    return {"status": "NOT_PROVEN" if why else "PASSED_THIS_TEST_ONLY", "reasons": why or ["positive net, enough trades and stable folds on THIS data - still not a forecast"]}
