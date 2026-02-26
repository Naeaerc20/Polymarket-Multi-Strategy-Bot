"""
strategies/RSI_VWAP_Signal/markets/btc/bot.py
------------------------------------------------------
RSI + VWAP Signal Strategy — BTC

HOW THE BOT WORKS (step by step):
─────────────────────────────────────────────────────────────────────────────
1. PRELOAD HISTORY
   On startup, fetches the last 50 closed 5-minute candles from Binance REST
   and feeds them into the RSI and VWAP calculators. This avoids waiting
   ~75 minutes for the indicators to warm up from scratch.

2. CONNECT BINANCE WEBSOCKET
   Opens a persistent stream to wss://stream.binance.com for BTCusdt
   5-minute klines. Every time a candle CLOSES (every 5 minutes):
     • RSI is recalculated using Wilder's EMA smoothing
     • VWAP is updated (resets at UTC midnight)
     • A Signal is emitted: UP | DOWN | NEUTRAL + confidence score

3. CONNECT POLYMARKET WEBSOCKET (MarketStream)
   Simultaneously subscribes to the Polymarket CLOB market channel for the
   current BTC 5m window. Gets real-time UP and DOWN token prices.

4. WAIT FOR SIGNAL + CHECK TOKEN PRICE
   When SignalEngine emits an actionable signal (UP or DOWN) with
   confidence >= MIN_SIGNAL_CONFIDENCE:

   → direction = UP:
       Check if UP token price is inside RSI_VWAP_PRICE_RANGE
       If yes → place FAK BUY on UP token

   → direction = DOWN:
       Check if DOWN token price is inside RSI_VWAP_PRICE_RANGE
       If yes → place FAK BUY on DOWN token

   The price range check prevents buying tokens that are already priced
   too high (e.g. UP token at 0.85 when a signal says UP — not worth it).

5. WINDOW MANAGEMENT
   Each Polymarket window (5 min) is treated independently.
   Maximum 1 buy per direction per window.
   After window closes, bot finds the next active market and resets.

ENTRY RULES SUMMARY:
─────────────────────────────────────────────────────────────────────────────
  BUY UP   when: signal=UP  AND confidence>=threshold AND up_price in RANGE
  BUY DOWN when: signal=DOWN AND confidence>=threshold AND down_price in RANGE

  NEVER buy when: RSI is overbought (>70) or oversold (<30)
  NEVER buy the same side twice in the same window

.env variables (BTC-specific):
  BTC_RSI_VWAP_PRICE_RANGE    Price range to buy token e.g. "0.30-0.55"
  BTC_RSI_VWAP_AMOUNT         USDC per buy e.g. 1.0
  BTC_RSI_VWAP_POLL_INTERVAL  Seconds between price checks (default 0.5)

.env variables (shared across all RSI_VWAP assets):
  RSI_PERIOD              Candles for RSI calculation (default 14)
  RSI_OVERBOUGHT          RSI above this = avoid entries (default 70)
  RSI_OVERSOLD            RSI below this = avoid entries (default 30)
  RSI_BULL_THRESHOLD      RSI must exceed this for UP signal (default 52)
  RSI_BEAR_THRESHOLD      RSI must be below this for DOWN signal (default 48)
  MIN_SIGNAL_CONFIDENCE   Min confidence score to act on signal (default 0.55)
  SIGNAL_CANDLE_INTERVAL  Kline timeframe: 1m|3m|5m|15m (default 5m)
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

# ── Path resolution ──────────────────────────────────────────────────────────
# Dynamically finds project root and signal_engine.py regardless of
# where the project lives on disk or how deep the folder structure is.

def _find_dir_with(marker: str) -> Path:
    """Walk up from this file until a directory containing `marker` is found."""
    p = Path(__file__).resolve().parent
    for _ in range(12):
        if (p / marker).exists():
            return p
        p = p.parent
    raise FileNotFoundError(
        f"Cannot find '{marker}' in any parent directory of {__file__}. "
        f"Make sure your project structure is correct."
    )

# Project root = folder that contains order_executor.py
_ROOT = _find_dir_with("order_executor.py")

# signal_engine.py can be in markets/ OR RSI_VWAP_Signal/ — find it dynamically
_SIGNAL_ENGINE_DIR = _find_dir_with("signal_engine.py")

load_dotenv(_ROOT / ".env")

# Add both to sys.path so all imports resolve
for _p in [str(_ROOT), str(_SIGNAL_ENGINE_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from order_executor import OrderExecutor
from market_stream  import MarketStream
from signal_engine  import SignalEngine, Signal, preload_history

logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s][%(levelname)s] - %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("BTC-RSI-VWAP")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def _parse_range(raw: str):
    parts = raw.strip().split("-")
    return float(parts[0]), float(parts[1])

def _float(key, default):
    v = os.getenv(key, "")
    try:
        return float(v) if v.strip().lower() not in ("", "null", "none") else default
    except ValueError:
        return default

def _int(key, default):
    v = os.getenv(key, "")
    try:
        return int(v) if v.strip().lower() not in ("", "null", "none") else default
    except ValueError:
        return default

# Per-asset
PRICE_RANGE    = _parse_range(os.getenv("BTC_RSI_VWAP_PRICE_RANGE", "0.30-0.55"))
AMOUNT_TO_BUY  = _float("BTC_RSI_VWAP_AMOUNT",        1.0)
POLL_INTERVAL  = _float("BTC_RSI_VWAP_POLL_INTERVAL", 0.5)

# Shared signal parameters
RSI_PERIOD          = _int(  "RSI_PERIOD",           14)
RSI_OVERBOUGHT      = _float("RSI_OVERBOUGHT",       70.0)
RSI_OVERSOLD        = _float("RSI_OVERSOLD",         30.0)
RSI_BULL_THRESHOLD  = _float("RSI_BULL_THRESHOLD",   52.0)
RSI_BEAR_THRESHOLD  = _float("RSI_BEAR_THRESHOLD",   48.0)
MIN_CONFIDENCE      = _float("MIN_SIGNAL_CONFIDENCE", 0.55)
CANDLE_INTERVAL     = os.getenv("SIGNAL_CANDLE_INTERVAL", "5m")

BUY_ORDER_TYPE    = (os.getenv("BUY_ORDER_TYPE") or "FAK").upper()
WSS_READY_TIMEOUT = _float("WSS_READY_TIMEOUT", 10.0)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
CHAIN_ID  = 137

SLUG_TEMPLATES = {
    "5m" : "btc-updown-5m-{ts}",
    "15m": "btc-updown-15m-{ts}",
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
    log.info(f"Searching for active BTC {interval.upper()} market …")
    while True:
        ts = get_current_window_timestamp(interval)
        for candidate in [ts, ts + window]:
            slug   = template.format(ts=candidate)
            market = fetch_market(slug)
            if market and market.get("active") and not market.get("closed"):
                log.info(f"Found market: {slug}")
                return market
        log.info("No active market yet — retrying in 15s …")
        time.sleep(15)


def parse_market_tokens(market: dict) -> dict:
    import json as _json
    outcomes = market.get("outcomes",      "[]")
    prices   = market.get("outcomePrices", "[0.5,0.5]")
    tokens   = market.get("clobTokenIds") or market.get("clob_token_ids", "[]")

    outcomes = _json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    prices   = [float(p) for p in (_json.loads(prices) if isinstance(prices, str) else prices)]
    tokens   = _json.loads(tokens)   if isinstance(tokens, str) else tokens

    result = {}
    for i, name in enumerate(outcomes):
        key = "UP" if name.lower() in ("up", "yes") else "DOWN"
        result[key] = {"token_id": tokens[i], "price": prices[i]}
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
        return float(client.get_tick_size(token_id) or 0.01)
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


def get_token_price(stream: MarketStream, token_id: str) -> Optional[float]:
    price = stream.get_midpoint(token_id)
    if price is None:
        price = fetch_midpoint_rest(token_id)
    return price


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
#  BOT STATE
# ══════════════════════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.bought_up   = False
        self.bought_down = False
        self.up_price    = 0.0
        self.down_price  = 0.0
        self.up_cost     = 0.0
        self.down_cost   = 0.0
        self.up_signal   : Optional[Signal] = None
        self.down_signal : Optional[Signal] = None

    def summary(self) -> str:
        lines = ["BTC RSI+VWAP position summary:"]
        if self.bought_up:
            sig = self.up_signal
            lines.append(
                f"  UP   bought @ {self.up_price:.4f}  cost=${self.up_cost:.4f}"
                f"  [RSI={sig.rsi:.1f} VWAP={sig.vwap:.4f} conf={sig.confidence:.2f}]"
            )
        if self.bought_down:
            sig = self.down_signal
            lines.append(
                f"  DOWN bought @ {self.down_price:.4f}  cost=${self.down_cost:.4f}"
                f"  [RSI={sig.rsi:.1f} VWAP={sig.vwap:.4f} conf={sig.confidence:.2f}]"
            )
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_window(
    market   : dict,
    executor : OrderExecutor,
    engine   : SignalEngine,
    state    : BotState,
):
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
    log.info(f"  AMOUNT     : ${AMOUNT_TO_BUY:.2f} per side")
    log.info(f"  ORDER_TYPE : {BUY_ORDER_TYPE}")
    log.info("=" * 60)

    # Open Polymarket price stream
    stream = MarketStream(asset_ids=[token_up, token_down])
    stream.start()
    ready = stream.wait_ready(timeout=WSS_READY_TIMEOUT)
    if ready:
        log.info("[Poly WSS] Connected")
    else:
        log.warning(f"[Poly WSS] Not ready after {WSS_READY_TIMEOUT}s — using REST")

    try:
        while True:
            now = datetime.now(timezone.utc)
            if end_time and now >= end_time:
                log.info("Window closed.")
                break

            time_left  = (end_time - now).total_seconds() if end_time else 999
            mins, secs = divmod(int(time_left), 60)

            if state.bought_up and state.bought_down:
                log.info(f"[{mins:02d}:{secs:02d}]  Both sides bought — waiting for window close.")
                time.sleep(POLL_INTERVAL)
                continue

            # Get current signal from engine
            signal = engine.last_signal

            # Get current token prices
            up_price   = get_token_price(stream, token_up)
            down_price = get_token_price(stream, token_down)

            if up_price is None or down_price is None:
                log.warning("Price fetch failed — skipping tick")
                time.sleep(POLL_INTERVAL)
                continue

            # Build status string for each side
            def _status(bought, price, direction):
                if bought: return "✔ bought"
                if signal is None: return "no signal"
                if signal.direction == direction:
                    in_range = range_low <= price <= range_high
                    conf_ok  = signal.confidence >= MIN_CONFIDENCE
                    if in_range and conf_ok:
                        return f"READY conf={signal.confidence:.2f}"
                    elif not in_range:
                        return f"out of range ({price:.4f})"
                    else:
                        return f"low conf ({signal.confidence:.2f})"
                return f"signal={signal.direction if signal else 'none'}"

            rsi_str  = f"RSI={signal.rsi:.1f}" if signal else "RSI=warming"
            vwap_str = f"VWAP={signal.vwap:.4f}" if signal else "VWAP=warming"
            sig_str  = signal.direction if signal else "NEUTRAL"
            conf_str = f"conf={signal.confidence:.2f}" if signal else ""

            log.info(
                f"[{mins:02d}:{secs:02d}]  "
                f"UP={up_price:.4f}[{_status(state.bought_up, up_price, 'UP')}]  "
                f"DOWN={down_price:.4f}[{_status(state.bought_down, down_price, 'DOWN')}]  "
                f"{rsi_str}  {vwap_str}  sig={sig_str} {conf_str}"
            )

            # ── Entry logic ────────────────────────────────────────────────
            if signal and signal.is_actionable and signal.confidence >= MIN_CONFIDENCE:

                # BUY UP
                if (signal.direction == "UP"
                        and not state.bought_up
                        and range_low <= up_price <= range_high):
                    log.info(
                        f"*** UP ENTRY SIGNAL  RSI={signal.rsi:.1f}  "
                        f"price={signal.price:.2f} > VWAP={signal.vwap:.4f}  "
                        f"conf={signal.confidence:.2f} — buying {BUY_ORDER_TYPE} ***"
                    )
                    resp = executor.place_buy(
                        token_id  = token_up,
                        price     = up_price,
                        usdc_size = AMOUNT_TO_BUY,
                        tick_size = tick_up,
                    )
                    if resp and resp.get("success"):
                        shares, cost = _parse_buy_result(resp, up_price, AMOUNT_TO_BUY)
                        state.bought_up  = True
                        state.up_price   = up_price
                        state.up_cost    = cost
                        state.up_signal  = signal
                        log.info(f"  ✔ UP bought | shares={shares:.4f} | cost=${cost:.4f}")
                        log.info(state.summary())
                    else:
                        log.error(f"  ✗ UP buy failed | resp={resp}")

                # BUY DOWN
                if (signal.direction == "DOWN"
                        and not state.bought_down
                        and range_low <= down_price <= range_high):
                    log.info(
                        f"*** DOWN ENTRY SIGNAL  RSI={signal.rsi:.1f}  "
                        f"price={signal.price:.2f} < VWAP={signal.vwap:.4f}  "
                        f"conf={signal.confidence:.2f} — buying {BUY_ORDER_TYPE} ***"
                    )
                    resp = executor.place_buy(
                        token_id  = token_down,
                        price     = down_price,
                        usdc_size = AMOUNT_TO_BUY,
                        tick_size = tick_down,
                    )
                    if resp and resp.get("success"):
                        shares, cost = _parse_buy_result(resp, down_price, AMOUNT_TO_BUY)
                        state.bought_down  = True
                        state.down_price   = down_price
                        state.down_cost    = cost
                        state.down_signal  = signal
                        log.info(f"  ✔ DOWN bought | shares={shares:.4f} | cost=${cost:.4f}")
                        log.info(state.summary())
                    else:
                        log.error(f"  ✗ DOWN buy failed | resp={resp}")

            time.sleep(POLL_INTERVAL)

    finally:
        stream.stop()

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
                choice = input("Market interval — enter 5 or 15: ").strip()
                if choice in ("5", "15"):
                    interval = f"{choice}m"
                    break

    range_low, range_high = PRICE_RANGE
    log.info("=" * 60)
    log.info("BTC RSI+VWAP Strategy starting")
    log.info(f"  Market    : BTC {interval.upper()}")
    log.info(f"  Interval  : {CANDLE_INTERVAL} candles")
    log.info(f"  RSI       : period={RSI_PERIOD}  OB={RSI_OVERBOUGHT}  OS={RSI_OVERSOLD}")
    log.info(f"  Thresholds: bull>{RSI_BULL_THRESHOLD}  bear<{RSI_BEAR_THRESHOLD}")
    log.info(f"  Min conf  : {MIN_CONFIDENCE}")
    log.info(f"  PRICE_RANGE: {range_low:.2f} – {range_high:.2f}")
    log.info(f"  Amount    : ${AMOUNT_TO_BUY:.2f} per side")
    log.info("=" * 60)

    # Build signal engine
    engine = SignalEngine(
        asset              = "btc",
        rsi_period         = RSI_PERIOD,
        rsi_overbought     = RSI_OVERBOUGHT,
        rsi_oversold       = RSI_OVERSOLD,
        rsi_bull_threshold = RSI_BULL_THRESHOLD,
        rsi_bear_threshold = RSI_BEAR_THRESHOLD,
        min_confidence     = MIN_CONFIDENCE,
        interval           = CANDLE_INTERVAL,
    )

    # Pre-warm indicators with historical data (avoids 75-min wait)
    log.info("Preloading historical candles from Binance …")
    preload_history(engine, lookback_candles=50)

    # Start live WebSocket feed
    engine.start()
    log.info("Binance signal engine running — waiting for first live signal …")

    # Build CLOB client
    client   = build_clob_client()
    executor = OrderExecutor(client=client, log=log)
    log.info("CLOB client authenticated OK")

    # Main loop: window by window
    while True:
        state  = BotState()
        market = wait_for_active_market(interval)
        run_window(market, executor, engine, state)

        # Wait for next window
        end_time  = get_market_end_time(market)
        wait_secs = 30
        if end_time:
            remaining = (end_time - datetime.now(timezone.utc)).total_seconds()
            wait_secs = max(5, remaining + 5)
        log.info(f"Waiting {wait_secs:.0f}s for next window …")
        time.sleep(wait_secs)


if __name__ == "__main__":
    run()