"""
strategies/YES+NO_1usd/markets/eth/bot.py
----------------------------------
Strategy: YES+NO Arbitrage — buy UP + DOWN when both are inside PRICE_RANGE.

LOGIC:
  - Monitor UP and DOWN prices via WSS (REST fallback)
  - When UP price is inside PRICE_RANGE  → buy UP  (max 1 buy per window)
  - When DOWN price is inside PRICE_RANGE → buy DOWN (max 1 buy per window)
  - Goal: capture UP + DOWN for a combined cost < $1.00 (guaranteed profit at resolution)
  - No TP, no SL, no DCA — just single FAK buy per side

PRICE_RANGE format: "0.40-0.45"  → triggers when price >= 0.40 AND price <= 0.45

.env variables:
  ETH_PRICE_RANGE        e.g. "0.40-0.45"
  ETH_AMOUNT_TO_BUY      USDC amount per side (e.g. 1.0)
  ETH_POLL_INTERVAL      seconds between price checks (e.g. 0.5)
  BUY_ORDER_TYPE                 FAK (recommended for this strategy)
  WSS_READY_TIMEOUT              seconds to wait for WSS (default 10)
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

_range_raw        = os.getenv("ETH_PRICE_RANGE", "0.40-0.45")
PRICE_RANGE       = _parse_range(_range_raw)
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
        self.bought_up   : bool  = False
        self.bought_down : bool  = False
        self.up_shares   : float = 0.0
        self.down_shares : float = 0.0
        self.up_cost     : float = 0.0
        self.down_cost   : float = 0.0
        self.up_price    : float = 0.0
        self.down_price  : float = 0.0

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
            lines.append(f"  UP   → {self.up_shares:.4f} shares @ {self.up_price:.4f}  spent=${self.up_cost:.4f}")
        if self.bought_down:
            lines.append(f"  DOWN → {self.down_shares:.4f} shares @ {self.down_price:.4f}  spent=${self.down_cost:.4f}")
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

    range_low, range_high = PRICE_RANGE

    log.info("=" * 60)
    log.info(f"Window | Market ID: {market.get('id', '')}")
    log.info(f"  UP   token : {token_up}")
    log.info(f"  DOWN token : {token_down}")
    log.info(f"  End time   : {end_time}")
    log.info(f"  PRICE_RANGE: {range_low:.2f} – {range_high:.2f}")
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

            up_status   = "✔ bought" if state.bought_up   else ("IN RANGE" if range_low <= up_price   <= range_high else "waiting")
            down_status = "✔ bought" if state.bought_down else ("IN RANGE" if range_low <= down_price <= range_high else "waiting")

            log.info(
                f"[{mins:02d}:{secs:02d}]  "
                f"UP={up_price:.4f}[{up_status}]  "
                f"DOWN={down_price:.4f}[{down_status}]  "
                f"combined={combined:.4f}  {src}"
            )

            if not state.bought_up and range_low <= up_price <= range_high:
                log.info(f"*** UP in range: {up_price:.4f} — buying {BUY_ORDER_TYPE} ***")
                resp = executor.place_buy(token_id=token_up, price=up_price,
                                          usdc_size=AMOUNT_TO_BUY, tick_size=tick_up)
                if resp and resp.get("success"):
                    shares, cost = _parse_buy_result(resp, up_price, AMOUNT_TO_BUY)
                    state.bought_up = True
                    state.up_shares = shares
                    state.up_cost   = cost
                    state.up_price  = up_price
                    log.info(f"  ✔ UP bought | shares={shares:.4f} | cost=${cost:.4f}")
                else:
                    log.error(f"  ✗ UP buy failed | resp={resp}")

            if not state.bought_down and range_low <= down_price <= range_high:
                log.info(f"*** DOWN in range: {down_price:.4f} — buying {BUY_ORDER_TYPE} ***")
                resp = executor.place_buy(token_id=token_down, price=down_price,
                                          usdc_size=AMOUNT_TO_BUY, tick_size=tick_down)
                if resp and resp.get("success"):
                    shares, cost = _parse_buy_result(resp, down_price, AMOUNT_TO_BUY)
                    state.bought_down = True
                    state.down_shares = shares
                    state.down_cost   = cost
                    state.down_price  = down_price
                    log.info(f"  ✔ DOWN bought | shares={shares:.4f} | cost=${cost:.4f}")
                else:
                    log.error(f"  ✗ DOWN buy failed | resp={resp}")

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

    range_low, range_high = PRICE_RANGE
    log.info("=" * 60)
    log.info("ETH YES+NO Strategy starting")
    log.info(f"  Market     : ETH {interval.upper()}")
    log.info(f"  Range      : {range_low:.2f} – {range_high:.2f}")
    log.info(f"  Buy amount : ${AMOUNT_TO_BUY:.2f} per side")
    log.info(f"  Buy type   : {BUY_ORDER_TYPE}")
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