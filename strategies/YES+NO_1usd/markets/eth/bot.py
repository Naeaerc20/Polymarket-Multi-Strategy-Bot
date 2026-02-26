"""
strategies/YES+NO_1usd/markets/eth/bot.py
----------------------------------
Strategy: YES+NO Arbitrage — buy UP + DOWN to capture combined cost < $1.00.

MODES (controlled by LOSS_PREVENTION in .env):

  LOSS_PREVENTION=false  (default)
    Both sides use PRICE_RANGE to trigger a buy.
    Waits until each side independently enters the range before buying.

  LOSS_PREVENTION=true
    Uses TRIGGER_RANGE as a "first side" trigger — buys whichever side
    reaches TRIGGER_RANGE first (the side that has already moved up).
    Then waits for the opposite side to fall into PRICE_RANGE before buying.
    This prevents the scenario where one side keeps rising and never returns
    to the profitable range, leaving a single exposed position.

    Example:
      TRIGGER_RANGE=0.52-0.54  → buy whichever side hits this first
      PRICE_RANGE=0.40-0.45    → then buy the opposite side when it drops here

.env variables:
  ETH_PRICE_RANGE        e.g. "0.40-0.45"  — range for standard buy (both sides if LP=false, second side if LP=true)
  ETH_TRIGGER_RANGE      e.g. "0.52-0.54"  — range for first-side buy (only used when LOSS_PREVENTION=true)
  ETH_AMOUNT_TO_BUY      USDC amount per side (e.g. 1.0)
  ETH_POLL_INTERVAL      seconds between price checks (e.g. 0.5)
  ETH_LOSS_PREVENTION    true | false  (default: false)
  BUY_ORDER_TYPE                 FAK (recommended)
  WSS_READY_TIMEOUT              seconds to wait for WSS before REST fallback
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
log = logging.getLogger("ETH-YES+NO")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def _parse_range(raw: str):
    parts = raw.strip().split("-")
    return float(parts[0]), float(parts[1])

def _parse_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes")

_range_raw        = os.getenv("ETH_PRICE_RANGE",   "0.40-0.45")
_trigger_raw      = os.getenv("ETH_TRIGGER_RANGE", "0.52-0.54")
_lp_raw           = os.getenv("ETH_LOSS_PREVENTION", "false")

PRICE_RANGE       = _parse_range(_range_raw)
TRIGGER_RANGE     = _parse_range(_trigger_raw)
LOSS_PREVENTION   = _parse_bool(_lp_raw)
AMOUNT_TO_BUY     = float(os.getenv("ETH_AMOUNT_TO_BUY",  "1.0"))
POLL_INTERVAL     = float(os.getenv("ETH_POLL_INTERVAL",   "0.5"))
BUY_ORDER_TYPE    = (os.getenv("BUY_ORDER_TYPE") or "FAK").upper()
WSS_READY_TIMEOUT = float(os.getenv("WSS_READY_TIMEOUT", "10.0"))

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID  = 137

SLUG_TEMPLATES = {
    "5m" : "eth-updown-5m-{ts}",
    "15m": "eth-updown-15m-{ts}",
}
WINDOW_SECONDS = {"5m": 300, "15m": 900}


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
        log.error("Missing credentials in .env — run setup.py first")
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
    log.info(f"Searching for active ETH {interval.upper()} market ...")
    while True:
        ts = get_current_window_timestamp(interval)
        for candidate in [ts, ts + window]:
            slug   = template.format(ts=candidate)
            market = fetch_market(slug)
            if market and market.get("active") and not market.get("closed"):
                log.info(f"Found market: {slug}")
                log.info(f"  End date : {market.get('endDate') or market.get('end_date_iso')}")
                return market
        log.info("No active market yet — retrying in 15s ...")
        time.sleep(15)


def parse_market_tokens(market: dict) -> dict:
    import json
    outcomes = market.get("outcomes",      "[]")
    prices   = market.get("outcomePrices", "[0.5,0.5]")
    tokens   = market.get("clobTokenIds") or market.get("clob_token_ids", "[]")

    outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    prices   = [float(p) for p in (json.loads(prices) if isinstance(prices, str) else prices)]
    tokens   = json.loads(tokens)   if isinstance(tokens, str) else tokens

    result = {}
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
    up_price   = stream.get_midpoint(token_up)
    down_price = stream.get_midpoint(token_down)
    if up_price is None:   up_price   = fetch_midpoint_rest(token_up)
    if down_price is None: down_price = fetch_midpoint_rest(token_down)
    if up_price is None or down_price is None:
        return None
    return {"UP": up_price, "DOWN": down_price}


# ══════════════════════════════════════════════════════════════════════════════
#  BOT STATE
# ══════════════════════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.bought_up      : bool          = False
        self.bought_down    : bool          = False
        self.up_shares      : float         = 0.0
        self.down_shares    : float         = 0.0
        self.up_cost        : float         = 0.0
        self.down_cost      : float         = 0.0
        self.up_price       : float         = 0.0
        self.down_price     : float         = 0.0
        # Loss prevention: tracks which side was bought via TRIGGER_RANGE
        # so the bot knows to wait for PRICE_RANGE on the OPPOSITE side only
        self.trigger_side   : Optional[str] = None  # "UP" | "DOWN" | None

    @property
    def both_bought(self) -> bool:
        return self.bought_up and self.bought_down

    @property
    def total_cost(self) -> float:
        return self.up_cost + self.down_cost

    @property
    def combined_price(self) -> float:
        return self.up_price + self.down_price

    def summary(self) -> str:
        lines = ["YES+NO position summary:"]
        if self.bought_up:
            tag = " [trigger]" if self.trigger_side == "UP" else ""
            lines.append(f"  UP   → {self.up_shares:.4f} shares @ {self.up_price:.4f}  spent=${self.up_cost:.4f}{tag}")
        if self.bought_down:
            tag = " [trigger]" if self.trigger_side == "DOWN" else ""
            lines.append(f"  DOWN → {self.down_shares:.4f} shares @ {self.down_price:.4f}  spent=${self.down_cost:.4f}{tag}")
        if self.both_bought:
            combined         = self.combined_price
            profit_per_share = round(1.0 - combined, 4)
            min_shares       = min(self.up_shares, self.down_shares)
            total_profit     = round(profit_per_share * min_shares, 4)
            is_profitable    = profit_per_share > 0
            lines.append(f"  Combined price : {self.up_price:.4f} + {self.down_price:.4f} = {combined:.4f} per share")
            lines.append(f"  Profit/share   : $1.00 - {combined:.4f} = ${profit_per_share:.4f}  →  {'PROFITABLE ✔' if is_profitable else 'NOT PROFITABLE ✗'}")
            lines.append(f"  Est. total P&L : ${profit_per_share:.4f} × {min_shares:.4f} shares = ${total_profit:.4f}")
            lines.append(f"  Total spent    : ${self.total_cost:.4f}  (${self.up_cost:.4f} UP + ${self.down_cost:.4f} DOWN)")
        return "\n".join(lines)


def _parse_buy_result(resp: dict, fallback_price: float, fallback_usdc: float):
    try:
        shares = float(resp.get("takingAmount", 0))
        usdc   = float(resp.get("makingAmount", 0))
        shares = shares if shares > 0 else fallback_usdc / fallback_price
        usdc   = usdc   if usdc   > 0 else fallback_usdc
        return shares, usdc
    except Exception:
        return fallback_usdc / fallback_price, fallback_usdc


def _execute_buy(executor, token_id, price, tick_size, label) -> tuple:
    """Place a FAK buy and return (shares, cost) or (None, None) on failure."""
    log.info(f"*** {label}: {price:.4f} — buying {BUY_ORDER_TYPE} ***")
    resp = executor.place_buy(token_id=token_id, price=price,
                              usdc_size=AMOUNT_TO_BUY, tick_size=tick_size)
    if resp and resp.get("success"):
        shares, cost = _parse_buy_result(resp, price, AMOUNT_TO_BUY)
        log.info(f"  ✔ {label} bought | shares={shares:.4f} | cost=${cost:.4f}")
        return shares, cost
    log.error(f"  ✗ {label} buy failed | resp={resp}")
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

    log.info("=" * 60)
    log.info(f"Window | Market ID: {market.get('id', '')}")
    log.info(f"  UP   token : {token_up}")
    log.info(f"  DOWN token : {token_down}")
    log.info(f"  End time   : {end_time}")
    log.info(f"  MODE       : {'Loss Prevention ON' if LOSS_PREVENTION else 'Standard'}")
    log.info(f"  PRICE_RANGE: {range_low:.2f} – {range_high:.2f}  {'(both sides)' if not LOSS_PREVENTION else '(second side)'}")
    if LOSS_PREVENTION:
        log.info(f"  TRIGGER    : {trigger_low:.2f} – {trigger_high:.2f}  (first side)")
    log.info(f"  BUY_AMOUNT : ${AMOUNT_TO_BUY:.2f} per side")
    log.info(f"  BUY_TYPE   : {BUY_ORDER_TYPE}")
    log.info("=" * 60)

    log.info("[WSS] Opening market channel ...")
    stream = MarketStream(asset_ids=[token_up, token_down])
    stream.start()

    ready = stream.wait_ready(timeout=WSS_READY_TIMEOUT)
    if ready:
        ts_up   = stream.get_tick_size(token_up)
        ts_down = stream.get_tick_size(token_down)
        if ts_up   != tick_up:   tick_up   = ts_up
        if ts_down != tick_down: tick_down = ts_down
        mid_up   = stream.get_midpoint(token_up)
        mid_down = stream.get_midpoint(token_down)
        up_str   = f"{mid_up:.4f}"   if mid_up   is not None else "pending"
        down_str = f"{mid_down:.4f}" if mid_down is not None else "pending"
        log.info(f"[WSS] Connected — UP={up_str}  DOWN={down_str}")
    else:
        log.warning(f"[WSS] Not ready after {WSS_READY_TIMEOUT}s — using REST fallback")

    try:
        while True:
            now = datetime.now(timezone.utc)
            if end_time and now >= end_time:
                log.info("Window closed.")
                break

            time_left  = (end_time - now).total_seconds() if end_time else 999
            mins, secs = divmod(int(time_left), 60)

            if state.both_bought:
                log.info(f"[{mins:02d}:{secs:02d}]  Both sides purchased — waiting for window close.")
                time.sleep(POLL_INTERVAL)
                continue

            tick_up   = stream.get_tick_size(token_up)
            tick_down = stream.get_tick_size(token_down)

            prices = get_prices(stream, token_up, token_down)
            if prices is None:
                log.warning("Price fetch failed — skipping tick")
                time.sleep(POLL_INTERVAL)
                continue

            up_price   = prices["UP"]
            down_price = prices["DOWN"]
            combined   = up_price + down_price
            src        = "WSS" if stream.is_connected else "REST"

            # ── Status labels for tick display ─────────────────────────────
            def _status(bought, price, is_trigger_side_active):
                if bought:
                    return "✔ bought"
                if LOSS_PREVENTION and is_trigger_side_active:
                    # After first-side trigger buy, opposite side waits for PRICE_RANGE
                    return "IN RANGE" if range_low <= price <= range_high else "waiting→PR"
                if LOSS_PREVENTION and not state.trigger_side:
                    # No side bought yet — both watching TRIGGER_RANGE
                    return "IN TRIGGER" if trigger_low <= price <= trigger_high else "waiting→TR"
                # Standard mode
                return "IN RANGE" if range_low <= price <= range_high else "waiting"

            up_in_trigger_active   = state.trigger_side == "DOWN"  # UP is the "opposite" side
            down_in_trigger_active = state.trigger_side == "UP"    # DOWN is the "opposite" side
            up_status   = _status(state.bought_up,   up_price,   up_in_trigger_active)
            down_status = _status(state.bought_down, down_price, down_in_trigger_active)

            log.info(
                f"[{mins:02d}:{secs:02d}]  "
                f"UP={up_price:.4f}[{up_status}]  "
                f"DOWN={down_price:.4f}[{down_status}]  "
                f"combined={combined:.4f}  {src}"
            )

            # ══════════════════════════════════════════════════════════════
            #  BUY LOGIC
            # ══════════════════════════════════════════════════════════════

            if not LOSS_PREVENTION:
                # ── Standard mode: both sides use PRICE_RANGE ─────────────
                if not state.bought_up and range_low <= up_price <= range_high:
                    shares, cost = _execute_buy(executor, token_up, up_price, tick_up, "UP")
                    if shares:
                        state.bought_up = True
                        state.up_shares = shares
                        state.up_cost   = cost
                        state.up_price  = up_price

                if not state.bought_down and range_low <= down_price <= range_high:
                    shares, cost = _execute_buy(executor, token_down, down_price, tick_down, "DOWN")
                    if shares:
                        state.bought_down = True
                        state.down_shares = shares
                        state.down_cost   = cost
                        state.down_price  = down_price

            else:
                # ── Loss prevention mode ───────────────────────────────────
                #
                # Phase 1 — No side bought yet:
                #   Watch both UP and DOWN for TRIGGER_RANGE.
                #   Buy whichever hits it first.
                #
                # Phase 2 — One side bought via trigger:
                #   Watch ONLY the opposite side for PRICE_RANGE.
                #   Do NOT buy the already-bought side again.

                if not state.bought_up and not state.bought_down:
                    # Phase 1: neither side bought — watch TRIGGER_RANGE on both
                    if trigger_low <= up_price <= trigger_high:
                        log.info(f"  [LP] UP hit TRIGGER_RANGE {trigger_low:.2f}–{trigger_high:.2f}")
                        shares, cost = _execute_buy(executor, token_up, up_price, tick_up, "UP [trigger]")
                        if shares:
                            state.bought_up    = True
                            state.up_shares    = shares
                            state.up_cost      = cost
                            state.up_price     = up_price
                            state.trigger_side = "UP"
                            log.info(f"  [LP] Now waiting for DOWN to enter PRICE_RANGE {range_low:.2f}–{range_high:.2f}")

                    elif trigger_low <= down_price <= trigger_high:
                        log.info(f"  [LP] DOWN hit TRIGGER_RANGE {trigger_low:.2f}–{trigger_high:.2f}")
                        shares, cost = _execute_buy(executor, token_down, down_price, tick_down, "DOWN [trigger]")
                        if shares:
                            state.bought_down  = True
                            state.down_shares  = shares
                            state.down_cost    = cost
                            state.down_price   = down_price
                            state.trigger_side = "DOWN"
                            log.info(f"  [LP] Now waiting for UP to enter PRICE_RANGE {range_low:.2f}–{range_high:.2f}")

                elif state.trigger_side == "UP" and not state.bought_down:
                    # Phase 2: UP was bought via trigger — wait for DOWN in PRICE_RANGE
                    if range_low <= down_price <= range_high:
                        shares, cost = _execute_buy(executor, token_down, down_price, tick_down, "DOWN [LP second]")
                        if shares:
                            state.bought_down = True
                            state.down_shares = shares
                            state.down_cost   = cost
                            state.down_price  = down_price

                elif state.trigger_side == "DOWN" and not state.bought_up:
                    # Phase 2: DOWN was bought via trigger — wait for UP in PRICE_RANGE
                    if range_low <= up_price <= range_high:
                        shares, cost = _execute_buy(executor, token_up, up_price, tick_up, "UP [LP second]")
                        if shares:
                            state.bought_up = True
                            state.up_shares = shares
                            state.up_cost   = cost
                            state.up_price  = up_price

            if state.bought_up or state.bought_down:
                log.info(state.summary())

            time.sleep(POLL_INTERVAL)

    finally:
        log.info("[WSS] Closing market channel.")
        stream.stop()

    log.info("Window loop ended.")
    if state.bought_up or state.bought_down:
        log.info(state.summary())


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run(interval: Optional[str] = None):
    if interval is None:
        try:
            import questionary
            choice = questionary.select(
                "Select market interval:",
                choices=["5 minutes", "15 minutes"],
            ).ask()
            interval = "15m" if "15" in choice else "5m"
        except (ImportError, Exception):
            while True:
                choice = input("Select market interval — enter 5 or 15: ").strip()
                if choice in ("5", "15"):
                    interval = f"{choice}m"
                    break
                print("  Please enter 5 or 15")

    range_low, range_high     = PRICE_RANGE
    trigger_low, trigger_high = TRIGGER_RANGE

    log.info("=" * 60)
    log.info("ETH YES+NO Strategy starting")
    log.info(f"  Market          : ETH {interval.upper()}")
    log.info(f"  Mode            : {'Loss Prevention ON' if LOSS_PREVENTION else 'Standard'}")
    log.info(f"  PRICE_RANGE     : {range_low:.2f} – {range_high:.2f}")
    if LOSS_PREVENTION:
        log.info(f"  TRIGGER_RANGE   : {trigger_low:.2f} – {trigger_high:.2f}  (first side entry)")
        log.info(f"  Flow            : buy first side @ TRIGGER → buy second side @ PRICE_RANGE")
    log.info(f"  Buy amount      : ${AMOUNT_TO_BUY:.2f} per side")
    log.info(f"  Buy type        : {BUY_ORDER_TYPE}")
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