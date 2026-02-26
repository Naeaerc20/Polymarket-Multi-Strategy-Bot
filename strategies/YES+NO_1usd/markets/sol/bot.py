"""
strategies/YES+NO_1usd/markets/sol/bot.py
--------------------------------------------------
Strategy: YES+NO Arbitrage — buy UP + DOWN for combined cost < $1.00.

═══════════════════════════════════════════════════════════════════════════════
MODES
═══════════════════════════════════════════════════════════════════════════════

LOSS_PREVENTION=false  (Standard)
  Both UP and DOWN are watched simultaneously.
  Each side buys independently when its price enters PRICE_RANGE.
  Max 1 buy per side per window.

  Example:
    UP  hits 0.43 → buy UP  with AMOUNT_TO_BUY
    DOWN hits 0.44 → buy DOWN with AMOUNT_TO_BUY
    Combined: 0.43 + 0.44 = 0.87 → profit $0.13/share ✔

LOSS_PREVENTION=true
  Phase 1: Watch BOTH sides for TRIGGER_RANGE (the higher range).
    → Whichever side hits TRIGGER_RANGE first gets bought.
    → Amount spent = calc_trigger_amount() — NOT AMOUNT_TO_BUY.
      This ensures profit even if the trigger side wins.

  Phase 2: Watch ONLY the opposite side for PRICE_RANGE (the lower range).
    → When it drops into PRICE_RANGE, buy it with AMOUNT_TO_BUY.

  Why different amounts?
    If you spend the same amount on both sides and the trigger side (bought
    at 0.52) wins, the shares you got are fewer (1/0.52 = 1.92 per dollar)
    than the total you spent on both sides. You lose money.

    The fix: spend MORE on the trigger side so its payout exceeds total cost.

    Formula:
      trigger_amount = AMOUNT_TO_BUY × P_trigger / (1 - P_trigger)
                     × (1 + TRIGGER_PROFIT_MARGIN)

    Example (P_trigger=0.52, AMOUNT_TO_BUY=$2.50, margin=5%):
      trigger_amount = 2.50 × 0.52/0.48 × 1.05 = $2.844
      shares_trigger = 2.844 / 0.52 = 5.469
      shares_price   = 2.50  / 0.43 = 5.814

      If trigger wins: 5.469 × $1.00 = $5.469  total_spent=$5.344  profit=+$0.125 ✔
      If price wins:   5.814 × $1.00 = $5.814  total_spent=$5.344  profit=+$0.470 ✔

═══════════════════════════════════════════════════════════════════════════════
ORDER TYPES
═══════════════════════════════════════════════════════════════════════════════

BUY_ORDER_TYPE=FAK   Immediate market fill. Best for fast execution.
                     Uses MarketOrderArgs — fills at best available price.

BUY_ORDER_TYPE=FOK   Limit order — fill fully at exact price or cancel.
                     Uses OrderArgs + FOK. If liquidity fails and
                     FOK_GTC_FALLBACK=true, retries as GTC automatically.

BUY_ORDER_TYPE=GTC   Limit order that rests in the book until filled.
                     Auto-cancelled after GTC_TIMEOUT_SECONDS if set.
                     Good when you want exact price but can wait.

═══════════════════════════════════════════════════════════════════════════════
.env VARIABLES
═══════════════════════════════════════════════════════════════════════════════

Per asset:
  SOL_PRICE_RANGE           e.g. "0.40-0.45"
  SOL_TRIGGER_RANGE         e.g. "0.52-0.54"  (LP only)
  SOL_LOSS_PREVENTION       true | false
  SOL_AMOUNT_TO_BUY         USDC for PRICE side (and standard mode)
  SOL_TRIGGER_PROFIT_MARGIN e.g. 0.05 = 5% profit margin on trigger side
  SOL_POLL_INTERVAL         seconds between ticks (default 0.5)

Shared:
  BUY_ORDER_TYPE          FAK | FOK | GTC
  FOK_GTC_FALLBACK        true | false
  GTC_TIMEOUT_SECONDS     seconds | null
  WSS_READY_TIMEOUT       seconds before REST fallback
"""

import os
import sys
import time
import logging
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from order_executor import OrderExecutor
from market_stream  import MarketStream

logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s][%(levelname)s] - %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("SOL-YES+NO")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def _parse_range(raw: str):
    lo, hi = raw.strip().split("-")
    return float(lo), float(hi)

def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes")

def _float(key: str, default: float) -> float:
    v = os.getenv(key, "")
    try:
        return float(v) if v.strip().lower() not in ("", "null", "none") else default
    except ValueError:
        return default

PRICE_RANGE           = _parse_range(os.getenv("SOL_PRICE_RANGE",           "0.40-0.45"))
TRIGGER_RANGE         = _parse_range(os.getenv("SOL_TRIGGER_RANGE",         "0.52-0.54"))
LOSS_PREVENTION       = _parse_bool( os.getenv("SOL_LOSS_PREVENTION",       "false"))
AMOUNT_TO_BUY         = _float("SOL_AMOUNT_TO_BUY",         1.0)
TRIGGER_PROFIT_MARGIN = _float("SOL_TRIGGER_PROFIT_MARGIN", 0.05)
POLL_INTERVAL         = _float("SOL_POLL_INTERVAL",         0.5)
WSS_READY_TIMEOUT     = _float("WSS_READY_TIMEOUT",                10.0)

# Order type — read from shared .env, used by OrderExecutor internally.
# Supported: FAK (market fill), FOK (limit, full or cancel), GTC (rests in book).
# FOK_GTC_FALLBACK and GTC_TIMEOUT_SECONDS also read by OrderExecutor from .env.
BUY_ORDER_TYPE = (os.getenv("BUY_ORDER_TYPE") or "FAK").upper()

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID  = 137

SLUG_TEMPLATES = {
    "5m" : "sol-updown-5m-{ts}",
    "15m": "sol-updown-15m-{ts}",
}
WINDOW_SECONDS = {"5m": 300, "15m": 900}


# ══════════════════════════════════════════════════════════════════════════════
#  TRIGGER AMOUNT CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

def calc_trigger_amount(trigger_price: float) -> float:
    """
    Return the USDC amount to spend on the TRIGGER side so that profit
    is positive regardless of which side wins the market.

    Derivation:
      Let T = trigger_amount, P = AMOUNT_TO_BUY (price side), p = trigger_price
      Shares bought trigger side = T / p
      Total spent = T + P

      Condition for profit when trigger side wins:
        T/p > T + P
        T/p - T > P
        T(1/p - 1) > P
        T > P * p / (1 - p)          ← minimum break-even

      Add margin:
        T = P * p / (1 - p) * (1 + margin)

    Example: P=2.50, p=0.52, margin=0.05
      T = 2.50 * 0.52/0.48 * 1.05 = $2.844
      shares = 2.844/0.52 = 5.469
      total  = 2.844 + 2.50 = $5.344
      payout if trigger wins = 5.469 → profit = 5.469 - 5.344 = +$0.125 ✔
    """
    if trigger_price <= 0 or trigger_price >= 1.0:
        return AMOUNT_TO_BUY
    minimum = AMOUNT_TO_BUY * trigger_price / (1.0 - trigger_price)
    return round(minimum * (1.0 + TRIGGER_PROFIT_MARGIN), 4)


# ══════════════════════════════════════════════════════════════════════════════
#  CLOB CLIENT
# ══════════════════════════════════════════════════════════════════════════════

def build_clob_client():
    from py_clob_client.client     import ClobClient
    from py_clob_client.clob_types import ApiCreds

    pk   = os.getenv("POLY_PRIVATE_KEY",    "")
    fund = os.getenv("FUNDER_ADDRESS",      "")
    sig  = int(os.getenv("SIGNATURE_TYPE",  "2"))
    key  = os.getenv("POLY_API_KEY",        "")
    sec  = os.getenv("POLY_API_SECRET",     "")
    pas  = os.getenv("POLY_API_PASSPHRASE", "")

    if not all([pk, fund, key, sec, pas]):
        log.error("Missing credentials — run setup.py first")
        sys.exit(1)

    creds  = ApiCreds(api_key=key, api_secret=sec, api_passphrase=pas)
    client = ClobClient(
        host=CLOB_HOST, key=pk, chain_id=CHAIN_ID,
        creds=creds, signature_type=sig, funder=fund,
    )
    client.set_api_creds(creds)
    return client


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DISCOVERY
# ══════════════════════════════════════════════════════════════════════════════

