"""
strategies/DCA_Snipe/markets/sol/bot.py
-----------------------------------------------
SOL Up/Down Bot for Polymarket — DCA Snipe strategy.

═══════════════════════════════════════════════════════════════════════════════
ROOT CAUSE FIX — "Not Enough Allowance" on SELL orders
═══════════════════════════════════════════════════════════════════════════════

The error was caused by two compounding issues:

1. SHARES OVERESTIMATION on FAK fallback
   When a FAK buy returns {"success": true} but takingAmount=0 (partial fill or
   Polymarket not echoing amounts), the bot estimated shares as usdc/price.
   Due to floating-point imprecision this could give e.g. 1.66666... shares
   while the CTF contract actually credited 1.6666 (4dp truncated).
   Attempting to SELL 1.6667 when wallet has 1.6666 → "Not Enough Allowance".
   FIX: fallback estimate uses ROUND_DOWN to 4dp — always conservative.

2. STALE TOTAL_SHARES after TP fill
   If a GTC take-profit order fills while the bot is still in its polling loop
   (e.g. waiting for DCA trigger), the shares are already sold on-chain.
   The bot's state.total_shares still holds the old value.
   On next DCA or bracket replacement it tries to SELL shares it no longer has.
   FIX: poll open order status each tick. If tp_order_id is no longer open,
   treat it as filled → log profit → break out of position loop cleanly.

═══════════════════════════════════════════════════════════════════════════════
ENTRY ARMING
═══════════════════════════════════════════════════════════════════════════════
Both UP and DOWN prices must dip below ENTRY_PRICE at least once before a
trigger is armed. Prevents false entries when the window opens with prices
already above the target.

═══════════════════════════════════════════════════════════════════════════════
STOP LOSS MODES
═══════════════════════════════════════════════════════════════════════════════
Fixed:      STOP_LOSS=0.55   STOP_LOSS_OFFSET=null
            SL bracket order placed at 0.55 always.

Dynamic:    STOP_LOSS=null   STOP_LOSS_OFFSET=0.05
            SL = avg_entry_price - 0.05
            Recalculates and replaces SL bracket after every DCA fill.

Break-even: STOP_LOSS=null   STOP_LOSS_OFFSET=null
            SL = avg_entry_price - 1 tick (zero-loss guaranteed)
            Updates after every DCA fill.

═══════════════════════════════════════════════════════════════════════════════
.env variables
═══════════════════════════════════════════════════════════════════════════════
SOL_ENTRY_PRICE, SOL_AMOUNT_PER_BET, SOL_TAKE_PROFIT
SOL_STOP_LOSS          fixed price | null → break-even mode
SOL_STOP_LOSS_OFFSET   dynamic offset | null
SOL_BET_STEP           null | float — DCA step
SOL_POLL_INTERVAL      seconds between ticks

INTERVAL (runtime menu/argument):
  5m    | 15m | 1h    → timestamp-slug markets (e.g. sol-updown-1h-1740560400)
  1h_et              → ET-dated hourly  (e.g. solana-up-or-down-february-26-10am-et)
  24h                → daily market     (e.g. solana-up-or-down-on-february-26)

BUY_ORDER_TYPE=FAK|FOK|GTC
SELL_ORDER_TYPE=GTC
GTC_TIMEOUT_SECONDS=null|60
FOK_GTC_FALLBACK=true
AUTOSET_UP_TP_SL_ORDERS=true|false
  true  (default) → place GTC TP+SL bracket orders right after each BUY
  false           → monitor price manually; no bracket orders placed
"""

import os
import sys
import time
import logging
import requests
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# ── Path resolution ────────────────────────────────────────────────────────────
def _find_root(marker: str) -> Path:
    p = Path(__file__).resolve().parent
    for _ in range(12):
        if (p / marker).exists():
            return p
        p = p.parent
    raise FileNotFoundError(f"Cannot find '{marker}' walking up from {__file__}")

_ROOT = _find_root("order_executor.py")
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from order_executor import OrderExecutor
from market_stream  import MarketStream

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s][%(levelname)s] - %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("SOL-DCA")


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

def _float_env(key: str, default: float) -> float:
    v = os.getenv(key, "").strip().lower()
    try:
        return float(v) if v not in ("", "null", "none") else default
    except ValueError:
        return default

def _optional_float(key: str) -> Optional[float]:
    v = os.getenv(key, "").strip().lower()
    if v in ("", "null", "none"):
        return None
    try:
        return float(v)
    except ValueError:
        return None

