"""fno/engine.py - one cycle of the F&O engine: manage open positions, then look for entries. The clock, the data provider and the broker are INJECTED, so a whole
trading day can be replayed in a test. Never raises out of `tick` (a broken cycle is logged and the next one starts clean).

Order inside a cycle:  1) exits for open positions (square-off time, stop, trail, target, opposite signal)   2) entries per underlying: chain -> summary -> direction ->
gates -> contract -> lots -> fill. DEMO fills are virtual; REAL goes through execution.py behind its four locks."""
from __future__ import annotations

import functools
import logging
import threading
import time
import uuid
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
EXIT_CHAIN_FALLBACK_S = 30.0   # round 295: when a fresh chain cannot be had, a position is still managed on the last chain seen, if it is at most this old
STOP_ABSENT_AFTER_S = 120.0     # round 292: a stop whose POST failed is read as "never reached Dhan" only when Dhan answers 404 to the lookup AND this long has passed (a delayed order may still land)


def now_ist() -> datetime:
    return datetime.now(IST)


def _locked(fn):
    """Round 283: tick() runs on the runner thread, squareoff_all() and clear_exit_doubt() on a request thread. Without one lock both could read the same OPEN position and each send a SELL:
    if both filled, the second opened a short. Re-entrant, so a locked method may call another."""
    @functools.wraps(fn)
    def wrapper(self, *a, **k):
        with self._lock:
            return fn(self, *a, **k)
    return wrapper


