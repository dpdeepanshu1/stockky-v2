"""fno/realprobe.py - ONE real round trip (BUY 1 lot, hold a few seconds, SELL it) with every step explained: how the order is built and sent, how the fill is read back, where
the profit or loss comes from, and what the charges are. Round 261. Run it through scripts/fno_real_order_probe.py.

It is a MANUAL tool and sits beside the engine, not inside it: it does not use the engine's four locks, the database or the arm; it uses the same pieces the engine will use in REAL mode
(execution.build_order / DhanBroker, costs.round_trip), which is the point: the first real order proves those pieces, and the charges model, against Dhan's own books.

Safety, all enforced here (and tested with a fake broker):
  * default is a DRY RUN: it picks the contract, reads the quote, prints the exact order it WOULD send and what the experiment should cost, and sends nothing;
  * a live run needs `live=True` AND the typed phrase CONFIRM_PHRASE;
  * exactly 1 lot, LIMIT orders only (BUY at the ask, SELL at the bid), at most `max_outlay` rupees of premium, only inside 09:30-14:45 IST, only on a fresh two-sided quote with a small spread,
    never on the expiry day itself, and never when Dhan already shows a position in that contract (or cannot say);
  * an unfilled BUY is cancelled; a SELL that does not fill is cancelled and re-sent a little lower (at most `SELL_TRIES` times) and never for more than is held (a SELL on nothing would open a short);
  * when the position cannot be closed the run says so loudly, with the contract and quantity to close by hand in the Dhan app, and exits with code 3.
Exit codes: 0 round trip done (or dry run) | 2 refused / the BUY was rejected, nothing open | 3 a position is STILL OPEN | 4 the state of an order is in doubt: look in the Dhan app | 5 the BUY did not fill, cancelled, nothing open.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import costs, execution
from .params import Params

IST = timezone(timedelta(hours=5, minutes=30))
CONFIRM_PHRASE = "PLACE REAL ORDER"
WINDOW = ((9, 30), (14, 45))            # a live run only inside this IST window (no opening noise, no square-off / closing rush)
MAX_SPREAD_PCT = 3.0                    # same idea as FNO_MAX_SPREAD_PCT
MAX_CHAIN_AGE_S = 30.0
SELL_TRIES = 4                          # the SELL is re-sent this many times at most, each a little lower
FALLBACK_SEGMENT = {"NIFTY": "NSE_FNO", "SENSEX": "BSE_FNO"}


def file_stamp(dt: Optional[datetime] = None) -> str:
    """YYYYMMDD_HHMMSS in IST for the probe_<stamp>.json name. Round 269: the script used the host clock (UTC on the VM), so a file made at 17:58 IST was named 12:28."""
    return (dt or datetime.now(IST)).astimezone(IST).strftime("%Y%m%d_%H%M%S")


def round_tick(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 2)


def mask(cid: str) -> str:
    return ("*" * max(0, len(cid) - 3) + cid[-3:]) if cid else "(none)"


class _Out:
    def __init__(self, printer: Callable[[str], None]) -> None:
        self.lines: List[str] = []
        self._p = printer

    def __call__(self, s: str = "") -> None:
        self.lines.append(s)
        self._p(s)


def _wait(broker: Any, oid: str, timeout_s: int, sleep: Callable[[float], None], say: _Out, t0: float) -> Tuple[Optional[execution.OrderStatus], List[Dict[str, Any]]]:
    """Poll GET /orders/{id} once a second and print each CHANGE. Returns the last answer (None = Dhan never answered) and the timeline."""
    timeline: List[Dict[str, Any]] = []
    last: Optional[execution.OrderStatus] = None
    seen: Optional[Tuple[str, int]] = None
    for i in range(max(1, int(timeout_s))):
        st = broker.order_status(oid)
        if st is not None:
            last = st
            key = (st.state, st.filled_qty)
            if key != seen:
                seen = key
                rec = {"after_s": round(time.monotonic() - t0, 1), "state": st.state, "filled_qty": st.filled_qty, "avg_price": st.avg_price, "dhan_status": str(st.raw.get("orderStatus") or "")}
                timeline.append(rec)
                say(f"    +{rec['after_s']:>5}s  Dhan says {rec['dhan_status'] or st.state:<10} filled {st.filled_qty}  avg price {st.avg_price}")
            if st.state in ("TRADED", "REJECTED", "CANCELLED"):
                break
        if i < int(timeout_s) - 1:
            sleep(1.0)
    return last, timeline


def _cancel_and_settle(broker: Any, oid: str, sleep: Callable[[float], None], say: _Out, t0: float, fill_timeout_s: int) -> Optional[execution.OrderStatus]:
    """Cancel, then keep asking Dhan until the order is in a FINAL state (TRADED / CANCELLED / REJECTED). A cancel is only a request: the order can still fill while it travels. Returns the last
    answer; the caller treats anything that is not final as 'state in doubt' and sends nothing more (a second SELL on top of a live one could open a short)."""
    cr = broker.cancel(oid)
    say(f"  not filled in {fill_timeout_s}s: cancel sent -> {cr.status or ('ok' if cr.ok else 'refused')} {cr.message}")
    st, _ = _wait(broker, oid, 8, sleep, say, t0)
    return st


def dhan_hint(status: str, message: str) -> str:
    """Plain-words next step for the Dhan error codes seen on a first real order. Empty when the code is not one we know."""
    t = f"{status} {message}".upper()
    if "DH-906" in t and ("TRADE CONTROL" in t or "TRADER CONTROL" in t):
        return "Derivatives is switched OFF in Dhan > Trader Control (Trade Control): switch it ON there, then run again. Nothing was bought."
    if "DH-906" in t:
        return "Dhan says the order is incorrect for this account. If F&O was only just approved, the segment can take 1-2 working days to register at the exchange: try again on the next trading day. Nothing was bought."
    if "DH-905" in t:
        return "This server's IP is not whitelisted for orders (Dhan > API settings > Static IP). Nothing was bought."
    if "DH-901" in t or "DH-902" in t:
        return "The access token is expired or invalid: generate a new one. Nothing was bought."
    if "DH-904" in t or "429" in t:
        return "Dhan's rate limit: wait a minute and run again. Nothing was bought."
    if "DH-908" in t or "DH-909" in t or "DH-910" in t:
        return "Dhan had an internal / data problem: run again; if it repeats, tell Dhan support the code. Nothing was bought."
    return ""


def _quote(provider: Any, und: str, expiry: str, strike: float, side: str) -> Optional[Any]:
    ch = provider.chain(und, expiry)
    row = ch.row(strike) if ch else None
    return row.side(side) if row else None


def run(provider: Any, broker: Any, client_id: str, und: str = "NIFTY", side: str = "CE", offset: int = 0, live: bool = False, hold_s: int = 20, fill_timeout_s: int = 25,
        max_outlay: float = 15000.0, expiry_pick: Optional[str] = None, confirm: Optional[Callable[[str], str]] = None, sleep: Callable[[float], None] = time.sleep, now: Optional[Callable[[], datetime]] = None,
        printer: Callable[[str], None] = print, params: Optional[Params] = None) -> Dict[str, Any]:
    say = _Out(printer)
    p = params or Params()
    now = now or (lambda: datetime.now(IST))
    und, side = und.upper(), side.upper()
    res: Dict[str, Any] = {"live": live, "underlying": und, "side": side, "exit_code": 2, "orders": [], "lines": say.lines}
    t0 = time.monotonic()

    def done(code: int, msg: str = "") -> Dict[str, Any]:
        res["exit_code"] = code
        if msg:
            say(msg)
            res["reason"] = " ".join(msg.split())[:400]           # Round 269: keep WHY the run stopped in the saved json (the printed lines are not saved there)
        return res

    say("=" * 78)
    say(f"F&O REAL ORDER PROBE  -  {'LIVE: REAL MONEY' if live else 'DRY RUN: nothing is sent'}  -  {und} {side}  -  {now().strftime('%d %b %Y %H:%M:%S IST')}")
    say("=" * 78)
    say("WHAT THIS DOES: 1) pick the contract and read its live bid / ask  2) BUY 1 lot with a LIMIT order at the ask  3) read the fill back from Dhan  4) hold a few seconds")
    say("                5) SELL it with a LIMIT order at the bid  6) work out profit / loss and the charges and show where every rupee went.")
    say("It does NOT use the engine's locks, arm or database. Orders are INTRADAY product: nothing can be carried overnight.")
    say()

    # ---- 1. contract -------------------------------------------------------------------------------------------------------
    say("STEP 1  PICK THE CONTRACT")
    today = now().date()
    exps = [e for e in provider.expiries(und) if e and (date.fromisoformat(e) - today).days >= 1]
    if not exps:
        return done(2, "  REFUSED: no expiry at least 1 day away (market-data-service not reachable, or only the expiry day itself is left).")
    if expiry_pick:
        if expiry_pick not in exps:
            return done(2, f"  REFUSED: expiry {expiry_pick} is not one Dhan lists at least 1 day away. Available: {', '.join(exps[:6])}.")
        expiry = expiry_pick
    else:
        expiry = exps[0]
    chain = provider.chain(und, expiry)
    if chain is None or not chain.rows or chain.spot is None or chain.atm_index() is None:
        return done(2, f"  REFUSED: no option chain for {und} {expiry} from market-data-service.")
    step = chain.step() or 0.0
    i = chain.atm_index() + int(offset)
    if not (0 <= i < len(chain.rows)):
        return done(2, "  REFUSED: that strike offset is outside the chain.")
    strike = chain.rows[i].strike
    q = chain.rows[i].side(side)
    ref = provider.lookup(und, expiry, strike, side)
    if q is None or ref is None or not getattr(ref, "security_id", None) or not getattr(ref, "lot_size", None):
        return done(2, "  REFUSED: the contract is not in the scrip master / chain (no security id or lot size).")
    sid, lot, tick = int(ref.security_id), int(ref.lot_size), float(getattr(ref, "tick_size", 0.05) or 0.05)
    segment = getattr(ref, "segment", "") or FALLBACK_SEGMENT.get(und, "")
    if q.security_id and int(q.security_id) != sid:
        return done(2, f"  REFUSED: the chain says security id {q.security_id}, the scrip master says {sid}. Not sending anything on a mismatch.")
    label = f"{und} {expiry} {int(strike)} {side}"
    res.update(contract=label, security_id=sid, lot_size=lot)           # Round 269: known from here on, so a later refusal or in-doubt order still names the contract
    say(f"  nearest expiry at least 1 day away: {expiry} ({(date.fromisoformat(expiry) - today).days} day(s))   spot {chain.spot:.2f}   strike step {step:g}")
    say(f"  contract: {label}   (the ATM strike{'' if offset == 0 else f' {offset:+d} step(s)'})   Dhan security id {sid}   segment {segment}   lot size {lot} units   tick {tick}")
    say("  -> check the LOT SIZE against the exchange circular: it is what 'quantity' means below (quantity is in UNITS, 1 lot = lot-size units).")
    say()

    # ---- 2. quote ----------------------------------------------------------------------------------------------------------
    say("STEP 2  READ THE QUOTE")
    bid, ask, ltp = q.bid, q.ask, q.ltp
    age = chain.age_s(time.time())
    if not (bid and ask and bid > 0 and ask >= bid):
        return done(2, "  REFUSED: no two-sided quote (bid / ask) for this contract. A limit order needs a real price to aim at.")
    spread_pct = q.spread_pct or 0.0
    mid = q.mid
    say(f"  bid {bid}   ask {ask}   last traded {ltp}   mid {mid:.2f}   spread {ask - bid:.2f} = {spread_pct:.2f}% of mid   quote age {age:.0f}s")
    say("  bid = the best price a buyer offers (what you GET when you sell); ask = the best price a seller wants (what you PAY when you buy). You always cross this gap: it is a real cost.")
    qty = lot                                   # exactly one lot
    buy_px = round_tick(ask, tick)
    outlay = buy_px * qty
    est = costs.round_trip(und, buy_px, round_tick(bid, tick), qty, p)
    est_loss = (buy_px - round_tick(bid, tick)) * qty + est.total
    say(f"  plan: BUY {qty} units ({qty // lot} lot) LIMIT at {buy_px} -> premium to pay about Rs {outlay:,.2f} (must be in the account; options are bought with the full premium)")
    say(f"  if the price did not move at all, buying at the ask and selling at the bid would cost about Rs {est_loss:,.2f}  (spread Rs {(buy_px - round_tick(bid, tick)) * qty:,.2f} + charges Rs {est.total:,.2f}). That is the price of the lesson.")
    say()
    guard: List[str] = []
    if age > MAX_CHAIN_AGE_S:
        guard.append(f"the quote is {age:.0f}s old (limit {MAX_CHAIN_AGE_S:.0f}s): the market may be closed or the feed is stale")
    if spread_pct > MAX_SPREAD_PCT:
        guard.append(f"the spread is {spread_pct:.2f}% (limit {MAX_SPREAD_PCT}%)")
    if outlay > max_outlay:
        guard.append(f"the premium Rs {outlay:,.0f} is above --max-outlay Rs {max_outlay:,.0f}")
    hm = (now().hour, now().minute)
    if now().weekday() >= 5 or not (WINDOW[0] <= hm <= WINDOW[1]):
        guard.append(f"outside the live window {WINDOW[0][0]:02d}:{WINDOW[0][1]:02d}-{WINDOW[1][0]:02d}:{WINDOW[1][1]:02d} IST on a weekday")

    payload = execution.build_order(client_id, sid, segment, "BUY", qty, buy_px, "probebuy" + now().strftime("%H%M%S"))
    shown = dict(payload, dhanClientId=mask(client_id))
    say("STEP 3  THE ORDER, EXACTLY AS IT GOES TO DHAN  (POST /v2/orders)")
    for k, v in shown.items():
        say(f"    {k:<16} {v}")
    say("  transactionType BUY/SELL | productType INTRADAY = squared off the same day | orderType LIMIT = never worse than `price` | validity DAY | quantity in UNITS | correlationId = our tag")
    say()

    if not live:
        if guard:
            say("A live run would be REFUSED right now: " + "; ".join(guard) + ".")
        say("DRY RUN ends here: nothing was sent. To do it for real, run again with --live (it asks you to type a phrase first).")
        res.update(exit_code=0, contract=label, security_id=sid, lot_size=lot, planned={"buy_price": buy_px, "outlay": outlay, "estimated_cost_if_flat": round(est_loss, 2)})
        return res
    if guard:
        return done(2, "REFUSED: " + "; ".join(guard) + ".")
    if not client_id or broker is None:
        return done(2, "REFUSED: no Dhan client id / access token (set DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN).")
    before = broker.net_positions()
    if before is None:
        return done(2, "REFUSED: Dhan's net positions cannot be read (token, IP whitelist or network). Without that the run cannot prove it ended flat.")
    if before.get(str(sid)):
        return done(2, f"REFUSED: Dhan already shows a position of {before[str(sid)]} in this contract. Close it first.")
    typed = (confirm or input)(f"This sends a REAL order of about Rs {outlay:,.0f} premium ({label}). Type exactly  {CONFIRM_PHRASE}  to continue: ")
    if typed.strip() != CONFIRM_PHRASE:
        return done(2, "REFUSED: the confirmation phrase did not match. Nothing was sent.")
    say()

    # ---- 4. BUY ------------------------------------------------------------------------------------------------------------
    say("STEP 4  BUY")
    buys: List[Tuple[int, float]] = []
    sells: List[Tuple[int, float]] = []
    r = broker.place(payload)
    res["orders"].append({"side": "BUY", "price": buy_px, "qty": qty, "ok": r.ok, "order_id": r.order_id, "status": r.status, "message": r.message})
    if not r.ok:
        hint = dhan_hint(r.status, r.message)
        return done(2, f"  Dhan REFUSED the BUY: {r.status} {r.message}\n  Nothing is open. " + (f"WHAT TO DO: {hint}" if hint else "(DH-905 = server IP not whitelisted for orders; DH-901 = expired token; DH-906 = a Dhan account setting, see the message.)"))
    say(f"  accepted: order id {r.order_id}, first status {r.status or '?'}  (accepted = on its way to the exchange, NOT yet bought)")
    st, tl = _wait(broker, r.order_id, fill_timeout_s, sleep, say, t0)
    res["orders"][-1]["timeline"] = tl
    if st is None:
        return done(4, f"  Dhan never answered about order {r.order_id}. State in doubt: OPEN THE DHAN APP (Orders / Positions) and check {label} before doing anything else.")
    if st.state == "REJECTED":
        return done(2, f"  the exchange / Dhan REJECTED the BUY after accepting it: {st.raw.get('omsErrorDescription') or st.raw.get('remarks') or 'no reason given'}. Nothing is open.")
    if st.state != "TRADED":
        st2 = _cancel_and_settle(broker, r.order_id, sleep, say, t0, fill_timeout_s)
        if st2 is None or st2.state not in ("TRADED", "CANCELLED", "REJECTED"):
            return done(4, f"  the state of order {r.order_id} is in doubt after the cancel (Dhan does not show a final state): OPEN THE DHAN APP and check {label}. Nothing more is sent.")
        st = st2
    filled = st.filled_qty if st.state in ("TRADED", "CANCELLED") else 0
    if filled <= 0:
        return done(5, "  the BUY did not fill and is cancelled. Nothing is open. (A limit at the ask can miss when the price jumps; run again.)")
    bought_px = st.avg_price or buy_px
    buys.append((filled, bought_px))
    res["orders"][-1].update(filled_qty=filled, avg_price=bought_px)
    say(f"  BOUGHT {filled} units at an average Rs {bought_px:.2f}  -> money paid for the premium: Rs {filled * bought_px:,.2f}")
    mid_buy = mid
    say(f"  you asked Rs {buy_px}, the mid was Rs {mid_buy:.2f}: paying above the mid by Rs {(bought_px - mid_buy) * filled:,.2f} is the spread cost of the BUY.")
    say()

    # ---- 5. hold -----------------------------------------------------------------------------------------------------------
    say(f"STEP 5  HOLD {hold_s}s  (the position is now open at Dhan; this is where stop / target would work in the engine)")
    sleep(float(hold_s))
    say()

    # ---- 6. SELL -----------------------------------------------------------------------------------------------------------
    say("STEP 6  SELL")
    remaining = filled
    mid_sell: Optional[float] = None
    for attempt in range(SELL_TRIES):
        if remaining <= 0:
            break
        pos = broker.net_positions()
        if pos is not None:
            held = int(pos.get(str(sid), 0))
            if held <= 0:
                say("  Dhan shows no long position in this contract any more (closed elsewhere?). Not sending another SELL: it would open a short.")
                break
            remaining = min(remaining, held)
        qn = _quote(provider, und, expiry, strike, side)
        bid_now = (qn.bid if qn and qn.bid else bid)
        if qn and qn.mid:
            mid_sell = qn.mid
        sell_px = max(tick, round_tick(bid_now - attempt * 2 * tick, tick))
        so = execution.build_order(client_id, sid, segment, "SELL", remaining, sell_px, f"probesell{attempt}" + now().strftime("%H%M%S"))
        say(f"  attempt {attempt + 1}/{SELL_TRIES}: SELL {remaining} units LIMIT at {sell_px}  (bid now {bid_now}{'' if attempt == 0 else f', {attempt * 2} tick(s) lower to get filled'})")
        rs = broker.place(so)
        rec = {"side": "SELL", "price": sell_px, "qty": remaining, "ok": rs.ok, "order_id": rs.order_id, "status": rs.status, "message": rs.message}
        res["orders"].append(rec)
        if not rs.ok:
            say(f"  Dhan REFUSED this SELL: {rs.status} {rs.message}")
            continue
        sst, stl = _wait(broker, rs.order_id, fill_timeout_s, sleep, say, t0)
        rec["timeline"] = stl
        if sst is None:
            res["exit_code"] = 4
            return done(4, f"  the state of SELL order {rs.order_id} is in doubt: OPEN THE DHAN APP and close {label} ({remaining} units) by hand if it is still held. Not sending any more orders.")
        if sst.state != "TRADED":
            sst2 = _cancel_and_settle(broker, rs.order_id, sleep, say, t0, fill_timeout_s)
            if sst2 is None or sst2.state not in ("TRADED", "CANCELLED", "REJECTED"):
                return done(4, f"  the state of SELL order {rs.order_id} is in doubt after the cancel: OPEN THE DHAN APP and close {label} ({remaining} units) by hand if it is still held. Not sending any more orders.")
            sst = sst2
        got = sst.filled_qty if sst.state in ("TRADED", "CANCELLED") else 0
        if got > 0:
            px = sst.avg_price or sell_px
            sells.append((got, px))
            rec.update(filled_qty=got, avg_price=px)
            remaining -= got
            say(f"  SOLD {got} units at an average Rs {px:.2f}")
    sold = sum(n for n, _ in sells)
    say()
    after = broker.net_positions()
    left = int(after.get(str(sid), 0)) if after is not None else None
    res.update(contract=label, security_id=sid, bought=filled, sold=sold, dhan_net_position_after=left)
    if sold < filled and (left is None or left > 0):
        short = filled - sold if left is None else left
        say("!" * 78)
        say(f"!!  A POSITION IS STILL OPEN: {label}  quantity {short}  (security id {sid}, {segment}, product INTRADAY)")
        say("!!  CLOSE IT NOW in the Dhan app (Positions > Exit). Do not wait: INTRADAY positions are also auto squared off by Dhan near 15:15-15:25, at a worse price.")
        say("!" * 78)
        res["exit_code"] = 3
    else:
        res["exit_code"] = 0

    # ---- 7. profit / loss and charges --------------------------------------------------------------------------------------
    if sold > 0:
        q_ = sold
        b_avg = sum(n * px for n, px in buys) / sum(n for n, _ in buys)
        s_avg = sum(n * px for n, px in sells) / sold
        gross = (s_avg - b_avg) * q_
        c = costs.round_trip(und, b_avg, s_avg, q_, p)
        buy_leg, sell_leg = costs.round_trip(und, b_avg, 0.0, q_, p), costs.round_trip(und, 0.0, s_avg, q_, p)
        extra_orders = max(0, len([1 for o in res["orders"] if o["side"] == "SELL" and o.get("filled_qty")]) - 1)
        extra = extra_orders * p.brokerage_per_order * (1 + p.gst_pct / 100.0)
        total_charges = c.total + extra
        net = gross - total_charges
        ms = mid_sell if mid_sell is not None else mid
        move = (ms - mid_buy) * q_
        spread_buy = (b_avg - mid_buy) * q_
        spread_sell = (ms - s_avg) * q_
        say("STEP 7  PROFIT / LOSS AND CHARGES")
        say(f"  bought {q_} units at Rs {b_avg:.2f}   sold {q_} units at Rs {s_avg:.2f}")
        say(f"  GROSS P&L = (sell price - buy price) x quantity = ({s_avg:.2f} - {b_avg:.2f}) x {q_} = Rs {gross:,.2f}      <- this is what the Dhan Positions screen shows as realised")
        say("  CHARGES (our model, fno/costs.py; the broker takes them on top of the gross):")
        say(f"    {'':<26}{'BUY leg':>10}{'SELL leg':>10}{'total':>10}")
        for name, a, b2, t in (("brokerage (flat per order)", buy_leg.brokerage, sell_leg.brokerage, c.brokerage), ("STT (on the SELL premium)", buy_leg.stt, sell_leg.stt, c.stt),
                               ("exchange transaction", buy_leg.exchange, sell_leg.exchange, c.exchange), ("SEBI fee", buy_leg.sebi, sell_leg.sebi, c.sebi),
                               ("GST (18% of brok+exch+SEBI)", buy_leg.gst, sell_leg.gst, c.gst), ("stamp duty (on the BUY)", buy_leg.stamp, sell_leg.stamp, c.stamp)):
            say(f"    {name:<26}{a:>10.2f}{b2:>10.2f}{t:>10.2f}")
        say(f"    {'TOTAL':<26}{buy_leg.total:>10.2f}{sell_leg.total:>10.2f}{c.total:>10.2f}")
        if extra:
            say(f"    + {extra_orders} extra SELL order(s) executed: one more flat brokerage + GST each = Rs {extra:.2f} (the model counts one SELL order)")
        say(f"  NET P&L = gross - charges = {gross:,.2f} - {total_charges:,.2f} = Rs {net:,.2f}")
        say(f"  to break even the premium must rise about {total_charges / q_:.2f} points ({total_charges / (b_avg * q_) * 100:.2f}% of what you paid) just to pay the charges.")
        say("  WHERE THE MONEY WENT (the identity: gross = market move - spread cost of the BUY - spread cost of the SELL):")
        say(f"    market move   (mid at sell {ms:.2f} - mid at buy {mid_buy:.2f}) x {q_}        {move:>10,.2f}")
        say(f"    spread, BUY   ({b_avg:.2f} paid - {mid_buy:.2f} mid) x {q_}                -{spread_buy:>9,.2f}")
        say(f"    spread, SELL  ({ms:.2f} mid - {s_avg:.2f} got) x {q_}                -{spread_sell:>9,.2f}")
        say(f"    = gross                                                  {move - spread_buy - spread_sell:>10,.2f}")
        say(f"    charges                                                  -{total_charges:>9,.2f}")
        say(f"    = net                                                    {net:>10,.2f}")
        res["pnl"] = {"bought_avg": round(b_avg, 4), "sold_avg": round(s_avg, 4), "qty": q_, "gross": round(gross, 2), "charges": round(total_charges, 2), "charges_detail": c.to_json(),
                      "net": round(net, 2), "market_move": round(move, 2), "spread_buy": round(spread_buy, 2), "spread_sell": round(spread_sell, 2)}
        say()
        say("STEP 8  CHECK IT AGAINST DHAN (this is what makes the charges model trustworthy)")
        ids = ", ".join(str(o["order_id"]) for o in res["orders"] if o.get("order_id"))
        say(f"  Dhan app > Orders: order id(s) {ids}: status Traded, average price must equal the prices above.")
        say("  Dhan app > Positions: the contract shows net 0 and realised P&L = the GROSS above (charges are not in it).")
        say("  Charges: Dhan's contract note / Reports > P&L statement (available after the close or the next morning) lists brokerage, STT, exchange charges, SEBI, GST, stamp duty for this trade.")
        say("  Compare line by line with the table above. A line that differs by more than a few paise: send me both numbers and the rate gets corrected in Params (FNO_* names).")
        trades = None
        if hasattr(broker, "_call"):
            try:
                trades = [broker._call("GET", f"/trades/{o['order_id']}") for o in res["orders"] if o.get("order_id") and o.get("filled_qty")]
            except Exception as e:  # noqa: BLE001 - best effort only
                say(f"  (extra check GET /trades/<order id> not available: {type(e).__name__})")
        if trades:
            res["tradebook"] = trades
            say(f"  extra check, Dhan's own trade rows for these orders: {str(trades)[:600]}")
    say()
    say("This tool bypasses the engine's locks on purpose; the engine itself is unchanged. A backtest or one trade proves nothing about profit: this run only proves the plumbing.")
    return res