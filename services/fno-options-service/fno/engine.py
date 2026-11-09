"""fno/engine.py - one cycle of the F&O engine: manage open positions, then look for entries. The clock, the data provider and the broker are INJECTED, so a whole
trading day can be replayed in a test. Never raises out of `tick` (a broken cycle is logged and the next one starts clean).

Order inside a cycle:  1) exits for open positions (square-off time, stop, trail, target, opposite signal)   2) entries per underlying: chain -> summary -> direction ->
gates -> contract -> lots -> fill. DEMO fills are virtual; REAL goes through execution.py behind its four locks."""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from . import analytics, capital, execution, gates, paper, signals, sizing, strategies
from .models import Chain, ContractRef
from .params import Params
from .paper import Position
from .provider import IST, Provider
from .store import Store, day_of

log = logging.getLogger("fno.engine")
THROTTLE_S = 60.0


def now_ist() -> datetime:
    return datetime.now(IST)


class Engine:
    def __init__(self, store: Store, provider: Provider, cfg: Any, broker: Optional[execution.Broker] = None, holidays: Tuple[str, ...] = ()):
        self.store, self.provider, self.cfg, self.broker, self.holidays = store, provider, cfg, broker, holidays
        self._last_log: Dict[str, float] = {}
        self.sleep = time.sleep                      # injected in tests
        self.fill_tries, self.fill_wait_s = 5, 1.0   # how long a REAL order is waited for before it is cancelled
        self.last_reconcile: Dict[str, Any] = {"ts": None}
        self._reconcile_at = 0.0
        self.last_tick: Dict[str, Any] = {"ts": None, "ok": None, "error": None, "summary": {}}
        self.capital_why = ""

    # ── helpers ──
    def _note(self, kind: str, und: str, data: Dict[str, Any], key: str = "", force: bool = False, now: Optional[datetime] = None) -> None:
        k = f"{kind}:{und}:{key}"
        t = (now or now_ist()).timestamp()
        if not force and t - self._last_log.get(k, 0.0) < THROTTLE_S:
            return
        self._last_log[k] = t
        try:
            self.store.add_event(kind, data, underlying=und, ts=t)
        except Exception:  # noqa: BLE001
            log.exception("journal write failed")

    def params(self) -> Params:
        """The cycle's Params. In REAL mode the capital is the amount the three-way capital board allots to F&O (fno/capital.py), NOT FNO_DEMO_CAPITAL; no fresh board = 0 = no entry."""
        p = Params.from_config(self.cfg)
        self.capital_why = ""
        if str(getattr(self.cfg, "FNO_MODE", "DEMO")).upper() == "REAL":
            amt, why = capital.real_capital(self.cfg)
            self.capital_why = why
            p = p.with_(capital=float(amt))
        return p

    def underlyings(self) -> List[str]:
        u = str(getattr(self.cfg, "FNO_UNDERLYINGS", "BOTH")).upper()
        return ["NIFTY", "SENSEX"] if u == "BOTH" else [u] if u in ("NIFTY", "SENSEX") else []

    def pick_expiry(self, und: str, today: date, p: Params) -> Optional[str]:
        need = max(p.min_days_to_expiry, 1 if p.block_expiry_day else 0)
        for e in self.provider.expiries(und):
            try:
                if (date.fromisoformat(e) - today).days >= need:
                    return e
            except ValueError:
                continue
        return None

    def armed_day(self) -> Optional[str]:
        return self.store.kv_get("fno_armed_day")

    def day_state(self, now: datetime, mode: str) -> gates.DayState:
        s = self.store.day_summary(day_of(now.timestamp()), mode)           # round 249: a leftover from an earlier day that closed today counts against today's loss limit (carried_closed_net)
        return gates.DayState(trades_today=s["trades"], open_positions=s["open"], realized_pnl_net=s["realized_net"] + s.get("carried_closed_net", 0.0), last_stopout_ts=s["last_stopout_ts"],
                              open_same_side=s["open_same_side"], entries_by_strategy={k: v["entries"] for k, v in (s.get("by_strategy") or {}).items()})

    # ── REAL order helpers (only reached behind the four locks; the default Broker cannot confirm, so DEMO tests and old behaviour are unchanged) ──
    def _can_confirm(self) -> bool:
        return bool(self.broker and getattr(self.broker, "can_confirm", False))

    @staticmethod
    def _whole_lots(qty: Any, lot_size: int) -> int:
        try:
            return max(0, int(qty) // int(lot_size)) if int(lot_size) > 0 else 0
        except (TypeError, ValueError):
            return 0

    def _confirm(self, oid: str) -> Optional[execution.OrderStatus]:
        """Wait for a final state. None = no answer (a legacy broker that cannot tell, or, for a broker that can, a network miss that outlasted the retries).
        PENDING after the wait = still open."""
        st = None
        for i in range(max(1, self.fill_tries)):
            st = self.broker.order_status(oid) if self.broker else None
            if st is not None and st.state in ("TRADED", "REJECTED", "CANCELLED"):
                return st
            if st is None and not self._can_confirm():
                return None
            if i + 1 < self.fill_tries:
                self.sleep(self.fill_wait_s)
        return st

    def _settle(self, oid: str) -> Optional[execution.OrderStatus]:
        """Confirm; if still pending (or unanswered) cancel it and read the final state (the cancel can lose a race with a fill: the final read decides)."""
        st = self._confirm(oid)
        if (st is not None and st.state in ("PENDING", "UNKNOWN")) or (st is None and self._can_confirm()):
            self.broker.cancel(oid)
            st = self.broker.order_status(oid) or st
        return st

    def _unknown_origin(self, pos: Position) -> bool:
        return pos.broker_order_ids.get("entry_state") == "UNKNOWN"

    def _place_sl(self, pos: Position, now: datetime) -> None:
        if not self.broker or not pos.security_id:
            return
        if self._unknown_origin(pos):                       # a resting SELL stop on a position that may not exist would open a short
            return
        try:
            res = self.broker.place(execution.build_sl_order(self.cfg.DHAN_CLIENT_ID, pos.security_id, pos.segment, pos.qty, pos.sl_price))
        except ValueError as e:
            self._note("order_failed", pos.underlying, {"side": "SL", "position": pos.id, "message": str(e)}, key=f"sl{pos.id}", now=now, force=True)
            return
        if res.ok:
            pos.broker_order_ids["sl"] = res.order_id
            self.store.update_position(pos)
        else:
            # the software stop (checked every cycle) still protects the position; say so loudly
            self._note("order_failed", pos.underlying, {"side": "SL", "position": pos.id, "status": res.status, "message": res.message, "note": "no broker-side stop; software stop only"}, key=f"sl{pos.id}", now=now, force=True)

    def _book_partial(self, pos: Position, lots: int, price: float, reason: str, oid: str, p: Params, now: datetime, by: str) -> Position:
        """`lots` of an OPEN position were sold at the broker: store them as a closed child row and shrink the open position."""
        child = paper.split_closed(pos, lots, price, now.timestamp(), reason, p, oid)
        self.store.insert_position(child)
        pos.lots -= lots
        pos.note = (pos.note + f" | {by}: sold {lots} lot(s) at {price:g}, {pos.lots} still open").strip(" |")
        self.store.update_position(pos)
        self._note("exit", pos.underlying, {"position": pos.id, "label": pos.label(), "reason": reason, "partial": True, "by": by, "lots_sold": lots, "lots_left": pos.lots,
                                            "exit": price, "net": child.net_pnl}, force=True, now=now)
        return child

    def _sl_state(self, pos: Position, p: Params, now: datetime) -> Tuple[str, Optional[float]]:
        """Before an exit: cancel the resting stop and read what became of it. ("filled", price) = the stop had already sold everything; ("wait", None) = cannot tell, do not
        sell now; ("none", None) = no stop is working any more (the position, possibly with fewer lots after a partial stop fill, is still ours to sell)."""
        sl = pos.broker_order_ids.get("sl")
        if not sl or not self.broker:
            return "none", None
        st = self.broker.order_status(sl)
        if st is not None and st.state == "TRADED" and st.avg_price > 0:
            return "filled", st.avg_price
        self.broker.cancel(sl)
        st = self.broker.order_status(sl)
        if st is None and self._can_confirm():
            return "wait", None
        if st is not None and st.state == "TRADED" and st.avg_price > 0:
            return "filled", st.avg_price
        if st is not None and st.filled_qty > 0:            # the stop sold part of it before the cancel
            n = self._whole_lots(st.filled_qty, pos.lot_size)
            px = st.avg_price if st.avg_price > 0 else pos.sl_price
            if n >= pos.lots:
                return "filled", px
            if n >= 1:
                self._book_partial(pos, n, px, paper.SL, sl, p, now, "broker stop")
            else:
                self._note("reconcile", pos.underlying, {"msg": "stop filled an odd quantity: check the Dhan app", "position": pos.id, "filled_qty": st.filled_qty}, key=f"sl-odd{pos.id}", now=now, force=True)
            if st.state in ("PENDING", "UNKNOWN"):
                return "wait", None
        pos.broker_order_ids.pop("sl", None)
        return "none", None

    def check_broker_stops(self, now: datetime, p: Params) -> List[Position]:
        """REAL positions: a resting stop that traded closes the position at its fill; a stop that vanished is re-placed."""
        done: List[Position] = []
        if not self.broker:
            return done
        for pos in self.store.open_positions("REAL"):
            sl = pos.broker_order_ids.get("sl")
            if not sl:
                ok, _ = execution.real_allowed(self.cfg, self.armed_day(), day_of(now.timestamp()))
                if ok:
                    self._place_sl(pos, now)
                continue
            st = self.broker.order_status(sl)
            if st is None:
                continue
            if st.state == "TRADED" and st.avg_price > 0:
                pos.note = (pos.note + " | closed by the broker-side stop" + (" (fill price estimated from the stop's limit: Dhan gave no average)" if (st.raw or {}).get("_avg_estimated") else "")).strip(" |")
                paper.close_position(pos, st.avg_price, now.timestamp(), paper.SL, p)
                self.store.update_position(pos)
                self._note("exit", pos.underlying, {"position": pos.id, "label": pos.label(), "reason": paper.SL, "by": "broker stop", "entry": pos.entry_premium, "exit": st.avg_price,
                                                    "net": pos.net_pnl}, force=True, now=now)
                done.append(pos)
            elif st.state in ("REJECTED", "CANCELLED"):
                self._note("order_failed", pos.underlying, {"side": "SL", "position": pos.id, "status": st.state, "note": "stop order gone; re-placing"}, key=f"sl{pos.id}", now=now, force=True)
                pos.broker_order_ids.pop("sl", None)
                self.store.update_position(pos)
            elif st.state == "PENDING" and st.filled_qty > 0:
                self._note("reconcile", pos.underlying, {"msg": "the stop is part-filled and still working: check the Dhan app", "position": pos.id, "filled_qty": st.filled_qty,
                                                         "qty": pos.qty}, key=f"sl-part{pos.id}", now=now)
        return done

    def resolve_unknown_entries(self, now: datetime) -> None:
        """A REAL entry whose fill could not be read stays OPEN but flagged UNKNOWN: it gets no stop and is never sold. Each cycle we try to settle it from the order's own state
        (TRADED: confirmed at the real price, REJECTED / CANCELLED with nothing filled: it never existed) and, failing that, from Dhan's net position (>= our quantity: confirmed).
        A zero net position alone never closes it (the positions call is unverified): it stays flagged and loud for a human."""
        if not self.broker:
            return
        for pos in self.store.open_positions("REAL"):
            if not self._unknown_origin(pos):
                continue
            oid = pos.broker_order_ids.get("entry")
            st = self.broker.order_status(oid) if oid else None
            confirmed, price = False, None
            if st is not None and st.state == "TRADED" and st.avg_price > 0:
                confirmed, price = True, st.avg_price
            elif st is not None and st.state in ("REJECTED", "CANCELLED") and st.filled_qty == 0:
                pos.status, pos.exit_ts, pos.exit_reason = paper.CLOSED, now.timestamp(), "NOT_FILLED"
                pos.gross_pnl = pos.charges = pos.net_pnl = 0.0
                pos.note = (pos.note + " | the entry order turned out not to have filled: no position at Dhan").strip(" |")
                self.store.update_position(pos)
                self._note("reconcile", pos.underlying, {"msg": "unknown entry resolved: it never filled", "position": pos.id}, key=f"unk{pos.id}", now=now, force=True)
                continue
            else:
                net = self.broker.net_positions()
                if net is not None and net.get(str(pos.security_id), 0) >= pos.qty:
                    confirmed = True
            if not confirmed:
                self._note("reconcile", pos.underlying, {"msg": "entry fill still unknown: no stop, will not be sold by the engine. Check the Dhan app", "position": pos.id}, key=f"unk{pos.id}", now=now)
                continue
            if price and abs(price - pos.entry_premium) > 1e-9:         # rebase stop / target on the real price
                pos.entry_premium = price
                pp = Params.from_config(self.cfg)                      # the same percentages open_position used
                pos.sl_price = paper.round_tick(price * (1 - pp.sl_pct / 100.0), 0.05, up=True)
                pos.target_price = paper.round_tick(price * (1 + pp.target_pct / 100.0), 0.05, up=True)
                pos.max_premium = max(pos.max_premium or price, price)
                pos.min_premium = min(pos.min_premium or price, price)
            pos.broker_order_ids.pop("entry_state", None)
            pos.note = (pos.note + " | entry fill now confirmed").strip(" |")
            self.store.update_position(pos)
            self._note("reconcile", pos.underlying, {"msg": "unknown entry confirmed", "position": pos.id, "price": price}, key=f"unk{pos.id}", now=now, force=True)
            self._place_sl(pos, now)

    def reconcile(self, now: datetime, force: bool = False) -> Dict[str, Any]:
        """Compare OUR open REAL positions with Dhan's net positions (F&O). Read-only: a difference is logged, never traded on."""
        if not self.broker or (not force and now.timestamp() - self._reconcile_at < 60.0):
            return self.last_reconcile
        self._reconcile_at = now.timestamp()
        net = self.broker.net_positions()
        if net is None:
            self.last_reconcile = {"ts": now.timestamp(), "ok": None, "note": "broker positions unavailable"}
            return self.last_reconcile
        own: Dict[str, int] = {}
        for pos in self.store.open_positions("REAL"):
            own[str(pos.security_id)] = own.get(str(pos.security_id), 0) + pos.qty
        diffs = [{"security_id": k, "ours": own.get(k, 0), "broker": net.get(k, 0)} for k in sorted(set(own) | set(net)) if own.get(k, 0) != net.get(k, 0)]
        self.last_reconcile = {"ts": now.timestamp(), "ok": not diffs, "differences": diffs, "ours": own, "broker": net}
        for d in diffs:
            self._note("reconcile", "", {**d, "msg": "our REAL position and Dhan's net position differ: check the Dhan app"}, key=d["security_id"], now=now)
        return self.last_reconcile

    # ── exits ──
    def _exit_outcome(self, pos: Position, oid: str, st: Optional[execution.OrderStatus], fill: float, reason: str, p: Params, now: datetime) -> Tuple[str, Optional[float]]:
        """What a SELL order's final state means for the position.
        ("filled", price)  everything is sold (price = the real average when Dhan gave it)
        ("partial", None)  some lots sold: they are booked as a closed child row, the rest stays OPEN
        ("open", None)     nothing sold (rejected / cancelled): the position stays OPEN
        ("wait", None)     the state is in doubt: do NOT send another SELL (it could sell twice and leave a short); look again next cycle"""
        if st is None:
            return ("wait", None) if self._can_confirm() else ("filled", fill)
        if st.state == "TRADED" and st.avg_price > 0:
            return "filled", st.avg_price
        sold = self._whole_lots(st.filled_qty, pos.lot_size)
        px = st.avg_price if st.avg_price > 0 else fill
        if sold >= pos.lots:
            return "filled", px
        doubt = st.state in ("PENDING", "UNKNOWN")
        if sold >= 1:
            self._book_partial(pos, sold, px, reason, oid, p, now, "exit order")
            if doubt:
                return "wait", None
            return "partial", None
        if st.filled_qty > 0:
            self._note("reconcile", pos.underlying, {"msg": "exit filled an odd quantity: check the Dhan app", "position": pos.id, "filled_qty": st.filled_qty}, key=f"x-odd{pos.id}", now=now, force=True)
            return "wait", None
        return ("wait", None) if doubt else ("open", None)

    def _expired_value(self, pos: Position, now: datetime) -> Optional[float]:
        """Round 250 (DEMO only): the value of a contract whose expiry day is BEFORE today = its intrinsic value on the close of the expiry day's daily candle (the index settles on that day's close), at least one tick.
        None when the expiry has not passed or the provider has no candle for that day: the caller then keeps the entry premium (unchanged behaviour)."""
        try:
            if pos.expiry >= day_of(now.timestamp()):
                return None
            for cd in self.provider.daily(pos.underlying) or []:
                if day_of(float(cd["t"])) == pos.expiry and float(cd.get("c") or 0) > 0:
                    spot = float(cd["c"])
                    intrinsic = max(0.0, spot - pos.strike) if pos.opt_type == "CE" else max(0.0, pos.strike - spot)
                    return max(0.05, round(intrinsic / 0.05) * 0.05)
        except Exception as ex:                                       # a provider that fails here must not stop the square-off: fall back to the old price
            log.warning("expired value of position %s not available: %s", getattr(pos, "id", "?"), ex)
        return None

    def _exit(self, pos: Position, reason: str, side: Optional[Any], p: Params, now: datetime) -> Position:
        bid = getattr(side, "bid", None)
        ltp = getattr(side, "ltp", None)
        fill = paper.sell_fill(bid, ltp, p)
        if not fill:
            # No quote. DEMO books the entry premium as before. A REAL SELL at the ENTRY price would sit above a premium that has fallen and never fill, so at square-off the position
            # would stay open: use a price well under the lowest premium seen instead (a limit that crosses the book; Dhan fills at the market side).
            fill = max(0.05, round(pos.min_premium * 0.85 / 0.05) * 0.05) if (pos.mode == "REAL" and pos.min_premium) else None
            if fill is None:
                fill = self._expired_value(pos, now) if pos.mode != "REAL" else None   # round 250: a DEMO contract that has already expired is booked at its settlement value, not at its entry premium (that showed 0 P&L instead of the loss)
            if fill is None:
                fill = pos.entry_premium
        if pos.mode == "REAL":
            ok, closed = execution.real_allowed(self.cfg, self.armed_day(), day_of(now.timestamp()))
            if not ok:
                self._note("real_blocked", pos.underlying, {"would": "SELL", "position": pos.id, "reason": reason, "locks": closed}, key=f"x{pos.id}", now=now)
                return pos
            if not self.broker:
                self._note("order_failed", pos.underlying, {"side": "SELL", "position": pos.id, "message": "no broker"}, key=f"x{pos.id}", now=now, force=True)
                return pos
            if self._unknown_origin(pos):                   # never SELL what may not exist (that opens a short): only if Dhan's net position shows it
                net = self.broker.net_positions()
                if net is None or net.get(str(pos.security_id), 0) < pos.qty:
                    self._note("reconcile", pos.underlying, {"msg": "will not SELL a position whose entry fill is unknown and not visible at Dhan: check the Dhan app", "position": pos.id},
                               key=f"x-unk{pos.id}", now=now)
                    return pos
            old = pos.broker_order_ids.get("exit")          # an earlier SELL that was in doubt: find out what became of it before anything else
            if old:
                kind, px = self._exit_outcome(pos, old, self._settle(old), fill, reason, p, now)
                if kind == "wait":
                    self._note("reconcile", pos.underlying, {"msg": "an earlier exit order is still in doubt: no new SELL until it is known", "position": pos.id, "order": old}, key=f"x-old{pos.id}", now=now)
                    return pos
                pos.broker_order_ids.pop("exit", None)
                if kind == "filled":
                    fill = px if px else fill
                    return self._finish_exit(pos, fill, reason, p, now)
                if kind == "partial":
                    self.store.update_position(pos)
                    self._place_sl(pos, now)
                    return pos
            sl_kind, stopped = self._sl_state(pos, p, now)
            if sl_kind == "wait":
                self._note("reconcile", pos.underlying, {"msg": "the stop order state is unknown: no SELL this cycle", "position": pos.id}, key=f"x-sl{pos.id}", now=now)
                return pos
            if sl_kind == "filled":                         # the resting stop traded first: nothing to sell
                pos.note = (pos.note + " | closed by the broker-side stop").strip(" |")
                paper.close_position(pos, stopped, now.timestamp(), paper.SL, p)
                self.store.update_position(pos)
                self._note("exit", pos.underlying, {"position": pos.id, "label": pos.label(), "reason": paper.SL, "by": "broker stop", "exit": stopped, "net": pos.net_pnl}, force=True, now=now)
                return pos
            payload = execution.build_order(self.cfg.DHAN_CLIENT_ID, pos.security_id or 0, pos.segment, "SELL", pos.qty, fill)
            res = self.broker.place(payload)
            if not res.ok:
                self._note("order_failed", pos.underlying, {"side": "SELL", "position": pos.id, "status": res.status, "message": res.message}, key=f"x{pos.id}", now=now, force=True)
                self._place_sl(pos, now)                    # the stop was cancelled above: put protection back
                return pos
            pos.broker_order_ids["exit"] = res.order_id
            st = self._settle(res.order_id)
            kind, px = self._exit_outcome(pos, res.order_id, st, fill, reason, p, now)
            if kind == "wait":                              # in doubt: keep the order id so the next cycle resolves it; no stop, no second SELL
                pos.note = (pos.note + " | exit order state UNKNOWN: check the Dhan app").strip(" |")
                self.store.update_position(pos)
                self._note("reconcile", pos.underlying, {"msg": "exit order state unknown after cancel", "position": pos.id, "order": res.order_id}, key=f"x-wait{pos.id}", now=now, force=True)
                return pos
            if kind in ("open", "partial"):                 # not (fully) sold: stay OPEN, re-arm the stop, try again next cycle
                if kind == "open":
                    self._note("order_failed", pos.underlying, {"side": "SELL", "position": pos.id, "status": st.state if st else "", "message": "exit not filled in time, cancelled; retrying"},
                               key=f"x{pos.id}", now=now, force=True)
                pos.broker_order_ids.pop("exit", None)
                self.store.update_position(pos)
                self._place_sl(pos, now)
                return pos
            if st is None:
                pos.note = (pos.note + " | exit fill unconfirmed (limit price used)").strip(" |")
            fill = px if px else fill
        return self._finish_exit(pos, fill, reason, p, now)

    def _finish_exit(self, pos: Position, fill: float, reason: str, p: Params, now: datetime) -> Position:
        paper.close_position(pos, fill, now.timestamp(), reason, p)
        self.store.update_position(pos)
        self._note("exit", pos.underlying, {"position": pos.id, "label": pos.label(), "reason": reason, "strategy": pos.strategy, "entry": pos.entry_premium, "exit": fill, "net": pos.net_pnl,
                                            "gross": pos.gross_pnl, "charges": pos.charges}, force=True, now=now)
        return pos

    def manage_open(self, now: datetime, p: Params, chains: Dict[Tuple[str, str], Optional[Chain]], flips: Dict[str, str]) -> List[Position]:
        if self.broker:
            self.resolve_unknown_entries(now)
        closed: List[Position] = self.check_broker_stops(now, p)
        for pos in self.store.open_positions():
            sq = gates.after_squareoff(now, p, pos.expiry) or gates.carried_over(day_of(pos.entry_ts), now, self.holidays)   # a contract that expires today is closed earlier (expiry_squareoff_at); a leftover from an earlier day is closed at the open (round 248)
            key = (pos.underlying, pos.expiry)
            if key not in chains:
                chains[key] = self.provider.chain(*key)
            ch = chains[key]
            side = None
            if ch is not None:
                row = ch.row(pos.strike)
                side = row.side(pos.opt_type) if row else None
            prem = (side.mid if side and side.mid else None)
            if prem is None:
                if sq:                                      # no price at square-off: still close the book at the last known premium
                    self._exit(pos, paper.SQUAREOFF, None, p, now)
                    if pos.status == paper.CLOSED:          # a REAL exit that was blocked / is in doubt leaves it OPEN: do not report it as closed
                        closed.append(pos)
                else:
                    self._note("stale", pos.underlying, {"position": pos.id, "msg": "no premium for an open position"}, key=str(pos.id), now=now)
                continue
            hit = paper.evaluate_tick(pos, prem, p, squareoff=sq)
            if hit is None and flips.get(pos.key) == "flip":
                hit = (paper.SIGNAL_FLIP, prem)
            if hit:
                self._exit(pos, hit[0], side, p, now)
                if pos.status == paper.CLOSED:
                    closed.append(pos)
            else:
                self.store.update_position(pos)             # persists the new high / trail
        return closed

    # ── entries ──
    def _enter(self, und: str, direction: str, bias: Dict[str, Any], chain: Chain, p: Params, now: datetime, strategy: str = "CORE") -> Optional[Position]:
        p = strategies.params_for(strategy, p)                       # an expiry-day strategy is sized at half the risk and one lot
        if p.capital <= 0:                                           # REAL with no capital from the board: never size an order from a made-up figure
            self._note("skip", und, {"stage": "capital", "strategy": strategy, "reasons": [self.capital_why or "no capital to size with"]}, key=f"capital:{strategy}", now=now)
            return None
        lot_fb = int(getattr(self.cfg, "LOT_FALLBACK", {}).get(und, 0) or 0)
        pick = signals.pick_contract(chain, direction, now, p, lot_size=lot_fb or None, security_lookup=self.provider.lookup)
        if not pick["ok"]:
            self._note("skip", und, {"stage": "contract", "strategy": strategy, "reasons": pick["reasons"], "considered": pick["considered"]}, key=f"contract:{strategy}", now=now)
            return None
        c: ContractRef = pick["contract"]
        row = chain.row(c.strike)
        side = row.side(c.opt_type) if row else None
        fill = paper.buy_fill(getattr(side, "ask", None), getattr(side, "ltp", None), p, c.tick_size)
        if not fill:
            return None
        lots = sizing.lots_for(fill, c.lot_size, p)                  # round 250: sized on the FILL (ask + slippage), because the stop is measured from the fill; sizing on the ask let the risk at the stop run up to the slippage above the budget
        if not lots:
            self._note("skip", und, {"stage": "sizing", "strategy": strategy, "reasons": ["one lot exceeds the risk budget"], "premium": fill, "lot_size": c.lot_size,
                                          "min_capital_for_one_lot": sizing.min_capital_for_one_lot(fill, c.lot_size, p)}, key=f"sizing:{strategy}", now=now)
            return None
        mode = str(getattr(self.cfg, "FNO_MODE", "DEMO")).upper()
        mode = mode if mode in ("DEMO", "REAL") else "DEMO"
        if mode == "REAL":                                           # round 247: the premium of positions already open counts against the allotment of the capital board
            used = sum(float(q.entry_premium) * q.qty for q in self.store.open_positions("REAL"))
            fit = sizing.lots_in_room(lots, fill, c.lot_size, p.capital, used)
            if fit < 1:
                self._note("skip", und, {"stage": "capital_room", "strategy": strategy, "reasons": [f"the allotted capital Rs {p.capital:,.0f} has only Rs {max(0.0, p.capital - used):,.0f} free after the open positions; one lot costs Rs {fill * c.lot_size:,.0f}"]},
                           key=f"capital_room:{strategy}", now=now)
                return None
            lots = fit

        def build(n_lots: int, price: float) -> Position:
            q = paper.open_position(c, n_lots, price, now.timestamp(), p, mode=mode, score=bias["score"], spot=chain.spot, strategy=strategy)
            q.note = "; ".join(bias["reasons"][:6])
            return q

        pos = build(lots, fill)
        if pos.mode == "REAL":
            ok, closed = execution.real_allowed(self.cfg, self.armed_day(), day_of(now.timestamp()))
            if not ok:
                self._note("real_blocked", und, {"would": "BUY", "contract": c.label(), "strategy": strategy, "lots": lots, "price": fill, "locks": closed}, key="entry", now=now)
                return None
            if not c.security_id:
                self._note("order_failed", und, {"side": "BUY", "message": "no security id for the contract"}, key="entry", now=now, force=True)
                return None
            res = self.broker.place(execution.build_order(self.cfg.DHAN_CLIENT_ID, c.security_id, c.segment, "BUY", pos.qty, fill)) if self.broker else execution.OrderResult(False, message="no broker")
            if not res.ok:
                self._note("order_failed", und, {"side": "BUY", "contract": c.label(), "status": res.status, "message": res.message}, key="entry", now=now, force=True)
                return None
            ordered_qty = pos.qty
            st = self._settle(res.order_id)
            if st is None and not self._can_confirm():       # a legacy broker that cannot answer: recorded at the limit price, flagged
                pos.broker_order_ids["entry"] = res.order_id
                pos.note = (pos.note + " | entry fill unconfirmed (limit price used)").strip(" |")
            elif st is not None and st.state == "TRADED" and st.avg_price > 0:
                if abs(st.avg_price - fill) > 1e-9:         # the real fill sets the stop / target / P&L base
                    pos = build(lots, st.avg_price)
                    fill = st.avg_price
                pos.broker_order_ids["entry"] = res.order_id
            elif st is not None and st.filled_qty > 0:      # PARTIAL: some lots filled; the rest was cancelled (or may still be working)
                n = self._whole_lots(st.filled_qty, c.lot_size)
                self._note("reconcile", und, {"msg": "entry filled only in part", "order": res.order_id, "contract": c.label(), "filled_qty": st.filled_qty, "ordered_qty": ordered_qty,
                                              "state": st.state}, key="entry-partial", now=now, force=True)
                if n < 1:                                   # less than one lot cannot be held as a position: reconcile will show it
                    self._note("order_failed", und, {"side": "BUY", "contract": c.label(), "message": "entry filled less than one lot: no position kept, check the Dhan app"}, key="entry", now=now, force=True)
                    return None
                n = min(n, lots)
                fill = st.avg_price if st.avg_price > 0 else fill
                pos = build(n, fill)
                pos.broker_order_ids["entry"] = res.order_id
                pos.note = (pos.note + f" | PARTIAL fill: {st.filled_qty} of {ordered_qty}").strip(" |")
                if st.state in ("PENDING", "UNKNOWN"):      # the cancel did not land: more may still fill
                    pos.broker_order_ids["entry_state"] = "UNKNOWN"
                    pos.note += " | cancel unconfirmed: check the Dhan app"
                lots = n
            elif st is not None and st.state in ("REJECTED", "CANCELLED"):
                self._note("order_failed", und, {"side": "BUY", "contract": c.label(), "status": st.state, "message": "entry not filled (cancelled / rejected)"}, key="entry", now=now, force=True)
                return None
            else:                                           # PENDING / UNKNOWN / no answer after the cancel: we cannot tell. Kept as a position, but flagged: no stop, never sold blind
                pos.broker_order_ids["entry"] = res.order_id
                pos.broker_order_ids["entry_state"] = "UNKNOWN"
                pos.note = (pos.note + " | entry fill UNKNOWN after cancel: check the Dhan app").strip(" |")
                self._note("reconcile", und, {"msg": "entry order state unknown after cancel", "order": res.order_id, "contract": c.label()}, key="entry-unknown", now=now, force=True)
        self.store.insert_position(pos)
        if pos.mode == "REAL":
            self._place_sl(pos, now)
        self._note("entry", und, {"position": pos.id, "label": pos.label(), "mode": pos.mode, "strategy": strategy, "lots": lots, "fill": fill, "score": bias["score"], "reasons": bias["reasons"],
                                  "sl": pos.sl_price, "target": pos.target_price, "delta": pick["delta"]}, force=True, now=now)
        return pos

    # ── the cycle ──
    def tick(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        now = now or now_ist()
        out: Dict[str, Any] = {"ts": now.timestamp(), "enabled": bool(getattr(self.cfg, "FNO_ENGINE_ENABLED", False)), "entries": [], "exits": [], "decisions": {}}
        try:
            p = self.params()
            enabled = strategies.enabled_keys(self.cfg, expiry=False)
            enabled_exp = strategies.enabled_keys(self.cfg, expiry=True)
            today_iso = now.date().isoformat()
            mainch: Dict[str, Chain] = {}
            ecands: Dict[str, Tuple[Chain, List[Dict[str, Any]]]] = {}      # expiry-day strategies: the chain that expires TODAY and their signals
            chains: Dict[Tuple[str, str], Optional[Chain]] = {}
            summaries: Dict[str, Dict[str, Any]] = {}
            biases: Dict[str, Dict[str, Any]] = {}
            cands: Dict[str, List[Dict[str, Any]]] = {}
            flips: Dict[str, str] = {}
            vix: Optional[float] = None
            trading_day = gates.is_trading_day(now.date(), self.holidays)
            if out["enabled"] and trading_day:
                vix = self.provider.vix()
                for und in self.underlyings():
                    exp = self.pick_expiry(und, now.date(), p)
                    if not exp:
                        out["decisions"][und] = {"action": "wait", "why": ["no usable expiry"]}
                        continue
                    ch = self.provider.chain(und, exp)
                    chains[(und, exp)] = ch
                    if ch is None:
                        out["decisions"][und] = {"action": "wait", "why": ["no chain"]}
                        continue
                    mainch[und] = ch
                    summ = analytics.chain_summary(ch, p.strikes_each_side)
                    daily_u, intra_u = self.provider.daily(und), self.provider.intraday(und)
                    ctx = strategies.build_ctx(und, daily_u, intra_u, summ, now, p, self.holidays, vix)
                    bias = strategies.core_bias(ctx)                 # always computed: it also flips a held position, whichever strategy opened it
                    summaries[und], biases[und] = summ, bias
                    cands[und] = strategies.candidates(ctx, enabled, bias)
                    vetoes = list(ctx.vetoes)
                    if enabled_exp and today_iso in (self.provider.expiries(und) or []):
                        ch0 = self.provider.chain(und, today_iso)    # the contract that expires today
                        chains[(und, today_iso)] = ch0
                        if ch0 is not None:
                            ctx0 = strategies.build_ctx(und, daily_u, intra_u, analytics.chain_summary(ch0, p.strikes_each_side), now, p, self.holidays, vix, expiry_day=True)
                            ecands[und] = (ch0, strategies.candidates(ctx0, enabled_exp))
                            vetoes += ctx0.vetoes
                    for v in vetoes:                                 # a chain veto leaves no trade, so log it: the SIM has no chain, this is how live and SIM can be compared
                        self._note("veto", und, v, key=f"{v['strategy']}:{v['direction']}", now=now)
                    for ot, want in (("CE", signals.DOWN), ("PE", signals.UP)):          # a held CALL is flipped by a clear DOWN signal and vice versa
                        if bias["direction"] == want:
                            flips[f"{und}:{ot}"] = "flip"
            out["exits"] = [x.id for x in self.manage_open(now, p, chains, flips)] if (out["enabled"] or self.store.open_positions()) else []
            if self.broker and (self.store.open_positions("REAL") or str(getattr(self.cfg, "FNO_MODE", "DEMO")).upper() == "REAL"):
                self.reconcile(now)
            if out["enabled"] and trading_day:
                mode = str(getattr(self.cfg, "FNO_MODE", "DEMO")).upper()
                mode = mode if mode in ("DEMO", "REAL") else "DEMO"
                for und, bias in biases.items():
                    ch = mainch[und]
                    ex = ecands.get(und)
                    allc = [(c, ch, False) for c in cands.get(und, [])] + ([(c, ex[0], True) for c in ex[1]] if ex else [])
                    d = {"direction": bias["direction"], "score": bias["score"], "parts": bias["parts"], "vix": vix, "spot": ch.spot, "action": "none",
                         "strategies": [c["strategy"] for c, _, _ in allc]}
                    out["decisions"][und] = d
                    if not allc:
                        if bias["direction"] == signals.FLAT:
                            self._note("signal", und, {**d, "reasons": bias["reasons"]}, key="flat", now=now)
                        continue
                    for cand, chn, is_exp in allc:
                        sname = cand["strategy"]
                        key = f"{und}:{'CE' if cand['direction'] == signals.UP else 'PE'}"
                        ok, why = gates.entry_gates(now, date.fromisoformat(chn.expiry), vix, chn.age_s(now.timestamp()), self.day_state(now, mode), p, key=key,
                                                    holidays=self.holidays, strategy=sname, expiry_strategy=is_exp, mode=mode)
                        if not ok:
                            d.update(action="blocked", why=why)
                            self._note("skip", und, {**d, "stage": "gates", "strategy": sname}, key=f"gates:{sname}:" + "|".join(why)[:80], now=now)
                            continue
                        pos = self._enter(und, cand["direction"], cand, chn, p, now, strategy=sname)
                        if pos is not None:
                            d.update(action="entered", strategy=sname)
                            out["entries"].append(pos.id)
                            break                                    # one new position per underlying per cycle
            self.last_tick = {"ts": now.timestamp(), "ok": True, "error": None, "summary": out}
        except Exception as e:  # noqa: BLE001
            log.exception("engine tick failed")
            self.last_tick = {"ts": now.timestamp(), "ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}", "summary": out}
            out["error"] = self.last_tick["error"]
        return out

    def squareoff_all(self, reason: str = paper.MANUAL, now: Optional[datetime] = None) -> List[int]:
        now = now or now_ist()
        p = Params.from_config(self.cfg)
        done: List[int] = []
        for pos in self.store.open_positions():
            ch = self.provider.chain(pos.underlying, pos.expiry)
            row = ch.row(pos.strike) if ch else None
            self._exit(pos, reason, row.side(pos.opt_type) if row else None, p, now)
            if pos.status == paper.CLOSED:
                done.append(pos.id)
        return done