ENTRY_PRICE      = _float_env("SOL_ENTRY_PRICE",    0.70)
AMOUNT_PER_BET   = _float_env("SOL_AMOUNT_PER_BET", 1.0)
TAKE_PROFIT      = _float_env("SOL_TAKE_PROFIT",    0.95)
POLL_INTERVAL    = _float_env("SOL_POLL_INTERVAL",  0.5)
BET_STEP         = _optional_float("SOL_BET_STEP")
STOP_LOSS        = _optional_float("SOL_STOP_LOSS")
STOP_LOSS_OFFSET = _optional_float("SOL_STOP_LOSS_OFFSET")

SL_BREAKEVEN_MODE = (STOP_LOSS is None) and (STOP_LOSS_OFFSET is None)

BUY_ORDER_TYPE  = (os.getenv("BUY_ORDER_TYPE")  or "FAK").upper()
SELL_ORDER_TYPE = (os.getenv("SELL_ORDER_TYPE") or "GTC").upper()

# When true: place GTC TP+SL bracket orders immediately after every BUY fill.
# When false: monitor price manually via the existing fallback logic.
# Requires approve_ctf.py to have been run at least once when true.
AUTOSET_UP_TP_SL_ORDERS = os.getenv("AUTOSET_UP_TP_SL_ORDERS", "true").lower() not in ("false", "0", "no")

_gtc_raw = os.getenv("GTC_TIMEOUT_SECONDS", "null").strip().lower()
GTC_TIMEOUT: Optional[int] = None if _gtc_raw == "null" else int(_gtc_raw)

WSS_READY_TIMEOUT = _float_env("WSS_READY_TIMEOUT", 10.0)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CHAIN_ID  = 137

SLUG_TEMPLATES = {
    "5m":  "sol-updown-5m-{ts}",
    "15m": "sol-updown-15m-{ts}",
    "1h":  "sol-updown-1h-{ts}",
}
WINDOW_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "1h_et": 3600, "24h": 86400}

