"""
order_executor.py
-----------------
Handles order placement for Polymarket CLOB bots.

.env variables:
    BUY_ORDER_TYPE=FAK|FOK|GTC   — order type for BUY (entry/DCA)
    SELL_ORDER_TYPE=GTC           — order type for SELL (TP/SL bracket orders)
    GTC_TIMEOUT_SECONDS=null|60   — auto-cancel GTC after N seconds; null = never
    FOK_GTC_FALLBACK=true         — retry FOK as GTC on liquidity failure

Strategy:
    BUY  — executes immediately (FAK/FOK) or rests at exact price (GTC)
    SELL — always GTC bracket orders placed right after BUY:
             • one order at TAKE_PROFIT price
             • one order at STOP_LOSS price
           GTC_TIMEOUT_SECONDS=null keeps them in the book until filled or
           cancelled manually (at window close).

Decimal constraints enforced automatically:
    makerAmount = price * size  → max 2 decimal places
    takerAmount = size          → max 4 decimal places
"""

import os
import threading
from decimal import Decimal, ROUND_DOWN
from typing import Optional, Tuple, Dict

from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL


# ══════════════════════════════════════════════════════════════════════════════
#  DECIMAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _safe_order_params(price: float, usdc_size: float, tick_size=0.01) -> Tuple[float, float]:
    """
    Return (price_f, size_f) for FAK/FOK BUY.
    Snaps price DOWN to nearest tick. price * size has max 2dp, size max 4dp.
    """
    tick_d       = Decimal(str(float(tick_size)))
    price_d      = Decimal(str(price)).quantize(tick_d, rounding=ROUND_DOWN)
    price_d      = max(Decimal("0.01"), min(price_d, Decimal("0.99")))
    budget_cents = int(Decimal(str(usdc_size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN) * 100)

    for cents in range(budget_cents, max(0, budget_cents - 200), -1):
        maker_d = Decimal(cents) / Decimal("100")
        size_d  = (maker_d / price_d).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        if size_d > 0 and (price_d * size_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN) == maker_d:
            return float(price_d), float(size_d)

    size_d = (Decimal("0.01") / price_d).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return float(price_d), float(max(size_d, Decimal("0.0001")))


def _gtc_order_params(price: float, usdc_size: float, tick_size=0.01) -> Tuple[float, float]:
    """
    Return (price_f, size_f) for GTC BUY.
    Snaps to nearest tick WITHOUT slippage — exact entry price preserved.
    """
    tick_d       = Decimal(str(float(tick_size)))
    price_d      = (Decimal(str(price)) / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
    price_d      = max(Decimal("0.01"), min(price_d, Decimal("0.99")))
    budget_cents = int(Decimal(str(usdc_size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN) * 100)

    for cents in range(budget_cents, max(0, budget_cents - 200), -1):
        maker_d = Decimal(cents) / Decimal("100")
        size_d  = (maker_d / price_d).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        if size_d > 0 and (price_d * size_d).quantize(Decimal("0.01"), rounding=ROUND_DOWN) == maker_d:
            return float(price_d), float(size_d)

    size_d = (Decimal("0.01") / price_d).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return float(price_d), float(max(size_d, Decimal("0.0001")))


def _snap_price(price: float, tick_size=0.01) -> float:
    """Snap price to tick size and clamp to [0.01, 0.99]."""
    tick_d  = Decimal(str(float(tick_size)))
    price_d = (Decimal(str(price)) / tick_d).to_integral_value(rounding=ROUND_DOWN) * tick_d
    return float(max(Decimal("0.01"), min(price_d, Decimal("0.99"))))


def _sell_params(price: float, total_shares: float, tick_size=0.01) -> Tuple[float, float]:
    """
    Return (price_f, size_f) for a SELL limit order (GTC/FOK).
    Snaps price to tick, adjusts shares so that price * shares has max 2dp.
    """
    price_f  = _snap_price(price, tick_size)
    price_d  = Decimal(str(price_f))
    shares_d = Decimal(str(total_shares)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    for _ in range(200):
        taker = price_d * shares_d
        if taker == taker.quantize(Decimal("0.01"), rounding=ROUND_DOWN):
            break
        shares_d -= Decimal("0.0001")
    return price_f, float(max(shares_d, Decimal("0.0001")))


# ══════════════════════════════════════════════════════════════════════════════
#  GTC TRACKER
# ══════════════════════════════════════════════════════════════════════════════

class GtcTracker:
    """
    Tracks open GTC orders.
    If GTC_TIMEOUT_SECONDS is set, auto-cancels after that many seconds.
    If GTC_TIMEOUT_SECONDS=null, orders stay in book until cancelled manually.
    """

    def __init__(self, client):
        self.client  = client
        self._timers: Dict[str, threading.Timer] = {}

    def schedule(self, order_id: str, timeout: Optional[int], log=None) -> None:
        if timeout is None:
            msg = f"[GTC] Order {order_id} registered (no timeout — rests until filled or cancelled)"
            print(msg) if log is None else log.info(msg)
            self._timers[order_id] = None  # track it for cancel_all
            return

        def _cancel():
            msg = f"[GTC] Timeout ({timeout}s) — cancelling order {order_id}"
            print(msg) if log is None else log.warning(msg)
            try:
                self.client.cancel(order_id)
                msg2 = f"[GTC] Order {order_id} cancelled."
                print(msg2) if log is None else log.info(msg2)
            except Exception as exc:
                msg3 = f"[GTC] Cancel failed for {order_id}: {exc}"
                print(msg3) if log is None else log.error(msg3)
            finally:
                self._timers.pop(order_id, None)

        timer = threading.Timer(timeout, _cancel)
        timer.daemon = True
        timer.start()
        self._timers[order_id] = timer
        msg = f"[GTC] Auto-cancel scheduled in {timeout}s for order {order_id}"
        print(msg) if log is None else log.info(msg)

    def cancel(self, order_id: str, log=None) -> None:
        timer = self._timers.pop(order_id, None)
        if timer is not None:
            timer.cancel()
        try:
            self.client.cancel(order_id)
            msg = f"[GTC] Cancelled order {order_id}"
            print(msg) if log is None else log.info(msg)
        except Exception as exc:
            msg = f"[GTC] Cancel failed for {order_id}: {exc}"
            print(msg) if log is None else log.warning(msg)

    def cancel_all(self, log=None) -> None:
        for order_id in list(self._timers.keys()):
            self.cancel(order_id, log)

    @property
    def open_order_ids(self):
        return list(self._timers.keys())


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER EXECUTOR
# ══════════════════════════════════════════════════════════════════════════════

class OrderExecutor:
    """
    Executes BUY and SELL bracket orders on Polymarket CLOB.

    BUY  — uses BUY_ORDER_TYPE (FAK | FOK | GTC)
    SELL — uses SELL_ORDER_TYPE (always GTC for bracket orders)
           placed immediately after BUY at TP and SL prices

    Reads from .env:
        BUY_ORDER_TYPE=FAK
        SELL_ORDER_TYPE=GTC
        GTC_TIMEOUT_SECONDS=null     # null = no expiry, or integer seconds
        FOK_GTC_FALLBACK=true
    """

    def __init__(self, client, log=None):
        self.client = client
        self.log    = log

        self.buy_order_type  = (os.getenv("BUY_ORDER_TYPE")  or os.getenv("ORDER_TYPE", "FAK")).upper()
        self.sell_order_type = (os.getenv("SELL_ORDER_TYPE") or "GTC").upper()

        _timeout_raw = os.getenv("GTC_TIMEOUT_SECONDS", "null").strip().lower()
        self.gtc_timeout: Optional[int] = None if _timeout_raw == "null" else int(_timeout_raw)

        self.fok_fallback = os.getenv("FOK_GTC_FALLBACK", "true").lower() == "true"
        self.gtc_tracker  = GtcTracker(client)

    def _info(self, msg):  self.log.info(msg)    if self.log else print(msg)
    def _warn(self, msg):  self.log.warning(msg) if self.log else print(f"WARNING: {msg}")
    def _error(self, msg): self.log.error(msg)   if self.log else print(f"ERROR: {msg}")

    def _extract_order_id(self, resp: dict) -> Optional[str]:
        return resp.get("orderID") or resp.get("order_id") or resp.get("id")

    # ── Internal placement methods ─────────────────────────────────────────────

    # Minimum shares required by Polymarket for GTC limit orders
    GTC_MIN_SHARES = 5.0

    def _place_fok_order(self, token_id: str, price_f: float, size_f: float, side: str):
        args   = OrderArgs(token_id=token_id, price=price_f, size=size_f, side=side)
        signed = self.client.create_order(args)
        return self.client.post_order(signed, OrderType.FOK)

    def _place_fak_order(self, token_id: str, amount: float, side: str,
                         fallback_price: float, fallback_size: float):
        """
        FAK via MarketOrderArgs(token_id, amount, side).
          BUY  → amount = USDC to spend
          SELL → amount = shares to sell
        Falls back to FOK (limit order) if MarketOrderArgs fails.
        Both attempts are wrapped — returns None instead of raising so the
        caller can handle a failed order without crashing the bot.
        """
        try:
            margs  = MarketOrderArgs(
                token_id = token_id,
                amount   = float(Decimal(str(amount)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)),
                side     = side,
            )
            signed = self.client.create_market_order(margs)
            return self.client.post_order(signed, OrderType.FAK)
        except Exception as fak_err:
            self._warn(f"  FAK MarketOrderArgs failed ({fak_err}) — falling back to FOK")
            try:
                return self._place_fok_order(token_id, fallback_price, fallback_size, side)
            except Exception as fok_err:
                self._warn(f"  FOK fallback also failed ({fok_err}) — order skipped")
                return None

    def _place_gtc_order(self, token_id: str, price_f: float, size_f: float, side: str):
        if size_f < self.GTC_MIN_SHARES:
            self._warn(
                f"  GTC order skipped — size {size_f:.4f} shares < minimum {self.GTC_MIN_SHARES} shares. "
                f"Increase AMOUNT_PER_BET or lower entry price to meet the GTC minimum."
            )
            return None
        args   = OrderArgs(token_id=token_id, price=price_f, size=size_f, side=side)
        signed = self.client.create_order(args)
        return self.client.post_order(signed, OrderType.GTC)

    # ── BUY ────────────────────────────────────────────────────────────────────

    def place_buy(
        self,
        token_id:  str,
        price:     float,
        usdc_size: float,
        tick_size: float = 0.01,
    ) -> Optional[dict]:
        """
        Place a BUY order using BUY_ORDER_TYPE from .env.

        FAK — MarketOrderArgs(amount=USDC, side=BUY)  immediate market fill
        FOK — OrderArgs limit + FOK                   full fill or cancel
              → falls back to GTC on liquidity failure if FOK_GTC_FALLBACK=true
        GTC — OrderArgs limit + GTC                   rests in book
        """
        price_f, size_f = _safe_order_params(price, usdc_size, tick_size)
        gtc_pf, gtc_sf  = _gtc_order_params(price, usdc_size, tick_size)

        if self.buy_order_type == "FAK":
            amount_f = float(Decimal(str(usdc_size)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
            self._info(f"  BUY  ${amount_f:.2f} USDC  worst_price={price_f:.4f}  [FAK]")
            resp = self._place_fak_order(token_id, amount_f, BUY, price_f, size_f)

        elif self.buy_order_type == "FOK":
            self._info(f"  BUY  {size_f:.4f} shares @ {price_f:.4f}  [FOK]")
            try:
                resp = self._place_fok_order(token_id, price_f, size_f, BUY)
            except Exception as exc:
                exc_s = str(exc)
                if "fully filled" in exc_s or "FOK orders are fully filled" in exc_s:
                    self._warn("  BUY [FOK] liquidity failure")
                    resp = "LIQUIDITY_FAIL"
                else:
                    self._warn(f"  BUY [FOK] failed: {exc}")
                    resp = None

            if resp == "LIQUIDITY_FAIL" and self.fok_fallback:
                self._warn("  FOK failed — retrying as GTC ...")
                self._info(f"  BUY  {gtc_sf:.4f} shares @ {gtc_pf:.4f}  [GTC]")
                resp = self._place_gtc_order(token_id, gtc_pf, gtc_sf, BUY)
                if resp and isinstance(resp, dict):
                    order_id = self._extract_order_id(resp)
                    if order_id:
                        self.gtc_tracker.schedule(order_id, self.gtc_timeout, self.log)
                return resp if isinstance(resp, dict) else None

            if isinstance(resp, str):
                resp = None

        else:  # GTC
            self._info(f"  BUY  {gtc_sf:.4f} shares @ {gtc_pf:.4f}  [GTC]")
            resp = self._place_gtc_order(token_id, gtc_pf, gtc_sf, BUY)
            if resp and isinstance(resp, dict):
                order_id = self._extract_order_id(resp)
                if order_id:
                    self.gtc_tracker.schedule(order_id, self.gtc_timeout, self.log)

        return resp if isinstance(resp, dict) else None

    # ── SELL bracket orders ────────────────────────────────────────────────────

    def place_sell_bracket(
        self,
        token_id:      str,
        total_shares:  float,
        tp_price:      float,
        sl_price:      Optional[float],
        tick_size:     float = 0.01,
    ) -> dict:
        """
        Place GTC SELL orders at TP and SL prices immediately after a BUY.

        Both orders sit in the book simultaneously:
          • When TP order fills → SL order becomes orphaned → cancel_all() cleans it up
          • When SL order fills → TP order becomes orphaned → cancel_all() cleans it up

        Returns dict with order IDs:
            {"tp_order_id": "0x...", "sl_order_id": "0x..." or None}

        GTC_TIMEOUT_SECONDS=null → orders never auto-expire (recommended)
        GTC_TIMEOUT_SECONDS=60   → auto-cancel after 60s
        """
        result = {"tp_order_id": None, "sl_order_id": None}

        if total_shares < 0.0001:
            self._warn("place_sell_bracket: shares too small, skipping")
            return result

        # ── TP order ───────────────────────────────────────────────────────
        tp_pf, tp_sf = _sell_params(tp_price, total_shares, tick_size)
        self._info(f"  SELL {tp_sf:.4f} shares @ {tp_pf:.4f}  [GTC TP]")
        try:
            resp = self._place_gtc_order(token_id, tp_pf, tp_sf, SELL)
            if resp and isinstance(resp, dict):
                order_id = self._extract_order_id(resp)
                if order_id:
                    self.gtc_tracker.schedule(order_id, self.gtc_timeout, self.log)
                    result["tp_order_id"] = order_id
                    self._info(f"  TP order placed | id={order_id} | price={tp_pf:.4f}")
        except Exception as exc:
            self._error(f"  TP order failed: {exc}")

        # ── SL order ───────────────────────────────────────────────────────
        if sl_price is not None:
            sl_pf, sl_sf = _sell_params(sl_price, total_shares, tick_size)
            self._info(f"  SELL {sl_sf:.4f} shares @ {sl_pf:.4f}  [GTC SL]")
            try:
                resp = self._place_gtc_order(token_id, sl_pf, sl_sf, SELL)
                if resp and isinstance(resp, dict):
                    order_id = self._extract_order_id(resp)
                    if order_id:
                        self.gtc_tracker.schedule(order_id, self.gtc_timeout, self.log)
                        result["sl_order_id"] = order_id
                        self._info(f"  SL order placed | id={order_id} | price={sl_pf:.4f}")
            except Exception as exc:
                self._error(f"  SL order failed: {exc}")

        return result

    # ── Emergency SELL (fallback if bracket orders both fail) ──────────────────

    def place_sell_immediate(
        self,
        token_id:      str,
        total_shares:  float,
        current_price: float,
        tick_size:     float = 0.01,
    ) -> Optional[dict]:
        """
        Immediate SELL using SELL_ORDER_TYPE, with FAK → GTC → FOK fallback.
        Used as emergency exit if bracket orders were never placed or need refresh.
        """
        if total_shares < 0.0001:
            self._warn("SELL skipped — shares too small")
            return None

        price_f, size_f = _sell_params(current_price, total_shares, tick_size)

        # ── Attempt 1: FAK ─────────────────────────────────────────────────
        self._info(f"  SELL {size_f:.4f} shares  worst_price={price_f:.4f}  [FAK]")
        try:
            resp = self._place_fak_order(token_id, size_f, SELL, price_f, size_f)
            if resp and isinstance(resp, dict) and resp.get("success"):
                return resp
        except Exception as exc:
            self._warn(f"  SELL [FAK] failed: {exc}")

        # ── Attempt 2: GTC ─────────────────────────────────────────────────
        self._warn("  SELL [FAK] failed — retrying as GTC ...")
        self._info(f"  SELL {size_f:.4f} shares @ {price_f:.4f}  [GTC]")
        try:
            resp = self._place_gtc_order(token_id, price_f, size_f, SELL)
            if resp and isinstance(resp, dict):
                order_id = self._extract_order_id(resp)
                if order_id:
                    self.gtc_tracker.schedule(order_id, self.gtc_timeout, self.log)
                return resp
        except Exception as exc:
            self._warn(f"  SELL [GTC] failed: {exc}")

        # ── Attempt 3: FOK ─────────────────────────────────────────────────
        self._warn("  SELL [GTC] failed — last attempt as FOK ...")
        self._info(f"  SELL {size_f:.4f} shares @ {price_f:.4f}  [FOK]")
        try:
            resp = self._place_fok_order(token_id, price_f, size_f, SELL)
            if resp and isinstance(resp, dict):
                return resp
        except Exception as exc:
            self._warn(f"  SELL [FOK] failed: {exc}")

        self._error(
            "  SELL failed on all 3 attempts (FAK -> GTC -> FOK).\n"
            "  Possible causes:\n"
            "    1. No buyers in the order book at this price\n"
            "    2. Shares not yet settled on-chain — will retry next tick\n"
            "    3. Market closed or price out of valid range"
        )
        return None