def get_current_window_timestamp(interval: str) -> int:
    window = WINDOW_SECONDS[interval]
    return (int(datetime.now(timezone.utc).timestamp()) // window) * window


def fetch_market(slug: str) -> Optional[dict]:
    try:
        resp = requests.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data.get("slug") == slug:
            return data
    except Exception as exc:
        log.warning(f"Gamma API error for {slug}: {exc}")
    return None


def wait_for_active_market(interval: str) -> dict:
    template = SLUG_TEMPLATES[interval]
    window   = WINDOW_SECONDS[interval]
    log.info(f"Searching for active SOL {interval.upper()} market ...")
    while True:
        ts = get_current_window_timestamp(interval)
        for candidate in [ts, ts + window]:
            slug   = template.format(ts=candidate)
            market = fetch_market(slug)
            if market and market.get("active") and not market.get("closed"):
                log.info(f"Found market: {slug}")
                return market
        log.info("No active market yet — retrying in 15s ...")
        time.sleep(15)


def parse_market_tokens(market: dict) -> dict:
    import json as _json
    outcomes = market.get("outcomes",      "[]")
    prices   = market.get("outcomePrices", "[0.5,0.5]")
    tokens   = market.get("clobTokenIds") or market.get("clob_token_ids", "[]")
    outcomes = _json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    prices   = [float(p) for p in (_json.loads(prices) if isinstance(prices, str) else prices)]
    tokens   = _json.loads(tokens)   if isinstance(tokens, str) else tokens
    result   = {}
    for i, name in enumerate(outcomes):
        key = "UP" if name.lower() in ("up", "yes") else "DOWN"
        result[key] = {
            "token_id": tokens[i] if i < len(tokens) else None,
            "price":    prices[i] if i < len(prices) else 0.5,
        }
    return result


def get_market_end_time(market: dict) -> Optional[datetime]:
    for field in ("endDate", "end_date_iso", "closedTime"):
        val = market.get(field)
        if val:
            try:
                return datetime.fromisoformat(val.replace("Z", "+00:00")).astimezone(timezone.utc)
            except Exception:
                continue
    return None


def get_tick_size_rest(client, token_id: str) -> float:
    try:
        resp = client.get_tick_size(token_id)
        return float(resp) if resp else 0.01
    except Exception:
        return 0.01


# ══════════════════════════════════════════════════════════════════════════════
#  PRICE FEED
# ══════════════════════════════════════════════════════════════════════════════

def fetch_midpoint_rest(token_id: str) -> Optional[float]:
    try:
        resp = requests.get(
            f"{CLOB_HOST}/midpoint", params={"token_id": token_id}, timeout=5
        )
        resp.raise_for_status()
        return float(resp.json()["mid"])
    except Exception:
        return None


def get_prices(stream: MarketStream, token_up: str, token_down: str) -> Optional[dict]:
    up   = stream.get_midpoint(token_up)   or fetch_midpoint_rest(token_up)
    down = stream.get_midpoint(token_down) or fetch_midpoint_rest(token_down)
    if up is None or down is None:
        return None
    return {"UP": up, "DOWN": down}


# ══════════════════════════════════════════════════════════════════════════════
#  BOT STATE
# ══════════════════════════════════════════════════════════════════════════════

class BotState:
    """Tracks all position data for one market window."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.bought_up        : bool          = False
        self.bought_down      : bool          = False
        self.up_shares        : float         = 0.0
        self.down_shares      : float         = 0.0
        self.up_price         : float         = 0.0   # actual fill price
        self.down_price       : float         = 0.0
        self.up_cost          : float         = 0.0   # actual USDC spent
        self.down_cost        : float         = 0.0
        # LP tracking
        self.trigger_side     : Optional[str] = None  # "UP" | "DOWN" | None
        self.trigger_amount   : float         = 0.0   # USDC spent on trigger side

    @property
    def both_bought(self) -> bool:
        return self.bought_up and self.bought_down

    @property
    def total_cost(self) -> float:
        return round(self.up_cost + self.down_cost, 6)

    def summary(self) -> str:
        lines = ["─" * 55, "  SOL YES+NO — Window Summary"]

        if self.bought_up:
            tag = " [trigger]" if self.trigger_side == "UP" else " [price range]"
            lines.append(
                f"  UP   @ {self.up_price:.4f}  "
                f"shares={self.up_shares:.4f}  cost=${self.up_cost:.4f}{tag}"
            )
        if self.bought_down:
            tag = " [trigger]" if self.trigger_side == "DOWN" else " [price range]"
            lines.append(
                f"  DOWN @ {self.down_price:.4f}  "
                f"shares={self.down_shares:.4f}  cost=${self.down_cost:.4f}{tag}"
            )

        if self.both_bought:
            lines.append(f"  Total spent    : ${self.total_cost:.4f}")
            lines.append(f"  Order type     : {BUY_ORDER_TYPE}")

            # ── Profit calculation ──────────────────────────────────────────
            # Each winning share pays $1.00. Only the winning side pays out.
            # We hold both sides so one ALWAYS wins.
            # Profit = payout_of_winning_side - total_cost
            # We don't know which side wins, so show both scenarios.

            payout_if_up_wins   = self.up_shares   * 1.0
            payout_if_down_wins = self.down_shares  * 1.0
            profit_if_up_wins   = round(payout_if_up_wins   - self.total_cost, 4)
            profit_if_down_wins = round(payout_if_down_wins - self.total_cost, 4)

            lines.append("")
            lines.append("  ── Profit scenarios ──")
            lines.append(
                f"  If UP   wins: {self.up_shares:.4f} shares × $1.00 = ${payout_if_up_wins:.4f}"
                f"  →  profit = ${profit_if_up_wins:.4f}"
                f"  {'✔' if profit_if_up_wins > 0 else '✗ LOSS'}"
            )
            lines.append(
                f"  If DOWN wins: {self.down_shares:.4f} shares × $1.00 = ${payout_if_down_wins:.4f}"
                f"  →  profit = ${profit_if_down_wins:.4f}"
                f"  {'✔' if profit_if_down_wins > 0 else '✗ LOSS'}"
            )

            worst = min(profit_if_up_wins, profit_if_down_wins)
            lines.append(
                f"  Worst case     : ${worst:.4f}  "
                f"{'GUARANTEED PROFIT ✔' if worst > 0 else 'POSSIBLE LOSS ✗'}"
            )

            if self.trigger_side:
                lines.append("")
                lines.append(f"  ── Loss Prevention active (trigger={self.trigger_side}) ──")
                lines.append(
                    f"  Trigger amount : ${self.trigger_amount:.4f}  "
                    f"(base=${AMOUNT_TO_BUY:.2f} × margin={TRIGGER_PROFIT_MARGIN*100:.0f}%)"
                )

        lines.append("─" * 55)
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def _parse_buy_result(resp: dict, fallback_price: float, fallback_usdc: float):
    """Extract (shares, cost) from order response, with fallback estimates."""
    try:
        shares = float(resp.get("takingAmount", 0))
        usdc   = float(resp.get("makingAmount", 0))
        shares = shares if shares > 0 else round(fallback_usdc / fallback_price, 4)
        usdc   = usdc   if usdc   > 0 else fallback_usdc
        return shares, usdc
    except Exception:
        return round(fallback_usdc / fallback_price, 4), fallback_usdc


def _execute_buy(
    executor  : OrderExecutor,
    token_id  : str,
    price     : float,
    tick_size : float,
    label     : str,
    usdc      : float,          # explicit — always passed, never defaults to global
) -> tuple:
    """
    Place a buy order via OrderExecutor.

    OrderExecutor reads BUY_ORDER_TYPE from .env and handles:
      FAK → MarketOrderArgs immediate fill
      FOK → limit order full-or-cancel (+ GTC fallback if FOK_GTC_FALLBACK=true)
      GTC → limit order resting in book (auto-cancel after GTC_TIMEOUT_SECONDS)

    Returns (shares, cost) on success, (None, None) on failure.
    """
    log.info(
        f"  *** {label} | price={price:.4f}  amount=${usdc:.4f}  "
        f"type={BUY_ORDER_TYPE} ***"
    )
    resp = executor.place_buy(
        token_id  = token_id,
        price     = price,
        usdc_size = usdc,
        tick_size = tick_size,
    )
    if resp and resp.get("success"):
        shares, cost = _parse_buy_result(resp, price, usdc)
        log.info(f"  ✔ {label} filled | shares={shares:.4f}  cost=${cost:.4f}")
        return shares, cost
    log.error(f"  ✗ {label} failed | resp={resp}")
    return None, None


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_window(market: dict, executor: OrderExecutor, state: BotState):
    tokens     = parse_market_tokens(market)
    end_time   = get_market_end_time(market)
    token_up   = tokens["UP"]["token_id"]
    token_down = tokens["DOWN"]["token_id"]
    tick_up    = get_tick_size_rest(executor.client, token_up)
    tick_down  = get_tick_size_rest(executor.client, token_down)

    range_low,   range_high   = PRICE_RANGE
    trigger_low, trigger_high = TRIGGER_RANGE

    # Pre-calculate trigger amount using mid of TRIGGER_RANGE for the banner
    _trig_mid = (trigger_low + trigger_high) / 2
    _trig_est = calc_trigger_amount(_trig_mid)

    log.info("=" * 60)
    log.info(f"  SOL YES+NO | Market: {market.get('id','')}")
    log.info(f"  Mode        : {'LOSS PREVENTION' if LOSS_PREVENTION else 'STANDARD'}")
    log.info(f"  Order type  : {BUY_ORDER_TYPE}")
    log.info(f"  End time    : {end_time}")
    if LOSS_PREVENTION:
        log.info(f"  TRIGGER_RANGE : {trigger_low:.2f}–{trigger_high:.2f}  "
                 f"→ auto amount ≈${_trig_est:.4f}  (margin={TRIGGER_PROFIT_MARGIN*100:.0f}%)")
        log.info(f"  PRICE_RANGE   : {range_low:.2f}–{range_high:.2f}  "
                 f"→ amount=${AMOUNT_TO_BUY:.2f}")
    else:
        log.info(f"  PRICE_RANGE   : {range_low:.2f}–{range_high:.2f}  "
                 f"→ amount=${AMOUNT_TO_BUY:.2f} per side")
    log.info("=" * 60)

    stream = MarketStream(asset_ids=[token_up, token_down])
    stream.start()
    ready = stream.wait_ready(timeout=WSS_READY_TIMEOUT)
    if ready:
        tick_up   = stream.get_tick_size(token_up)   or tick_up
        tick_down = stream.get_tick_size(token_down) or tick_down
        mid_up    = stream.get_midpoint(token_up)
        mid_down  = stream.get_midpoint(token_down)
        log.info(
            f"[WSS] Connected  "
            f"UP={f'{mid_up:.4f}' if mid_up else 'pending'}  "
            f"DOWN={f'{mid_down:.4f}' if mid_down else 'pending'}"
        )
    else:
        log.warning(f"[WSS] Not ready after {WSS_READY_TIMEOUT}s — using REST")

    try:
        while True:
            now = datetime.now(timezone.utc)
            if end_time and now >= end_time:
                log.info("Window closed.")
                break

            time_left  = (end_time - now).total_seconds() if end_time else 999
            mins, secs = divmod(int(time_left), 60)

            if state.both_bought:
                log.info(f"[{mins:02d}:{secs:02d}]  Both sides filled — waiting for resolution.")
                time.sleep(POLL_INTERVAL)
                continue

            tick_up   = stream.get_tick_size(token_up)   or tick_up
            tick_down = stream.get_tick_size(token_down) or tick_down

            prices = get_prices(stream, token_up, token_down)
            if prices is None:
                log.warning("Price fetch failed — skipping tick")
                time.sleep(POLL_INTERVAL)
                continue

            up_price   = prices["UP"]
            down_price = prices["DOWN"]
            src        = "WSS" if stream.is_connected else "REST"

            # ── Status labels ──────────────────────────────────────────────
            def _label(bought, price, side):
                if bought:
                    return "✔ bought"
                if not LOSS_PREVENTION:
                    return f"IN_RANGE" if range_low <= price <= range_high else f"waiting  [{range_low:.2f}-{range_high:.2f}]"
                # LP mode
                if state.trigger_side is None:
                    # Phase 1: both sides watching TRIGGER_RANGE
                    return f"IN_TRIGGER" if trigger_low <= price <= trigger_high else f"waiting→TR[{trigger_low:.2f}-{trigger_high:.2f}]"
                else:
                    # Phase 2: only the non-trigger side watching PRICE_RANGE
                    if state.trigger_side == side:
                        return "✔ trigger"  # already bought
                    return f"IN_RANGE" if range_low <= price <= range_high else f"waiting→PR[{range_low:.2f}-{range_high:.2f}]"

            log.info(
                f"[{mins:02d}:{secs:02d}]  "
                f"UP={up_price:.4f}[{_label(state.bought_up, up_price, 'UP')}]  "
                f"DOWN={down_price:.4f}[{_label(state.bought_down, down_price, 'DOWN')}]  "
                f"{src}"
            )

            # ══════════════════════════════════════════════════════════════
            #  BUY LOGIC
            # ══════════════════════════════════════════════════════════════

            if not LOSS_PREVENTION:
                # ─────────────────────────────────────────────────────────
                # STANDARD MODE
                # Both sides independently watch PRICE_RANGE.
                # Buy each side with AMOUNT_TO_BUY when it enters range.
                # ─────────────────────────────────────────────────────────
                if not state.bought_up and range_low <= up_price <= range_high:
                    shares, cost = _execute_buy(
                        executor, token_up, up_price, tick_up, "UP", AMOUNT_TO_BUY
                    )
                    if shares:
                        state.bought_up  = True
                        state.up_shares  = shares
                        state.up_price   = up_price
                        state.up_cost    = cost
                        log.info(state.summary())

                if not state.bought_down and range_low <= down_price <= range_high:
                    shares, cost = _execute_buy(
                        executor, token_down, down_price, tick_down, "DOWN", AMOUNT_TO_BUY
                    )
                    if shares:
                        state.bought_down  = True
                        state.down_shares  = shares
                        state.down_price   = down_price
                        state.down_cost    = cost
                        log.info(state.summary())

            else:
                # ─────────────────────────────────────────────────────────
                # LOSS PREVENTION MODE
                #
                # Phase 1 (no side bought):
                #   Monitor BOTH sides for TRIGGER_RANGE.
                #   Buy the first one that hits it with calc_trigger_amount().
                #
                # Phase 2 (one side bought via trigger):
                #   Monitor ONLY the opposite side for PRICE_RANGE.
                #   Buy it with AMOUNT_TO_BUY.
                # ─────────────────────────────────────────────────────────

                if state.trigger_side is None:
                    # ── Phase 1 ───────────────────────────────────────────
                    # Check UP first, then DOWN (arbitrary tiebreak)
                    if not state.bought_up and trigger_low <= up_price <= trigger_high:
                        trig_amt = calc_trigger_amount(up_price)
                        log.info(
                            f"  [LP Phase 1] UP hit TRIGGER {trigger_low:.2f}–{trigger_high:.2f} "
                            f"@ {up_price:.4f}  →  trigger_amount=${trig_amt:.4f}"
                        )
                        shares, cost = _execute_buy(
                            executor, token_up, up_price, tick_up, "UP [trigger]", trig_amt
                        )
                        if shares:
                            state.bought_up      = True
                            state.up_shares      = shares
                            state.up_price       = up_price
                            state.up_cost        = cost
                            state.trigger_side   = "UP"
                            state.trigger_amount = trig_amt
                            log.info(
                                f"  [LP] Trigger side=UP bought. "
                                f"Now watching DOWN for PRICE_RANGE {range_low:.2f}–{range_high:.2f}"
                            )
                            log.info(state.summary())

                    elif not state.bought_down and trigger_low <= down_price <= trigger_high:
                        trig_amt = calc_trigger_amount(down_price)
                        log.info(
                            f"  [LP Phase 1] DOWN hit TRIGGER {trigger_low:.2f}–{trigger_high:.2f} "
                            f"@ {down_price:.4f}  →  trigger_amount=${trig_amt:.4f}"
                        )
                        shares, cost = _execute_buy(
                            executor, token_down, down_price, tick_down, "DOWN [trigger]", trig_amt
                        )
                        if shares:
                            state.bought_down    = True
                            state.down_shares    = shares
                            state.down_price     = down_price
                            state.down_cost      = cost
                            state.trigger_side   = "DOWN"
                            state.trigger_amount = trig_amt
                            log.info(
                                f"  [LP] Trigger side=DOWN bought. "
                                f"Now watching UP for PRICE_RANGE {range_low:.2f}–{range_high:.2f}"
                            )
                            log.info(state.summary())

                elif state.trigger_side == "UP":
                    # ── Phase 2: UP bought via trigger, wait for DOWN ──────
                    if not state.bought_down and range_low <= down_price <= range_high:
                        log.info(
                            f"  [LP Phase 2] DOWN hit PRICE_RANGE {range_low:.2f}–{range_high:.2f} "
                            f"@ {down_price:.4f}  →  amount=${AMOUNT_TO_BUY:.4f}"
                        )
                        shares, cost = _execute_buy(
                            executor, token_down, down_price, tick_down, "DOWN [LP second]", AMOUNT_TO_BUY
                        )
                        if shares:
                            state.bought_down  = True
                            state.down_shares  = shares
                            state.down_price   = down_price
                            state.down_cost    = cost
                            log.info(state.summary())

                elif state.trigger_side == "DOWN":
                    # ── Phase 2: DOWN bought via trigger, wait for UP ──────
                    if not state.bought_up and range_low <= up_price <= range_high:
                        log.info(
                            f"  [LP Phase 2] UP hit PRICE_RANGE {range_low:.2f}–{range_high:.2f} "
                            f"@ {up_price:.4f}  →  amount=${AMOUNT_TO_BUY:.4f}"
                        )
                        shares, cost = _execute_buy(
                            executor, token_up, up_price, tick_up, "UP [LP second]", AMOUNT_TO_BUY
                        )
                        if shares:
                            state.bought_up  = True
                            state.up_shares  = shares
                            state.up_price   = up_price
                            state.up_cost    = cost
                            log.info(state.summary())

            time.sleep(POLL_INTERVAL)

    finally:
        stream.stop()
        log.info("[WSS] Disconnected.")

    if state.bought_up or state.bought_down:
        log.info(state.summary())


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run(interval: Optional[str] = None):
    if interval is None:
        try:
            import questionary
            choice   = questionary.select(
                "Select market interval:",
                choices=["5 minutes", "15 minutes"],
            ).ask()
            interval = "15m" if "15" in choice else "5m"
        except (ImportError, Exception):
            while True:
                c = input("Market interval — enter 5 or 15: ").strip()
                if c in ("5", "15"):
                    interval = f"{c}m"
                    break

    range_low,   range_high   = PRICE_RANGE
    trigger_low, trigger_high = TRIGGER_RANGE

    log.info("=" * 60)
    log.info("SOL YES+NO Strategy starting")
    log.info(f"  Market     : SOL {interval.upper()}")
    log.info(f"  Mode       : {'LOSS PREVENTION' if LOSS_PREVENTION else 'STANDARD'}")
    log.info(f"  Order type : {BUY_ORDER_TYPE}")
    if LOSS_PREVENTION:
        log.info(f"  TRIGGER    : {trigger_low:.2f}–{trigger_high:.2f}")
        log.info(f"  PRICE      : {range_low:.2f}–{range_high:.2f}")
        log.info(f"  Margin     : {TRIGGER_PROFIT_MARGIN*100:.0f}%  (profit guarantee on trigger side)")
    else:
        log.info(f"  PRICE_RANGE: {range_low:.2f}–{range_high:.2f}  both sides")
    log.info(f"  AMOUNT     : ${AMOUNT_TO_BUY:.2f}  (price side / standard mode)")
    log.info("=" * 60)

    client   = build_clob_client()
    executor = OrderExecutor(client=client, log=log)
    log.info("CLOB client authenticated OK")

    while True:
        state  = BotState()
        market = wait_for_active_market(interval)
        run_window(market, executor, state)

        end_time  = get_market_end_time(market)
        wait_secs = 30
        if end_time:
            remaining = (end_time - datetime.now(timezone.utc)).total_seconds()
            wait_secs = max(5, remaining + 5)
        log.info(f"Waiting {wait_secs:.0f}s for next window ...")
        time.sleep(wait_secs)


if __name__ == "__main__":
    run()