# ── ET-dated slug helpers ──────────────────────────────────────────────────────
_COIN_PREFIX = "solana"
_MONTH_NAMES = [
    "", "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

def _et_now() -> datetime:
    """Current datetime in US Eastern Time (DST-aware if zoneinfo available)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except ImportError:
        return datetime.now(timezone(timedelta(hours=-5)))

def _fmt_slug_1h_et(dt: datetime) -> str:
    """e.g. solana-up-or-down-february-26-10am-et"""
    h12 = dt.hour % 12 or 12
    ap  = "am" if dt.hour < 12 else "pm"
    return f"{_COIN_PREFIX}-up-or-down-{_MONTH_NAMES[dt.month]}-{dt.day}-{h12}{ap}-et"

def _fmt_slug_24h(dt: datetime) -> str:
    """e.g. solana-up-or-down-on-february-26"""
    return f"{_COIN_PREFIX}-up-or-down-on-{_MONTH_NAMES[dt.month]}-{dt.day}"


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
    log.info(f"Searching for active SOL {interval.upper()} market ...")
    while True:
        if interval in ("5m", "15m", "1h"):
            window = WINDOW_SECONDS[interval]
            ts     = get_current_window_timestamp(interval)
            slugs  = [
                SLUG_TEMPLATES[interval].format(ts=ts),
                SLUG_TEMPLATES[interval].format(ts=ts + window),
            ]
        elif interval == "1h_et":
            now   = _et_now()
            slugs = [_fmt_slug_1h_et(now), _fmt_slug_1h_et(now + timedelta(hours=1))]
        else:  # "24h"
            now   = _et_now()
            slugs = [_fmt_slug_24h(now), _fmt_slug_24h(now + timedelta(days=1))]

        for slug in slugs:
            market = fetch_market(slug)
            if market and market.get("active") and not market.get("closed"):
                log.info(f"Found market: {slug}")
                log.info(f"  End date : {market.get('endDate') or market.get('end_date_iso')}")
                return market
        log.info("No active market — retrying in 15s ...")
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
#  REAL BALANCE QUERY
# ══════════════════════════════════════════════════════════════════════════════

def get_token_balance(client, token_id: str) -> Optional[float]:
    """
    Query the actual on-chain token balance from Polymarket data-api positions endpoint.
    Returns real shares held, or None if the query fails.

    Uses data-api.polymarket.com (not clob.polymarket.com/data/positions which returns 404).
    Filters the response list by the `asset` field matching token_id.
    `size` in the response is the actual shares currently held.

    NOTE: Uses FUNDER address (not EOA). With SignatureType=2, shares are
    held by the FUNDER account on-chain, not the signing EOA.
    """
    try:
        funder = getattr(client, "funder", None) or os.getenv("FUNDER_ADDRESS", "")
        resp = requests.get(
            f"{DATA_API}/positions",
            params  = {"user": funder, "sizeThreshold": ".01", "limit": 200},
            timeout = 5,
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            for pos in data:
                if str(pos.get("asset", "")) == str(token_id):
                    raw = pos.get("size", 0)
                    return float(Decimal(str(raw)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
        # token not found → no open position, treat as 0
        return 0.0
    except Exception as exc:
        log.warning(f"[balance] Failed to fetch position for token {token_id[:12]}...: {exc}")
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER STATUS CHECK
# ══════════════════════════════════════════════════════════════════════════════

def is_order_open(client, order_id: str) -> bool:
    """
    Returns True if the GTC order is still open/resting in the book.
    Returns False if it was filled, cancelled, or not found.

    Used to detect when a TP or SL bracket order was silently filled
    while the bot was in its polling loop.
    """
    try:
        resp = client.get_order(order_id)
        if not resp or not isinstance(resp, dict):
            return False
        status = resp.get("status", "").upper()
        # OPEN, LIVE, UNMATCHED = still in book
        return status in ("OPEN", "LIVE", "UNMATCHED", "PENDING")
    except Exception:
        return False


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
#  SAFE SHARES PARSER  — the fix for "Not Enough Allowance"
# ══════════════════════════════════════════════════════════════════════════════

def _parse_bet_result(resp: dict, fallback_price: float, fallback_usdc: float):
    """
    Extract (shares, cost) from order response.

    FIX: When takingAmount is missing or zero (common with FAK orders that don't
    echo fill amounts), the fallback estimate now uses ROUND_DOWN to 4 decimal
    places — ensuring we never claim MORE shares than the CTF contract credited.

    Without this fix: 1.00/0.60 = 1.666... → bot records 1.6667 shares
    CTF contract credits: 1.6666 shares (4dp truncated)
    SELL order for 1.6667 shares → "Not Enough Allowance" ✗

    With this fix: fallback = floor(1.00/0.60, 4dp) = 1.6666 shares
    SELL order for ≤ 1.6666 shares → OK ✔
    """
    try:
        shares = float(resp.get("takingAmount", 0))
        usdc   = float(resp.get("makingAmount", 0))

        if shares > 0:
            # API returned actual fill amount — still truncate to 4dp for safety
            shares = float(Decimal(str(shares)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
        else:
            # Fallback estimate — use ROUND_DOWN to avoid overestimating
            shares = float(
                (Decimal(str(fallback_usdc)) / Decimal(str(fallback_price)))
                .quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            )

        usdc = usdc if usdc > 0 else fallback_usdc
        return shares, usdc
    except Exception:
        safe_shares = float(
            (Decimal(str(fallback_usdc)) / Decimal(str(fallback_price)))
            .quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        )
        return safe_shares, fallback_usdc


# ══════════════════════════════════════════════════════════════════════════════
#  BOT STATE
# ══════════════════════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.side                : Optional[str]   = None
        self.token_id            : Optional[str]   = None   # LOCKED after first BUY
        self.entry_price         : float           = 0.0
        self.last_bet_price      : float           = 0.0
        self.avg_price           : float           = 0.0
        self.total_shares        : float           = 0.0
        self.total_spent         : float           = 0.0
        self.effective_stop_loss : Optional[float] = None
        self.bets_count          : int             = 0
        self.in_position         : bool            = False
        self.tp_order_id         : Optional[str]   = None
        self.sl_order_id         : Optional[str]   = None
        self.entry_armed         : bool            = False
        # Tick counter for order status polling (check every N ticks, not every tick)
        self._ticks_since_status_check : int = 0

    def update_after_bet(self, bet_price: float, usdc_paid: float, shares: float):
        self.total_shares += shares
        self.total_spent  += usdc_paid
        self.avg_price     = self.total_spent / self.total_shares if self.total_shares else bet_price

        if STOP_LOSS_OFFSET is not None:
            self.effective_stop_loss = round(self.avg_price - STOP_LOSS_OFFSET, 4)
        elif STOP_LOSS is not None:
            self.effective_stop_loss = STOP_LOSS
        else:
            # Break-even: SL = avg_price - 1 tick (prevents self-trigger)
            self.effective_stop_loss = round(self.avg_price - 0.01, 4)

        self.last_bet_price = bet_price
        self.bets_count    += 1
        self.in_position    = True

    def summary(self) -> str:
        sl_mode = "(dynamic)" if STOP_LOSS_OFFSET else "(break-even)" if SL_BREAKEVEN_MODE else "(fixed)"
        sl_val  = f"{self.effective_stop_loss:.4f}{sl_mode}" if self.effective_stop_loss else "none"
        mode    = f"DCA STEP={BET_STEP}" if BET_STEP else "Single bet"
        return (
            f"  Side={self.side}  Bets={self.bets_count}  Shares={self.total_shares:.4f}"
            f"  Spent=${self.total_spent:.2f}  AvgP={self.avg_price:.4f}\n"
            f"  SL={sl_val}  TP={TAKE_PROFIT}  [{mode}]\n"
            f"  tp_id={self.tp_order_id or 'none'}  sl_id={self.sl_order_id or 'none'}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  BRACKET ORDERS
# ══════════════════════════════════════════════════════════════════════════════

def place_brackets(executor: OrderExecutor, state: BotState, tick_size: float,
                   client=None):
    """
    Cancel existing brackets then place fresh TP + SL GTC orders.

    Before placing, optionally queries real balance to prevent "Not Enough
    Allowance". Uses the lower of state.total_shares vs actual wallet balance.
    """
    # Cancel old bracket orders
    for oid in [state.tp_order_id, state.sl_order_id]:
        if oid:
            try:
                executor.gtc_tracker.cancel(oid, log)
            except Exception:
                pass
    state.tp_order_id = None
    state.sl_order_id = None

    # ── Verify shares against real balance ────────────────────────────────────
    shares_to_sell = state.total_shares
    if client is not None:
        real_balance = get_token_balance(client, state.token_id)
        if real_balance is not None:
            if real_balance < shares_to_sell:
                log.warning(
                    f"  [bracket] Real balance {real_balance:.4f} < state {shares_to_sell:.4f} "
                    f"— using real balance to avoid allowance error"
                )
                shares_to_sell = real_balance
            else:
                log.info(f"  [bracket] Balance confirmed: {real_balance:.4f} shares ✔")

    if shares_to_sell < 0.0001:
        log.warning("  [bracket] Shares too small to place SELL orders — skipping")
        return

    sl_disp = f"{state.effective_stop_loss:.4f}" if state.effective_stop_loss else "none"
    sl_mode = " (break-even)" if SL_BREAKEVEN_MODE else ""
    log.info(f"  Placing brackets: TP={TAKE_PROFIT}  SL={sl_disp}{sl_mode}  shares={shares_to_sell:.4f}")

    result = executor.place_sell_bracket(
        token_id     = state.token_id,
        total_shares = shares_to_sell,
        tp_price     = TAKE_PROFIT,
        sl_price     = state.effective_stop_loss,
        tick_size    = tick_size,
    )
    state.tp_order_id = result.get("tp_order_id")
    state.sl_order_id = result.get("sl_order_id")

    if not state.tp_order_id:
        log.warning("  TP bracket failed — will monitor price manually")
    if state.effective_stop_loss and not state.sl_order_id:
        log.warning("  SL bracket failed — will monitor price manually")


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_window(market: dict, executor: OrderExecutor, state: BotState, interval: str):
    tokens     = parse_market_tokens(market)
    end_time   = get_market_end_time(market)
    token_up   = tokens["UP"]["token_id"]
    token_down = tokens["DOWN"]["token_id"]
    tick_up    = get_tick_size_rest(executor.client, token_up)
    tick_down  = get_tick_size_rest(executor.client, token_down)
    client     = executor.client

    sl_cfg = (
        f"SL_OFFSET={STOP_LOSS_OFFSET}(dynamic)" if STOP_LOSS_OFFSET
        else "SL=avg_price(break-even)" if SL_BREAKEVEN_MODE
        else f"SL={STOP_LOSS}(fixed)"
    )
    mode_str = f"DCA every {BET_STEP} pts" if BET_STEP else "Single bet"

    log.info("=" * 60)
    log.info(f"  SOL DCA | Market: {market.get('id','')}")
    log.info(f"  Interval   : {interval.upper()}")
    log.info(f"  End time   : {end_time}")
    log.info(f"  ENTRY={ENTRY_PRICE}  BET=${AMOUNT_PER_BET}  TP={TAKE_PROFIT}")
    log.info(f"  {sl_cfg}  {mode_str}")
    log.info(f"  BUY={BUY_ORDER_TYPE}  SELL={SELL_ORDER_TYPE}  AUTO_BRACKETS={'ON' if AUTOSET_UP_TP_SL_ORDERS else 'OFF'}")
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
        log.warning(f"[WSS] Not ready after {WSS_READY_TIMEOUT}s — using REST fallback")

    try:
        while True:
            # ── Window expiry ──────────────────────────────────────────────────
            now = datetime.now(timezone.utc)
            if end_time and now >= end_time:
                log.info("Window closed — cancelling all open bracket orders.")
                executor.gtc_tracker.cancel_all(log)
                break

            time_left  = (end_time - now).total_seconds() if end_time else 999
            _tl        = int(time_left)
            _hrs       = _tl // 3600
            _min       = (_tl % 3600) // 60
            _sec       = _tl % 60
            time_label = f"{_hrs}h{_min:02d}m" if _hrs > 0 else f"{_min:02d}:{_sec:02d}"

            # ── Sync tick sizes ────────────────────────────────────────────────
            tick_up   = stream.get_tick_size(token_up)   or tick_up
            tick_down = stream.get_tick_size(token_down) or tick_down

            # ── Price read ─────────────────────────────────────────────────────
            prices = get_prices(stream, token_up, token_down)
            if prices is None:
                log.warning("Price fetch failed — skipping tick")
                time.sleep(POLL_INTERVAL)
                continue

            up_price   = prices["UP"]
            down_price = prices["DOWN"]
            src        = "WSS" if stream.is_connected else "REST"

            # ══════════════════════════════════════════════════════════════════
            #  PHASE 1 — Waiting for entry
            # ══════════════════════════════════════════════════════════════════
            if not state.in_position:

                # ── Entry arming ───────────────────────────────────────────────
                if not state.entry_armed:
                    if up_price < ENTRY_PRICE and down_price < ENTRY_PRICE:
                        state.entry_armed = True
                        log.info(
                            f"  Entry armed — prices dipped below {ENTRY_PRICE} "
                            f"(UP={up_price:.4f} DOWN={down_price:.4f})"
                        )
                    else:
                        log.info(
                            f"[{time_label}]  UP={up_price:.4f}  DOWN={down_price:.4f}"
                            f"  | Waiting to arm at ENTRY={ENTRY_PRICE}  {src}"
                        )
                        time.sleep(POLL_INTERVAL)
                        continue

                # ── Entry trigger ──────────────────────────────────────────────
                trig_side  = None
                trig_price = None
                trig_tick  = 0.01

                if up_price >= ENTRY_PRICE:
                    trig_side, trig_price, trig_tick = "UP",   up_price,   tick_up
                elif down_price >= ENTRY_PRICE:
                    trig_side, trig_price, trig_tick = "DOWN", down_price, tick_down

                if trig_side:
                    log.info(
                        f"*** ENTRY: {trig_side} @ {trig_price:.4f} >= {ENTRY_PRICE} ***"
                    )
                    # Lock token_id at BUY time — never change it after this point
                    state.side        = trig_side
                    state.token_id    = token_up if trig_side == "UP" else token_down
                    state.entry_price = trig_price

                    resp = executor.place_buy(
                        token_id  = state.token_id,
                        price     = trig_price,
                        usdc_size = AMOUNT_PER_BET,
                        tick_size = trig_tick,
                    )

                    if resp and resp.get("success"):
                        shares, usdc_paid = _parse_bet_result(resp, trig_price, AMOUNT_PER_BET)
                        log.info(
                            f"  BET #1 filled | shares={shares:.4f}  "
                            f"usdc=${usdc_paid:.4f}  token={state.token_id[:16]}..."
                        )
                        state.update_after_bet(trig_price, usdc_paid, shares)
                        if AUTOSET_UP_TP_SL_ORDERS:
                            place_brackets(executor, state, trig_tick, client=client)
                        else:
                            log.info("  AUTOSET_UP_TP_SL_ORDERS=false — brackets skipped, monitoring manually")
                        log.info(state.summary())
                    else:
                        log.warning(f"  BET #1 failed (no fill) — resp={resp} — continuing")
                        state.reset()
                else:
                    log.info(
                        f"[{time_label}]  UP={up_price:.4f}  DOWN={down_price:.4f}"
                        f"  | Armed, waiting for ENTRY={ENTRY_PRICE}  {src}"
                    )

            # ══════════════════════════════════════════════════════════════════
            #  PHASE 2 — In position
            # ══════════════════════════════════════════════════════════════════
            else:
                cp        = up_price if state.side == "UP" else down_price
                tick_size = tick_up  if state.side == "UP" else tick_down

                log.info(
                    f"[{time_label}]  {state.side}={cp:.4f}"
                    f"  AvgP={state.avg_price:.4f}"
                    f"  SL={state.effective_stop_loss:.4f}"
                    f"  TP={TAKE_PROFIT}"
                    f"  Shares={state.total_shares:.4f}"
                    f"  {src}"
                )

                # ── Check if bracket orders were silently filled ───────────────
                # Poll every 6 ticks (~3s at default 0.5s interval) to avoid
                # hammering the API on every tick.
                state._ticks_since_status_check += 1
                if state._ticks_since_status_check >= 6:
                    state._ticks_since_status_check = 0

                    # TP filled externally?
                    if state.tp_order_id and not is_order_open(client, state.tp_order_id):
                        pnl = (TAKE_PROFIT - state.avg_price) * state.total_shares
                        log.info(
                            f"*** TAKE PROFIT FILLED (detected via order status) ***\n"
                            f"  TP={TAKE_PROFIT}  AvgP={state.avg_price:.4f}"
                            f"  Shares={state.total_shares:.4f}"
                            f"  Est. P&L=+${pnl:.4f}"
                        )
                        # Cancel the orphaned SL order
                        executor.gtc_tracker.cancel_all(log)
                        log.info("  Resetting state — looking for next entry in this window ...")
                        state.reset()
                        continue

                    # SL filled externally?
                    if state.sl_order_id and not is_order_open(client, state.sl_order_id):
                        pnl = (state.effective_stop_loss - state.avg_price) * state.total_shares
                        log.info(
                            f"*** STOP LOSS FILLED (detected via order status) ***\n"
                            f"  SL={state.effective_stop_loss:.4f}  AvgP={state.avg_price:.4f}"
                            f"  Shares={state.total_shares:.4f}"
                            f"  Est. P&L=${pnl:.4f}"
                        )
                        executor.gtc_tracker.cancel_all(log)
                        log.info("  Resetting state — looking for next entry in this window ...")
                        state.reset()
                        continue

                # ── Manual fallback: TP ────────────────────────────────────────
                if not state.tp_order_id and cp >= TAKE_PROFIT:
                    log.info(f"*** TP FALLBACK: {state.side}={cp:.4f} >= {TAKE_PROFIT} — selling ***")
                    executor.gtc_tracker.cancel_all(log)
                    real_bal = get_token_balance(client, state.token_id)
                    sell_shares = real_bal if real_bal is not None else state.total_shares
                    resp = executor.place_sell_immediate(
                        token_id      = state.token_id,
                        total_shares  = sell_shares,
                        current_price = cp,
                        tick_size     = tick_size,
                    )
                    if resp:
                        pnl = (cp - state.avg_price) * sell_shares
                        log.info(f"  CLOSED (TP fallback) | Est. P&L=+${pnl:.4f}")
                        log.info("  Resetting state — looking for next entry in this window ...")
                        state.reset()
                        continue
                    time.sleep(POLL_INTERVAL)
                    continue

                # ── Manual fallback: SL ────────────────────────────────────────
                if (
                    not state.sl_order_id
                    and state.effective_stop_loss is not None
                    and cp <= state.effective_stop_loss
                ):
                    sl_label = (
                        "(break-even)" if SL_BREAKEVEN_MODE
                        else "(dynamic)"  if STOP_LOSS_OFFSET
                        else "(fixed)"
                    )
                    log.info(
                        f"*** SL FALLBACK {sl_label}: {state.side}={cp:.4f} "
                        f"<= {state.effective_stop_loss:.4f} — selling ***"
                    )
                    executor.gtc_tracker.cancel_all(log)
                    real_bal = get_token_balance(client, state.token_id)
                    sell_shares = real_bal if real_bal is not None else state.total_shares
                    resp = executor.place_sell_immediate(
                        token_id      = state.token_id,
                        total_shares  = sell_shares,
                        current_price = cp,
                        tick_size     = tick_size,
                    )
                    if resp:
                        pnl = (cp - state.avg_price) * sell_shares
                        log.info(f"  CLOSED (SL fallback) | Est. P&L=${pnl:.4f}")
                        log.info("  Resetting state — looking for next entry in this window ...")
                        state.reset()
                        continue
                    time.sleep(POLL_INTERVAL)
                    continue

                # ── DCA ────────────────────────────────────────────────────────
                if BET_STEP is not None:
                    next_bet = round(state.last_bet_price + BET_STEP, 4)
                    if cp >= next_bet:
                        log.info(
                            f"*** DCA #{state.bets_count + 1}: {state.side}={cp:.4f}"
                            f" >= {next_bet:.4f} ***"
                        )
                        resp = executor.place_buy(
                            token_id  = state.token_id,  # SAME token as initial buy
                            price     = cp,
                            usdc_size = AMOUNT_PER_BET,
                            tick_size = tick_size,
                        )
                        if resp and resp.get("success"):
                            shares, usdc_paid = _parse_bet_result(resp, cp, AMOUNT_PER_BET)
                            log.info(f"  DCA filled | shares={shares:.4f}  usdc=${usdc_paid:.4f}")
                            state.update_after_bet(cp, usdc_paid, shares)
                            # Replace brackets with updated total + new SL
                            if AUTOSET_UP_TP_SL_ORDERS:
                                place_brackets(executor, state, tick_size, client=client)
                            else:
                                log.info("  AUTOSET_UP_TP_SL_ORDERS=false — brackets skipped, monitoring manually")
                            log.info(state.summary())
                        else:
                            log.warning(f"  DCA failed (no fill) — resp={resp} — continuing")

            time.sleep(POLL_INTERVAL)

    finally:
        log.info("[WSS] Closing market channel.")
        stream.stop()

    log.info("Window loop ended.")


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run(interval: Optional[str] = None):
    if interval is None:
        try:
            import questionary
            choice = questionary.select(
                "Select market interval:",
                choices=["5 minutes", "15 minutes", "1 hour", "1 hour (ET dated)", "24 hours"],
            ).ask()
            if choice is None:
                sys.exit(0)
            interval = {
                "5 minutes":         "5m",
                "15 minutes":        "15m",
                "1 hour":            "1h",
                "1 hour (ET dated)": "1h_et",
                "24 hours":          "24h",
            }[choice]
        except (ImportError, Exception):
            while True:
                c = input("Market interval — enter 5, 15, 60, 1h_et, or 24h: ").strip().lower()
                if c == "5":     interval = "5m";    break
                if c == "15":    interval = "15m";   break
                if c == "60":    interval = "1h";    break
                if c == "1h_et": interval = "1h_et"; break
                if c == "24h":   interval = "24h";   break

    sl_cfg = (
        f"SL_OFFSET={STOP_LOSS_OFFSET}(dynamic)" if STOP_LOSS_OFFSET
        else "SL=avg_price(break-even)" if SL_BREAKEVEN_MODE
        else f"SL={STOP_LOSS}(fixed)"
    )

    log.info("=" * 60)
    log.info("SOL DCA Snipe starting")
    log.info(f"  Interval : {interval.upper()}")
    log.info(f"  ENTRY={ENTRY_PRICE}  BET=${AMOUNT_PER_BET}  TP={TAKE_PROFIT}")
    log.info(f"  {sl_cfg}  BET_STEP={BET_STEP}")
    log.info(f"  BUY={BUY_ORDER_TYPE}  SELL={SELL_ORDER_TYPE}  AUTO_BRACKETS={'ON' if AUTOSET_UP_TP_SL_ORDERS else 'OFF'}")
    log.info("=" * 60)

    client   = build_clob_client()
    executor = OrderExecutor(client=client, log=log)
    log.info("CLOB client authenticated OK")

    while True:
        state  = BotState()
        market = wait_for_active_market(interval)
        run_window(market, executor, state, interval)

        end_time  = get_market_end_time(market)
        wait_secs = 30
        if end_time:
            remaining = (end_time - datetime.now(timezone.utc)).total_seconds()
            wait_secs = max(5, remaining + 5)
        log.info(f"Waiting {wait_secs:.0f}s for next window ...")
        time.sleep(wait_secs)


if __name__ == "__main__":
    run()