class Engine:
    def __init__(self, store: Store, provider: Provider, cfg: Any, broker: Optional[execution.Broker] = None, holidays: Tuple[str, ...] = ()):
        self.store, self.provider, self.cfg, self.broker, self.holidays = store, provider, cfg, broker, holidays
        self._last_log: Dict[str, float] = {}
        self.sleep = time.sleep                      # injected in tests
        self.fill_tries, self.fill_wait_s = 5, 1.0   # how long a REAL order is waited for before it is cancelled
        self.last_reconcile: Dict[str, Any] = {"ts": None}
        self._reconcile_at = 0.0
        self._unmanaged_at = 0.0
        self.last_tick: Dict[str, Any] = {"ts": None, "ok": None, "error": None, "summary": {}}
        self.capital_why = ""
        self._chain_seen: Dict[Tuple[str, str], Chain] = {}   # round 295: the last chain fetched per (underlying, expiry)
        self._lock = threading.RLock()               # round 283: one engine action at a time (runner cycle, manual square-off, clearing an exit-doubt mark)

    # ── helpers ──
    # ── chains (round 295) ──
    def _get_chain(self, chains: Dict[Tuple[str, str], Optional[Chain]], key: Tuple[str, str], swallow: bool = True) -> Optional[Chain]:
        """One fetch per (underlying, expiry) per cycle. Dhan allows one option-chain call per ~3 s and market-data spaces them (DHAN_OPTIONCHAIN_MIN_INTERVAL_S), so every repeat fetch inside a cycle
        queues behind real ones: 2 to 4 distinct chains already take 6 to 13 s. For OPEN positions a fetch that raises is a missing chain (`swallow`), never an aborted cycle;
        the entry analysis passes swallow=False so a provider failure still shows as a failed cycle (`last_tick.ok`)."""
        if key in chains:
            return chains[key]
        try:
            ch = self.provider.chain(*key)
        except Exception as e:  # noqa: BLE001
            if not swallow:
                raise
            log.warning("chain %s %s failed: %s", key[0], key[1], e)
            ch = None
        chains[key] = ch
        if ch is not None:
            self._chain_seen[key] = ch
        return ch

    def recent_chain(self, underlying: str, expiry: str, now_ts: Optional[float] = None, max_age_s: float = EXIT_CHAIN_FALLBACK_S) -> Optional[Chain]:
        """The last chain this engine fetched for the key, if it is not older than `max_age_s`. Readers that only DISPLAY (the Overview page) use it instead of queueing another Dhan call."""
        ch = self._chain_seen.get((underlying, expiry))
        return ch if ch is not None and ch.age_s(now_ts) <= max_age_s else None

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

    def unmanaged_real(self) -> List[Position]:
        """Round 296: REAL positions we think are open while no broker is loaded (after a restart the borrowed Dhan token is gone until someone ARMs). Nothing manages them: no stop checks, no square-off, no fill news."""
        try:
            return [] if self.broker is not None else self.store.open_positions("REAL")
        except Exception:  # noqa: BLE001
            return []

    def warn_unmanaged_real(self, now: Optional[datetime] = None, force: bool = False, every_s: float = 900.0) -> List[Position]:
        held = self.unmanaged_real()
        t = (now or now_ist()).timestamp()
        if not held or (not force and t - self._unmanaged_at < every_s):
            return held
        self._unmanaged_at = t
        labels = [p.label() for p in held]
        msg = (f"{len(held)} REAL position(s) are open and NO Dhan connection is loaded, so the engine is not managing them: {', '.join(labels)}. Press ARM (it re-borrows the token) or set FNO_DHAN_* in the environment. "
               "A stop already resting at Dhan still protects you; our books will not see it fill.")
        log.error("UNMANAGED REAL POSITIONS: %s", msg)
        self._note("unmanaged_real", "", {"msg": msg, "positions": [p.id for p in held], "labels": labels}, key="all", now=now, force=True)
        return held

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
                              open_same_side=s["open_same_side"], entries_by_strategy={k: v["entries"] for k, v in (s.get("by_strategy") or {}).items()}, last_exit_by_key=dict(s.get("last_exit_by_key") or {}))

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

    def _maybe_sent(self, payload: Dict[str, Any], res: execution.OrderResult, und: str, side: str, now: datetime, position: Optional[int] = None) -> None:
        """Round 277: a NETWORK failure on POST /orders (timeout, cut response) does not mean the order was not placed: Dhan may have accepted it. Say so loudly, with the correlationId to look for
        in the Dhan app, and compare our positions with Dhan's now instead of waiting up to a minute. Read-only: nothing is sent or retried."""
        if not execution.maybe_sent(res):               # round 282: a network failure, an HTTP 5xx / 408 or a 2xx without an order id (execution.maybe_sent)
            return
        self._note("reconcile", und, {"msg": f"the {side} order may have reached Dhan although the call failed ({res.status}: {res.message}): check the Dhan app for correlationId {payload.get('correlationId')}",
                                      "side": side, "correlation_id": payload.get("correlationId"), "position": position}, key=f"sent-{side}-{position}", now=now, force=True)
        try:
            self.reconcile(now, force=True)
        except Exception:  # noqa: BLE001
            log.exception("reconcile after an uncertain order failed")

    def _unknown_origin(self, pos: Position) -> bool:
        return pos.broker_order_ids.get("entry_state") == "UNKNOWN"

    def _place_sl(self, pos: Position, now: datetime) -> None:
        if not self.broker or not pos.security_id:
            return
        if self._unknown_origin(pos):                       # a resting SELL stop on a position that may not exist would open a short
            return
        if pos.broker_order_ids.get("exit_doubt"):          # round 282: a SELL may be live at Dhan: a stop on top of it could sell a second time
            return
        if pos.broker_order_ids.get("sl_doubt"):            # round 292: an earlier stop call failed and may be live at Dhan: a second stop could sell twice and leave a short
            if not self._resolve_sl_doubt(pos, now) or pos.broker_order_ids.get("sl"):
                return
        cid = "sl" + uuid.uuid4().hex[:18]                  # our own tag, so a failed call can be looked up at Dhan afterwards
        try:
            res = self.broker.place(execution.build_sl_order(self.cfg.DHAN_CLIENT_ID, pos.security_id, pos.segment, pos.qty, pos.sl_price, correlation_id=cid))
        except ValueError as e:
            self._note("order_failed", pos.underlying, {"side": "SL", "position": pos.id, "message": str(e)}, key=f"sl{pos.id}", now=now, force=True)
            return
        if res.ok:
            pos.broker_order_ids["sl"] = res.order_id
            pos.broker_order_ids.pop("sl_booked_qty", None)
            self.store.update_position(pos)
        else:
            # the software stop (checked every cycle) still protects the position; say so loudly
            doubt = execution.maybe_sent(res)
            if doubt:                                       # round 292: it may be live at Dhan: no second stop until the lookup by correlationId says what became of it
                pos.broker_order_ids.update(sl_doubt=cid, sl_doubt_ts=repr(now.timestamp()))
                self.store.update_position(pos)
            self._note("order_failed", pos.underlying, {"side": "SL", "position": pos.id, "status": res.status, "message": res.message,
                                                        "note": "no broker-side stop; software stop only" + ("; the stop MAY be live at Dhan (correlationId " + cid + "): no second stop until Dhan's order lookup answers" if doubt else "")},
                       key=f"sl{pos.id}", now=now, force=True)

    def _clear_stop_doubt(self, pos: Position) -> None:
        for k in ("sl_doubt", "sl_doubt_ts"):
            pos.broker_order_ids.pop(k, None)

    def _resolve_sl_doubt(self, pos: Position, now: datetime) -> bool:
        """Round 292: a stop whose POST failed with a network error / 5xx may be resting at Dhan. Look it up by the correlationId we sent. True = settled (the stop was found and is now ours as `sl`, or it is
        certainly not there, so a new one may be placed); False = still unknown: place nothing. Reads only; the only way to \"not there\" is Dhan's 404 after STOP_ABSENT_AFTER_S, or a REJECTED / CANCELLED
        order with nothing filled. A network error or a 5xx on the lookup is never read as \"not there\"."""
        ids = pos.broker_order_ids
        cid = str(ids.get("sl_doubt") or "")
        if not cid:
            return True
        st = self.broker.order_by_correlation(cid) if self.broker else None
        if st is None or st.state == "UNKNOWN":
            self._note("reconcile", pos.underlying, {"msg": "an earlier stop order call may have reached Dhan and its state cannot be read yet: no second stop", "position": pos.id, "correlation_id": cid}, key=f"sl-doubt{pos.id}", now=now)
            return False
        if st.state == "NOT_FOUND":
            try:
                age = now.timestamp() - float(ids.get("sl_doubt_ts") or 0)
            except (TypeError, ValueError):
                age = STOP_ABSENT_AFTER_S
            if age < STOP_ABSENT_AFTER_S:
                self._note("reconcile", pos.underlying, {"msg": "Dhan has no order with that correlationId yet; waiting before placing a new stop", "position": pos.id, "correlation_id": cid}, key=f"sl-doubt{pos.id}", now=now)
                return False
            self._clear_stop_doubt(pos)
            self.store.update_position(pos)
            self._note("reconcile", pos.underlying, {"msg": "Dhan has no such stop order: it never arrived; a new stop may be placed", "position": pos.id, "correlation_id": cid}, key=f"sl-doubt{pos.id}", now=now, force=True)
            return True
        oid = str((st.raw or {}).get("orderId") or "")
        if st.state in ("REJECTED", "CANCELLED") and st.filled_qty == 0:
            self._clear_stop_doubt(pos)
            self.store.update_position(pos)
            self._note("reconcile", pos.underlying, {"msg": f"Dhan says the stop order was {st.state.lower()} with nothing filled; a new stop may be placed", "position": pos.id, "correlation_id": cid}, key=f"sl-doubt{pos.id}", now=now, force=True)
            return True
        if oid:                                             # live (or already traded): it is ours now; check_broker_stops / _sl_state read it like any other stop
            self._clear_stop_doubt(pos)
            ids["sl"] = oid
            self.store.update_position(pos)
            self._note("reconcile", pos.underlying, {"msg": f"the stop order that looked failed is at Dhan ({st.state.lower()}): kept as the stop, no second one placed", "position": pos.id, "order": oid, "correlation_id": cid}, key=f"sl-doubt{pos.id}", now=now, force=True)
            return True
        self._note("reconcile", pos.underlying, {"msg": "Dhan found the stop order but gave no order id: check the Dhan app", "position": pos.id, "correlation_id": cid}, key=f"sl-doubt{pos.id}", now=now)
        return False

    @_locked
    def clear_stop_doubt(self, position_id: int) -> Dict[str, Any]:
        """You looked in the Dhan app and no stop is there (or you cancelled it): let the engine place a stop again. Logged; places nothing itself."""
        for pos in self.store.open_positions("REAL"):
            if pos.id == position_id:
                had = bool(pos.broker_order_ids.get("sl_doubt"))
                self._clear_stop_doubt(pos)
                self.store.update_position(pos)
                self._note("manual", pos.underlying, {"action": "clear_stop_doubt", "position": pos.id, "had_mark": had}, key=f"clrsl{pos.id}", now=now_ist(), force=True)
                return {"position": pos.id, "cleared": had}
        return {"position": position_id, "cleared": False, "error": "no open REAL position with that id"}

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

    def _await_final(self, oid: str) -> Optional[execution.OrderStatus]:
        """Round 294: read an order until it is TRADED / REJECTED / CANCELLED (a cancel is asynchronous: the first read after it can still say PENDING). Returns the last answer; None = no answer.
        A broker that cannot answer at all is not waited for (same rule as `_confirm`)."""
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

    def _absorb_stop_fill(self, pos: Position, st: execution.OrderStatus, p: Params, now: datetime) -> Tuple[str, Optional[float]]:
        """Round 294: a resting stop sold `st.filled_qty` before it was cancelled / expired. Book it ONCE (the quantity already booked is kept in `sl_booked_qty`, so reading the same order again next
        cycle cannot book the same lots twice). ("filled", price) = everything is sold; ("partial", price) = whole lots booked as a closed child row, the rest stays open; ("odd", None) = less than one
        lot (a human must look); ("none", None) = nothing new to book."""
        ids = pos.broker_order_ids
        try:
            already = int(ids.get("sl_booked_qty") or 0)
        except (TypeError, ValueError):
            already = 0
        new_q = max(0, int(st.filled_qty or 0) - already)
        if new_q <= 0:
            return "none", None
        n = self._whole_lots(new_q, pos.lot_size)
        px = st.avg_price if st.avg_price > 0 else pos.sl_price
        if n >= pos.lots:
            return "filled", px
        if n >= 1:
            ids["sl_booked_qty"] = str(already + n * pos.lot_size)
            self._book_partial(pos, n, px, paper.SL, str(ids.get("sl") or ""), p, now, "broker stop")
            return "partial", px
        self._note("reconcile", pos.underlying, {"msg": "stop filled an odd quantity: check the Dhan app", "position": pos.id, "filled_qty": st.filled_qty}, key=f"sl-odd{pos.id}", now=now, force=True)
        return "odd", None

    def _sl_state(self, pos: Position, p: Params, now: datetime) -> Tuple[str, Optional[float]]:
        """Before an exit: cancel the resting stop and read what became of it. ("filled", price) = the stop had already sold everything; ("wait", None) = cannot tell, do not
        sell now; ("none", None) = no stop is working any more (the position, possibly with fewer lots after a partial stop fill, is still ours to sell)."""
        if pos.broker_order_ids.get("sl_doubt") and self.broker:      # round 292: find out first whether the stop that looked failed is resting at Dhan (then it is cancelled below like any stop)
            self._resolve_sl_doubt(pos, now)
            if pos.broker_order_ids.get("sl_doubt"):
                self._note("reconcile", pos.underlying, {"msg": "a stop order call failed earlier and may be live at Dhan: this exit goes ahead, but check the Dhan app and cancel any stop for this contract afterwards",
                                                         "position": pos.id}, key=f"x-sldoubt{pos.id}", now=now, force=True)
        sl = pos.broker_order_ids.get("sl")
        if not sl or not self.broker:
            return "none", None
        st = self.broker.order_status(sl)
        if st is not None and st.state == "TRADED" and st.avg_price > 0:
            return "filled", st.avg_price
        self.broker.cancel(sl)
        st = self._await_final(sl)                          # round 294: a cancel still in transit read as "no stop" and the SELL went out beside a live stop: two sells = a short
        if st is None and self._can_confirm():
            return "wait", None
        if st is not None and st.state == "TRADED" and st.avg_price > 0:
            return "filled", st.avg_price
        if st is not None and st.filled_qty > 0:            # the stop sold part of it before the cancel
            kind, px = self._absorb_stop_fill(pos, st, p, now)
            if kind == "filled":
                return "filled", px
        if st is not None and st.state in ("PENDING", "UNKNOWN"):
            return "wait", None                             # still working at Dhan: do not sell on top of it
        pos.broker_order_ids.pop("sl", None)
        pos.broker_order_ids.pop("sl_booked_qty", None)
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
                kind, px = self._absorb_stop_fill(pos, st, p, now) if st.filled_qty > 0 else ("none", None)      # round 294: the stop may have sold part (or all) of it before it ended
                if kind == "filled":
                    pos.note = (pos.note + " | closed by the broker-side stop (it ended " + st.state.lower() + " after filling)").strip(" |")
                    paper.close_position(pos, px, now.timestamp(), paper.SL, p)
                    self.store.update_position(pos)
                    self._note("exit", pos.underlying, {"position": pos.id, "label": pos.label(), "reason": paper.SL, "by": "broker stop", "entry": pos.entry_premium, "exit": px, "net": pos.net_pnl}, force=True, now=now)
                    done.append(pos)
                    continue
                self._note("order_failed", pos.underlying, {"side": "SL", "position": pos.id, "status": st.state, "note": "stop order gone; re-placing" + (" (it sold part first: booked)" if kind == "partial" else "")}, key=f"sl{pos.id}", now=now, force=True)
                pos.broker_order_ids.pop("sl", None)
                pos.broker_order_ids.pop("sl_booked_qty", None)
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
            if not oid and pos.broker_order_ids.get("entry_cid"):       # round 293: the BUY call failed and we never got an order id: ask Dhan by the correlationId we sent
                cid = str(pos.broker_order_ids["entry_cid"])
                look = self.broker.order_by_correlation(cid)
                if look is None or look.state == "UNKNOWN":
                    self._note("reconcile", pos.underlying, {"msg": "entry BUY may be live at Dhan and its state cannot be read yet: no stop, no second BUY. Check the Dhan app", "position": pos.id, "correlation_id": cid},
                               key=f"unk{pos.id}", now=now)
                    continue
                if look.state == "NOT_FOUND":
                    if now.timestamp() - float(pos.entry_ts or 0) < STOP_ABSENT_AFTER_S:       # a delayed order may still land
                        self._note("reconcile", pos.underlying, {"msg": "Dhan has no order with that correlationId yet; waiting before treating the BUY as never placed", "position": pos.id, "correlation_id": cid},
                                   key=f"unk{pos.id}", now=now)
                        continue
                    pos.status, pos.exit_ts, pos.exit_reason = paper.CLOSED, now.timestamp(), "NOT_FILLED"
                    pos.gross_pnl = pos.charges = pos.net_pnl = 0.0
                    pos.note = (pos.note + " | Dhan has no such BUY order: it never arrived, no position").strip(" |")
                    self.store.update_position(pos)
                    self._note("reconcile", pos.underlying, {"msg": "unknown entry resolved: the BUY never reached Dhan", "position": pos.id, "correlation_id": cid}, key=f"unk{pos.id}", now=now, force=True)
                    continue
                oid = str((look.raw or {}).get("orderId") or "")
                if not oid:
                    self._note("reconcile", pos.underlying, {"msg": "Dhan found the BUY order but gave no order id: check the Dhan app", "position": pos.id, "correlation_id": cid}, key=f"unk{pos.id}", now=now)
                    continue
                pos.broker_order_ids["entry"] = oid
                pos.broker_order_ids.pop("entry_cid", None)
                self.store.update_position(pos)
                if look.state == "PENDING":                              # still resting: cancel it (the final read below decides what filled), as _settle does at entry
                    self.broker.cancel(oid)
            st = self.broker.order_status(oid) if oid else None
            confirmed, price = False, None
            if st is not None and st.state == "TRADED" and st.avg_price > 0:
                confirmed, price = True, st.avg_price
            elif st is not None and st.state in ("REJECTED", "CANCELLED") and st.filled_qty > 0:
                n = min(self._whole_lots(st.filled_qty, pos.lot_size), pos.lots)           # round 293: part filled, the rest is gone: keep exactly the filled whole lots (it was flagged forever before)
                if n < 1:
                    self._note("reconcile", pos.underlying, {"msg": "entry filled less than one lot: no position can be kept, check the Dhan app", "position": pos.id, "filled_qty": st.filled_qty}, key=f"unk{pos.id}", now=now)
                    continue
                if n < pos.lots:
                    pos.lots = n
                    pos.note = (pos.note + f" | PARTIAL fill: {st.filled_qty}, kept {n} lot(s)").strip(" |")
                confirmed, price = True, (st.avg_price if st.avg_price > 0 else None)
            elif st is not None and st.state in ("REJECTED", "CANCELLED") and st.filled_qty == 0:
                pos.status, pos.exit_ts, pos.exit_reason = paper.CLOSED, now.timestamp(), "NOT_FILLED"
                pos.gross_pnl = pos.charges = pos.net_pnl = 0.0
                pos.note = (pos.note + " | the entry order turned out not to have filled: no position at Dhan").strip(" |")
                self.store.update_position(pos)
                self._note("reconcile", pos.underlying, {"msg": "unknown entry resolved: it never filled", "position": pos.id}, key=f"unk{pos.id}", now=now, force=True)
                continue
            else:
                net = self.broker.net_positions()
                if net is not None and execution.net_qty(net, pos.security_id, pos.segment) >= pos.qty:
                    confirmed = True
            if not confirmed:
                self._note("reconcile", pos.underlying, {"msg": "entry fill still unknown: no stop, will not be sold by the engine. Check the Dhan app", "position": pos.id}, key=f"unk{pos.id}", now=now)
                continue
            if price and abs(price - pos.entry_premium) > 1e-9:         # rebase stop / target on the real price
                was = (1.0 - pos.sl_price / pos.entry_premium) * 100.0 if pos.entry_premium and pos.sl_price else 0.0     # round 498: keep the stop % this entry was given (the noise floor may have widened it); read BEFORE the premium changes
                pos.entry_premium = price
                pp = Params.from_config(self.cfg)                      # the same percentages open_position used
                pos.sl_price = paper.round_tick(price * (1 - max(pp.sl_pct, was) / 100.0), 0.05, up=True)
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
        # round 296: compare per (segment, security id): NSE and BSE can reuse an id. A plain-dict broker (no segment view) compares by id alone, as before.
        by_seg = getattr(net, "by_segment", None)
        theirs: Dict[Tuple[str, str], int] = dict(by_seg) if by_seg is not None else {("", k): v for k, v in net.items()}
        segs_of: Dict[str, set] = {}
        for sg, sid in theirs:
            segs_of.setdefault(sid, set()).add(sg)
        own_k: Dict[Tuple[str, str], int] = {}
        for pos in self.store.open_positions("REAL"):
            sid = str(pos.security_id)
            sg = (pos.segment or "") if by_seg is not None else ""
            if by_seg is not None and not sg and len(segs_of.get(sid, ())) == 1:      # a row saved without a segment: take the only segment Dhan shows for that id
                sg = next(iter(segs_of[sid]))
            own_k[(sg, sid)] = own_k.get((sg, sid), 0) + pos.qty
        shared = {sid for sid in {k[1] for k in set(own_k) | set(theirs)} if len({k[0] for k in set(own_k) | set(theirs) if k[1] == sid}) > 1}
        name = lambda k: f"{k[0]}:{k[1]}" if k[1] in shared and k[0] else k[1]      # noqa: E731  the plain id unless two segments share it
        diffs = []
        for k in sorted(set(own_k) | set(theirs)):
            if own_k.get(k, 0) != theirs.get(k, 0):
                diffs.append({"security_id": k[1], "segment": k[0], "ours": own_k.get(k, 0), "broker": theirs.get(k, 0)})
        own = {name(k): v for k, v in own_k.items()}
        self.last_reconcile = {"ts": now.timestamp(), "ok": not diffs, "differences": diffs, "ours": own, "broker": {name(k): v for k, v in theirs.items()}}
        for d in diffs:
            self._note("reconcile", "", {**d, "msg": "our REAL position and Dhan's net position differ: check the Dhan app"}, key=f"{d['segment']}:{d['security_id']}" if d["segment"] else d["security_id"], now=now)
        return self.last_reconcile

    # ── exits ──
    def _mark_exit_doubt(self, pos: Position, payload: Dict[str, Any], reason: str, fill: float, now: datetime) -> None:
        """Round 282: the exit SELL failed in a way that may still have reached Dhan (network, HTTP 5xx). Keep the fact on the position: no second SELL and no new stop until Dhan's net
        position shows what happened (`_resolve_exit_doubt`). Before this the position simply stayed OPEN and the next cycle sent another SELL: if both had filled, the second one opened a short."""
        pos.broker_order_ids.update(exit_doubt=str(payload.get("correlationId") or "?"), exit_doubt_reason=str(reason), exit_doubt_px=repr(float(fill)))
        self.store.update_position(pos)
        self._note("reconcile", pos.underlying, {"msg": "the exit SELL may have reached Dhan: no second SELL and no new stop until Dhan's positions show what happened. If Dhan still shows the contract after "
                                                         "you checked the Dhan app (correlationId below), close it there by hand, or clear this mark (POST /fno/exit-doubt/clear) to let the engine sell again",
                                                "position": pos.id, "label": pos.label(), "correlation_id": payload.get("correlationId")}, key=f"x-doubt{pos.id}", now=now, force=True)

    def _clear_exit_doubt(self, pos: Position) -> None:
        for k in ("exit_doubt", "exit_doubt_reason", "exit_doubt_px"):
            pos.broker_order_ids.pop(k, None)

    def _resolve_exit_doubt(self, pos: Position, p: Params, now: datetime) -> str:
        """Returns none (no SELL in doubt) | wait (still unknown: nothing is sent) | closed (Dhan shows the contract flat: the SELL did go through, the position is booked as sold at the limit price
        we sent, flagged). Dhan still showing the quantity is NOT proof that the SELL never arrived (a resting limit order looks the same), so it never triggers a second SELL by itself."""
        if not pos.broker_order_ids.get("exit_doubt"):
            return "none"
        if not self.broker:
            return "wait"
        cid = str(pos.broker_order_ids.get("exit_doubt") or "")
        st = self.broker.order_by_correlation(cid) if cid and cid != "?" else None      # round 292: ask Dhan about the order itself, by the correlationId we sent
        if st is not None and st.state not in ("UNKNOWN", "NOT_FOUND"):
            try:
                fill0 = float(pos.broker_order_ids.get("exit_doubt_px") or pos.entry_premium)
            except (TypeError, ValueError):
                fill0 = pos.entry_premium
            reason0 = pos.broker_order_ids.get("exit_doubt_reason") or paper.MANUAL
            kind, px = self._exit_outcome(pos, str((st.raw or {}).get("orderId") or cid), st, fill0, reason0, p, now)
            if kind == "wait":
                self._note("reconcile", pos.underlying, {"msg": f"the exit SELL is at Dhan ({st.state.lower()}, filled {st.filled_qty}): no second SELL. Check the Dhan app", "position": pos.id, "label": pos.label(),
                                                         "correlation_id": cid}, key=f"x-doubt-found{pos.id}", now=now)
                return "wait"
            self._clear_exit_doubt(pos)
            if kind == "filled":
                pos.note = (pos.note + " | exit SELL confirmed at Dhan by its correlationId: booked at Dhan's price").strip(" |")
                self._finish_exit(pos, px if px else fill0, reason0, p, now)
                return "closed"
            self.store.update_position(pos)                 # open / partial: Dhan says nothing (more) is sold, so the engine may sell what is left
            self._note("reconcile", pos.underlying, {"msg": f"Dhan says the exit SELL was {st.state.lower()}: the mark is cleared and the engine may sell again", "position": pos.id, "label": pos.label(), "correlation_id": cid},
                       key=f"x-doubt-found{pos.id}", now=now, force=True)
            return "cleared"
        net = self.broker.net_positions()
        if net is None:
            self._note("reconcile", pos.underlying, {"msg": "an earlier exit SELL may have reached Dhan and Dhan's positions cannot be read: no new SELL", "position": pos.id}, key=f"x-doubt-net{pos.id}", now=now)
            return "wait"
        held = execution.net_qty(net, pos.security_id, pos.segment)
        if held > 0:
            self._note("reconcile", pos.underlying, {"msg": f"an earlier exit SELL may be resting at Dhan (Dhan still shows {held} of {pos.qty}): no second SELL. Check the Dhan app; close it by hand "
                                                             "or clear the mark (POST /fno/exit-doubt/clear)", "position": pos.id, "label": pos.label(), "dhan_qty": held}, key=f"x-doubt-held{pos.id}", now=now)
            return "wait"
        try:
            fill = float(pos.broker_order_ids.get("exit_doubt_px") or pos.entry_premium)
        except (TypeError, ValueError):
            fill = pos.entry_premium
        reason = pos.broker_order_ids.get("exit_doubt_reason") or paper.MANUAL
        self._clear_exit_doubt(pos)
        pos.note = (pos.note + " | exit SELL call failed; Dhan shows the contract flat: booked at the limit price sent, check the contract note").strip(" |")
        self._finish_exit(pos, fill, reason, p, now)
        return "closed"

    @_locked
    def clear_exit_doubt(self, position_id: int) -> Dict[str, Any]:
        """You checked the Dhan app and the SELL is not there: let the engine sell again. Logged; changes nothing else."""
        for pos in self.store.open_positions("REAL"):
            if pos.id == position_id:
                had = bool(pos.broker_order_ids.get("exit_doubt"))
                self._clear_exit_doubt(pos)
                self.store.update_position(pos)
                self._note("manual", pos.underlying, {"action": "clear_exit_doubt", "position": pos.id, "had_mark": had}, key=f"clr{pos.id}", now=now_ist(), force=True)
                return {"position": pos.id, "cleared": had}
        return {"position": position_id, "cleared": False, "error": "no open REAL position with that id"}

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
            if not self.broker:
                self._note("order_failed", pos.underlying, {"side": "SELL", "position": pos.id, "message": "no broker"}, key=f"x{pos.id}", now=now, force=True)
                return pos
            ok, closed = execution.exit_allowed(self.cfg, self.broker)      # round 294: closing what we hold never waits for the ARM / readiness / mode locks (those only gate opening risk)
            if not ok:
                self._note("real_blocked", pos.underlying, {"would": "SELL", "position": pos.id, "reason": reason, "locks": closed}, key=f"x{pos.id}", now=now, force=True)
                return pos
            if self._unknown_origin(pos):                   # never SELL what may not exist (that opens a short): only if Dhan's net position shows it
                net = self.broker.net_positions()
                if net is None or execution.net_qty(net, pos.security_id, pos.segment) < pos.qty:
                    self._note("reconcile", pos.underlying, {"msg": "will not SELL a position whose entry fill is unknown and not visible at Dhan: check the Dhan app", "position": pos.id},
                               key=f"x-unk{pos.id}", now=now)
                    return pos
            elif gates.after_close(now):                    # round 298: after the close the only proof we accept is Dhan's own net position (it squares INTRADAY off itself: a SELL on a closed position opens a short)
                net = self.broker.net_positions()
                have = None if net is None else execution.net_qty(net, pos.security_id, pos.segment)
                if have is None or have < pos.qty:
                    why = ("Dhan's positions cannot be read" if have is None else f"Dhan shows {have} of {pos.qty}: it was probably closed there already")
                    self._note("real_blocked", pos.underlying, {"would": "SELL", "position": pos.id, "reason": reason, "dhan_qty": have,
                                                                "locks": [f"after the close a SELL is sent only when Dhan's net position still shows the contract ({why}); check the Dhan app"]}, key=f"x-close{pos.id}", now=now)
                    return pos
            elif day_of(pos.entry_ts) < day_of(now.timestamp()) and self.armed_day() != day_of(now.timestamp()):
                # round 294: a leftover from an EARLIER day, with no ARM today. Dhan squares INTRADAY positions off itself, so it may be gone already and a SELL would open a short. Without today's ARM
                # (the operator's "yes, trade") the only proof we accept is Dhan's own net position.
                net = self.broker.net_positions()
                if net is None or execution.net_qty(net, pos.security_id, pos.segment) < pos.qty:
                    self._note("real_blocked", pos.underlying, {"would": "SELL", "position": pos.id, "reason": reason, "locks": ["a leftover from an earlier day is sold without an ARM only when Dhan's net position still shows it"]},
                               key=f"x{pos.id}", now=now, force=True)
                    return pos
            if pos.broker_order_ids.get("exit_doubt"):      # round 282: an earlier SELL call failed and may be live at Dhan: resolve it from Dhan's positions, never sell on top of it
                self._resolve_exit_doubt(pos, p, now)
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
            try:
                payload = execution.build_order(self.cfg.DHAN_CLIENT_ID, pos.security_id or 0, pos.segment, "SELL", pos.qty, fill)
            except ValueError as e:                         # round 294: a bad payload must not abort the cycle for every other position
                self._note("order_failed", pos.underlying, {"side": "SELL", "position": pos.id, "message": str(e)}, key=f"x{pos.id}", now=now, force=True)
                self._place_sl(pos, now)
                return pos
            res = self.broker.place(payload)
            if not res.ok:
                self._maybe_sent(payload, res, pos.underlying, "SELL", now, pos.id)
                self._note("order_failed", pos.underlying, {"side": "SELL", "position": pos.id, "status": res.status, "message": res.message}, key=f"x{pos.id}", now=now, force=True)
                if execution.maybe_sent(res):               # round 282: it may be live: no second SELL, and no stop on top of it (the software stop keeps watching the premium)
                    self._mark_exit_doubt(pos, payload, reason, fill, now)
                    return pos
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
            try:                                            # round 294: one position raising must not leave the others without their stop / square-off, every cycle
                self._manage_one(pos, now, p, chains, flips, closed)
            except Exception as e:  # noqa: BLE001
                log.exception("managing position %s failed", getattr(pos, "id", "?"))
                self._note("position_error", pos.underlying, {"position": pos.id, "error": f"{type(e).__name__}: {str(e)[:160]}", "msg": "managing this position raised: the others are still managed"},
                           key=f"perr{pos.id}", now=now)
        return closed

    def _manage_one(self, pos: Position, now: datetime, p: Params, chains: Dict[Tuple[str, str], Optional[Chain]], flips: Dict[str, str], closed: List[Position]) -> None:
        if pos.mode == "REAL" and pos.broker_order_ids.get("exit_doubt"):      # round 282: settle an exit SELL that was in doubt first, whatever the premium does now
            if self._resolve_exit_doubt(pos, p, now) == "closed":
                closed.append(pos)
            return
        sq = gates.after_squareoff(now, p, pos.expiry) or gates.carried_over(day_of(pos.entry_ts), now, self.holidays)   # a contract that expires today is closed earlier (expiry_squareoff_at); a leftover from an earlier day is closed at the open (round 248)
        key = (pos.underlying, pos.expiry)
        ch = self._get_chain(chains, key)
        if ch is None:                                      # round 295: no fresh chain: manage the position on the last one seen (stop, trail, square-off) rather than not at all
            ch = self.recent_chain(pos.underlying, pos.expiry, now.timestamp())
            if ch is not None:
                self._note("stale_chain", pos.underlying, {"position": pos.id, "age_s": round(ch.age_s(now.timestamp()), 1), "msg": "no fresh chain: the position is managed on the last chain seen"}, key=f"sc{pos.id}", now=now)
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
                self._unpriced = getattr(self, "_unpriced", set()) | {pos.id}
                self._note("stale", pos.underlying, {"position": pos.id, "msg": "no premium for an open position"}, key=str(pos.id), now=now)
            return
        hit = paper.evaluate_tick(pos, prem, p, squareoff=sq)
        if hit is None and flips.get(pos.key) == "flip":
            hit = (paper.SIGNAL_FLIP, prem)
        if hit:
            self._exit(pos, hit[0], side, p, now)
            if pos.status == paper.CLOSED:
                closed.append(pos)
        else:
            self.store.update_position(pos)             # persists the new high / trail

    # ── entries ──
    def _enter(self, und: str, direction: str, bias: Dict[str, Any], chain: Chain, p: Params, now: datetime, strategy: str = "CORE", ext: Optional[Dict[str, float]] = None) -> Optional[Position]:
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
        p = sizing.with_noise_stop(p, fill, pick.get("delta"), bias.get("atr"))      # round 498 (E518): a stop never inside the option's own daily noise; lots shrink so the rupee risk stays
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
            q.score_parts = {k: float(v) for k, v in (bias.get("parts") or {}).items() if isinstance(v, (int, float))}      # round 507
            q.entry_ctx = dict(ext or {})                                                                                  # round 509
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
            buy_cid = "bu" + uuid.uuid4().hex[:18]            # round 293: our own tag, so a BUY whose call failed can be looked up at Dhan afterwards
            try:
                buy_payload = execution.build_order(self.cfg.DHAN_CLIENT_ID, c.security_id, c.segment, "BUY", pos.qty, fill, correlation_id=buy_cid)
            except ValueError as e:                         # round 294
                self._note("order_failed", und, {"side": "BUY", "contract": c.label(), "message": str(e)}, key="entry", now=now, force=True)
                return None
            res = self.broker.place(buy_payload) if self.broker else execution.OrderResult(False, message="no broker")
            if not res.ok:
                self._maybe_sent(buy_payload, res, und, "BUY", now)
                failed = {"side": "BUY", "contract": c.label(), "status": res.status, "message": res.message}
                found = self.broker.order_by_correlation(buy_cid) if (self.broker and execution.maybe_sent(res)) else None
                oid = str((found.raw or {}).get("orderId") or "") if found is not None else ""
                if not execution.maybe_sent(res) or (found is not None and found.state in ("REJECTED", "CANCELLED") and found.filled_qty == 0):
                    self._note("order_failed", und, failed, key="entry", now=now, force=True)      # Dhan refused it, or says nothing was bought: no order exists
                    return None
                if oid:                                      # round 293: the BUY did reach Dhan: carry on exactly as if the call had answered (settle: wait, cancel what rests, read the fill)
                    self._note("reconcile", und, {"msg": "the BUY call looked failed but Dhan has the order (found by correlationId): handled as placed", "order": oid, "correlation_id": buy_cid,
                                                  "contract": c.label()}, key="entry-found", now=now, force=True)
                    res = execution.OrderResult(True, order_id=oid, status="TRANSIT")
                else:                                        # cannot tell (or Dhan has no such order YET): it may fill later with no stop and no row of ours. Keep a flagged row: it blocks a second BUY,
                    pos.broker_order_ids.update(entry_state="UNKNOWN", entry_cid=buy_cid)   # gets no stop and is never sold; resolve_unknown_entries looks the order up each cycle
                    pos.note = (pos.note + " | entry BUY call failed and may be live at Dhan: no stop, will not be sold until the order lookup answers").strip(" |")
                    self.store.insert_position(pos)
                    self._note("reconcile", und, {"msg": "entry BUY may be live at Dhan and cannot be read yet: kept as an unknown entry, no second BUY, no stop until Dhan answers", "position": pos.id,
                                                  "contract": c.label(), "correlation_id": buy_cid}, key="entry-doubt", now=now, force=True)
                    return pos
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
    @_locked
    def tick(self, now: Optional[datetime] = None, manage_only: bool = False) -> Dict[str, Any]:
        """One cycle. `manage_only` (round 298, the runner after the close): stops / square-off / exit-doubt and unknown-entry resolution for what is open, but no analysis and no new entry."""
        now = now or now_ist()
        out: Dict[str, Any] = {"ts": now.timestamp(), "enabled": bool(getattr(self.cfg, "FNO_ENGINE_ENABLED", False)), "entries": [], "exits": [], "decisions": {}}
        self._unpriced = set()                                                 # round 387 (E403): ids of open positions with no premium this cycle (filled by _manage_one); a REAL entry waits while any exists
        paused = self.is_paused()
        out["paused"] = paused
        self.warn_unmanaged_real(now)                                          # round 296: repeated every 15 minutes while it stays true
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
            exts: Dict[str, Dict[str, float]] = {}                         # round 509: how far each index had already moved today (recorded on the trade, never used to decide)
            cands: Dict[str, List[Dict[str, Any]]] = {}
            flips: Dict[str, str] = {}
            failed: List[str] = []                                           # round 278: analysis errors; the cycle still manages open positions, then reports not-ok
            vix: Optional[float] = None
            trading_day = gates.is_trading_day(now.date(), self.holidays)
            analysing = bool(out["enabled"] and trading_day and not manage_only and not paused)       # round 387: paused = no analysis and no new entry; stops, targets, square-off and exit-doubt still run
            for pos0 in self.store.open_positions():            # round 295: the chains of OPEN positions first. Entry analysis below used to go first and its fetches queued ahead of the stop checks
                self._get_chain(chains, (pos0.underlying, pos0.expiry))
            if analysing:
                vix = self.provider.vix()
                for und in self.underlyings():
                    try:                                                 # round 278: one index failing to analyse must not stop the exits below (stops, square-off)
                        exp = self.pick_expiry(und, now.date(), p)
                        if not exp:
                            out["decisions"][und] = {"action": "wait", "why": ["no usable expiry"]}
                            continue
                        ch = self._get_chain(chains, (und, exp), swallow=False)
                        if ch is None:
                            out["decisions"][und] = {"action": "wait", "why": ["no chain"]}
                            continue
                        mainch[und] = ch
                        summ = analytics.chain_summary(ch, p.strikes_each_side)
                        daily_u, intra_u = self.provider.daily(und), self.provider.intraday(und)
                        ctx = strategies.build_ctx(und, daily_u, intra_u, summ, now, p, self.holidays, vix)
                        bias = strategies.core_bias(ctx)                 # always computed: it also flips a held position, whichever strategy opened it
                        summaries[und], biases[und] = summ, bias
                        try:
                            exts[und] = signals.entry_extension(intra_u)
                        except Exception:  # noqa: BLE001 - an observation field must never stop an entry
                            exts[und] = {}
                        cands[und] = strategies.candidates(ctx, enabled, bias)
                        vetoes = list(ctx.vetoes)
                        ctx0 = None
                        if enabled_exp and today_iso in (self.provider.expiries(und) or []):
                            ch0 = self._get_chain(chains, (und, today_iso), swallow=False)    # the contract that expires today
                            if ch0 is not None:
                                ctx0 = strategies.build_ctx(und, daily_u, intra_u, analytics.chain_summary(ch0, p.strikes_each_side), now, p, self.holidays, vix, expiry_day=True)
                                ecands[und] = (ch0, strategies.candidates(ctx0, enabled_exp))
                                vetoes += ctx0.vetoes
                        for er in ctx.errors + (ctx0.errors if ctx0 is not None else []):        # round 278: a strategy rule that raised is logged, never silent
                            self._note("strategy_error", und, {**er, "msg": f"strategy rule raised {er['error']}: no signal from it this cycle"}, key=f"{er['strategy']}:{er['error'][:60]}", now=now)
                        for v in vetoes:                                 # a chain veto leaves no trade, so log it: the SIM has no chain, this is how live and SIM can be compared
                            self._note("veto", und, v, key=f"{v['strategy']}:{v['direction']}", now=now)
                        for ot, want in (("CE", signals.DOWN), ("PE", signals.UP)):          # a held CALL is flipped by a clear DOWN signal and vice versa
                            if bias.get("raw_direction", bias["direction"]) == want:     # round 495: a held position is still flipped by the raw lean, even when the intraday-agree rule keeps CORE from OPENING on it
                                flips[f"{und}:{ot}"] = "flip"
                    except Exception as e:  # noqa: BLE001
                        log.exception("analysis of %s failed", und)
                        err = f"{type(e).__name__}: {str(e)[:160]}"
                        failed.append(err)
                        out["decisions"][und] = {"action": "wait", "why": [f"analysis failed: {err}"]}
                        biases.pop(und, None); cands.pop(und, None); ecands.pop(und, None)
                        self._note("strategy_error", und, {"strategy": "", "error": err, "msg": f"analysis of {und} raised {err}: no entry from it this cycle, open positions are still managed"}, key=f"analysis:{err[:60]}", now=now)
            out["exits"] = [x.id for x in self.manage_open(now, p, chains, flips)] if (out["enabled"] or self.store.open_positions()) else []
            if self.broker and (self.store.open_positions("REAL") or str(getattr(self.cfg, "FNO_MODE", "DEMO")).upper() == "REAL"):
                self.reconcile(now)
            if analysing:
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
                        if mode == "REAL" and getattr(self, "_unpriced", None):          # round 387 (E403): a REAL position whose stop / target cannot be priced this cycle: no NEW real money on top of it
                            d.update(action="blocked", why=["an open position has no premium this cycle"])
                            self._note("skip", und, {**d, "stage": "unpriced_position", "strategy": sname}, key=f"unpriced:{sname}", now=now)
                            continue
                        pos = self._enter(und, cand["direction"], cand, chn, p, now, strategy=sname, ext=exts.get(und))
                        if pos is not None:
                            d.update(action="entered", strategy=sname)
                            out["entries"].append(pos.id)
                            break                                    # one new position per underlying per cycle
            self.last_tick = {"ts": now.timestamp(), "ok": not failed, "error": failed[0] if failed else None, "summary": out}
            if failed:
                out["error"] = failed[0]
        except Exception as e:  # noqa: BLE001
            log.exception("engine tick failed")
            self.last_tick = {"ts": now.timestamp(), "ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}", "summary": out}
            out["error"] = self.last_tick["error"]
        return out

    def is_paused(self) -> bool:
        """Round 387 (E403): the manual PAUSE (kv `fno_paused`, survives a restart). Never raises; unreadable = not paused (a pause only ever removes entries)."""
        try:
            return bool(self.store.kv_get("fno_paused"))
        except Exception:  # noqa: BLE001
            return False

    @_locked                                                # round 389 (E406): round 387 put its new is_paused between this method and the round 283 lock, so the decorator moved onto is_paused (a plain kv read that /fno/overview calls) and a manual close / square-off could race the runner's exit: two SELLs, a short if both filled
    def close_position(self, pid: int, reason: str = paper.MANUAL, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Round 387 (E403): close ONE open position by hand (same exit path as squareoff_all, so a REAL position cancels its resting stop and SELLs through the normal checks).
        {"error": ...} when no such open position; otherwise {"id", "closed": bool}; a refused / in-doubt SELL leaves it open and says so (closed False)."""
        now = now or now_ist()
        pos = next((x for x in self.store.open_positions() if x.id == pid), None)
        if pos is None:
            return {"error": f"no open position with id {pid}"}
        p = Params.from_config(self.cfg)
        try:
            ch = self._get_chain({}, (pos.underlying, pos.expiry)) or self.recent_chain(pos.underlying, pos.expiry, now.timestamp())
            row = ch.row(pos.strike) if ch else None
            self._exit(pos, reason, row.side(pos.opt_type) if row else None, p, now)
        except Exception as e:  # noqa: BLE001
            log.exception("manual close of position %s failed", pid)
            return {"id": pid, "closed": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        return {"id": pid, "closed": pos.status == paper.CLOSED, "exit_reason": pos.exit_reason, "net_pnl": pos.net_pnl}

    @_locked
    def squareoff_all(self, reason: str = paper.MANUAL, now: Optional[datetime] = None) -> List[int]:
        now = now or now_ist()
        p = Params.from_config(self.cfg)
        done: List[int] = []
        for pos in self.store.open_positions():
            try:                                            # round 294: a failure on one position must not stop the square-off of the rest
                ch = self._get_chain({}, (pos.underlying, pos.expiry)) or self.recent_chain(pos.underlying, pos.expiry, now.timestamp())
                row = ch.row(pos.strike) if ch else None
                self._exit(pos, reason, row.side(pos.opt_type) if row else None, p, now)
            except Exception as e:  # noqa: BLE001
                log.exception("square-off of position %s failed", getattr(pos, "id", "?"))
                self._note("position_error", pos.underlying, {"position": pos.id, "error": f"{type(e).__name__}: {str(e)[:160]}", "msg": "square-off of this position raised"}, key=f"sqerr{pos.id}", now=now, force=True)
                continue
            if pos.status == paper.CLOSED:
                done.append(pos.id)
        return done
