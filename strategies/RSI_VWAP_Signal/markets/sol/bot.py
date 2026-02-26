"""
strategies/DCA_Snipe/markets/sol/bot.py
---------------------------------------
SOL Up/Down Bot for Polymarket — DCA Snipe strategy.

MARKET DISCOVERY
  5m/15m: slug = "sol-updown-{interval}-{unix_ts}"
          GET gamma-api.polymarket.com/markets?slug=...

  1h:     event slug = "solana-up-or-down-{month}-{day}-{hour}{ampm}-et"
          e.g. "solana-up-or-down-february-25-12pm-et"
          GET gamma-api.polymarket.com/events?slug=...  → extract active child market
          Tries current hour ±1 to handle clock drift.

POSITION VERIFICATION  (Not Enough Allowance fix)
  Before every SELL, queries:
    GET data-api.polymarket.com/positions
        ?user={FUNDER_ADDRESS}&sizeThreshold=0.01&market={conditionId}&limit=200
  Returns real shares held. Falls back to CLOB /balance-allowance on failure.
  Using real shares prevents selling more than we actually own.

AUTOSET_UP_TP_SL_ORDERS
  true  (default) → place GTC TP+SL brackets immediately after each BUY
  false           → no bracket orders; TP/SL monitored manually via price ticks
                    Use this when approve_ctf.py hasn't been run yet.

STOP LOSS MODES
  Fixed:      SOL_STOP_LOSS=0.55   SOL_STOP_LOSS_OFFSET=null
  Dynamic:    SOL_STOP_LOSS=null   SOL_STOP_LOSS_OFFSET=0.02  (SL = avg - 0.02)
  Break-even: SOL_STOP_LOSS=null   SOL_STOP_LOSS_OFFSET=null  (SL = avg - 1 tick)

.env variables
  SOL_ENTRY_PRICE, SOL_AMOUNT_PER_BET, SOL_TAKE_PROFIT
  SOL_STOP_LOSS, SOL_STOP_LOSS_OFFSET, SOL_BET_STEP, SOL_POLL_INTERVAL
  AUTOSET_UP_TP_SL_ORDERS=true|false
  BUY_ORDER_TYPE, SELL_ORDER_TYPE, GTC_TIMEOUT_SECONDS, FOK_GTC_FALLBACK
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
    raise FileNotFoundError(f"Project root not found (marker: {marker})")

_ROOT = _find_root("order_executor.py")
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from order_executor import OrderExecutor
from market_stream  import MarketStream

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
    return None if v in ("", "null", "none") else (float(v) if v else None)

def _bool_env(key: str, default: bool) -> bool:
    v = os.getenv(key, "").strip().lower()
    if v in ("", "null", "none"):
        return default
    return v not in ("false", "0", "no", "off")

ENTRY_PRICE      = _float_env("SOL_ENTRY_PRICE",    0.60)
AMOUNT_PER_BET   = _float_env("SOL_AMOUNT_PER_BET", 5.0)
TAKE_PROFIT      = _float_env("SOL_TAKE_PROFIT",    0.85)
POLL_INTERVAL    = _float_env("SOL_POLL_INTERVAL",  0.1)
BET_STEP         = _optional_float("SOL_BET_STEP")
STOP_LOSS        = _optional_float("SOL_STOP_LOSS")
STOP_LOSS_OFFSET = _optional_float("SOL_STOP_LOSS_OFFSET")

SL_BREAKEVEN_MODE = (STOP_LOSS is None) and (STOP_LOSS_OFFSET is None)

# Master switch: set false to skip bracket orders entirely (manual TP/SL only)
AUTOSET_BRACKETS = _bool_env("AUTOSET_UP_TP_SL_ORDERS", True)

BUY_ORDER_TYPE  = (os.getenv("BUY_ORDER_TYPE")  or "FAK").upper()
SELL_ORDER_TYPE = (os.getenv("SELL_ORDER_TYPE") or "FAK").upper()

_gtc_raw    = os.getenv("GTC_TIMEOUT_SECONDS", "null").strip().lower()
GTC_TIMEOUT : Optional[int] = None if _gtc_raw == "null" else int(_gtc_raw)

WSS_READY_TIMEOUT = _float_env("WSS_READY_TIMEOUT", 10.0)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CHAIN_ID  = 137

SLUG_TEMPLATES = {
    "5m":  "sol-updown-5m-{ts}",
    "15m": "sol-updown-15m-{ts}",
}
WINDOW_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


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

def _et_offset(month: int) -> int:
    """Return UTC→ET offset in hours. EST=-5 (Nov–Mar), EDT=-4 (Mar–Nov)."""
    return -4 if 3 <= month <= 11 else -5


def _build_1h_event_slug(dt_utc: datetime) -> str:
    """
    Convert a UTC datetime to the Polymarket 1h event slug.

    2025-02-25 17:00 UTC  →  "solana-up-or-down-february-25-12pm-et"
    """
    dt_et = (dt_utc + timedelta(hours=_et_offset(dt_utc.month))).replace(
        minute=0, second=0, microsecond=0
    )
    month_str = dt_et.strftime("%B").lower()
    hour      = dt_et.hour
    if   hour == 0:  h_str = "12am"
    elif hour < 12:  h_str = f"{hour}am"
    elif hour == 12: h_str = "12pm"
    else:            h_str = f"{hour - 12}pm"
    return f"solana-up-or-down-{month_str}-{dt_et.day}-{h_str}-et"


def _gamma_get(endpoint: str, params: dict) -> Optional[object]:
    try:
        r = requests.get(f"{GAMMA_API}/{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning(f"Gamma {endpoint}: {exc}")
        return None


def _find_market_5m_15m(interval: str) -> Optional[dict]:
    window = WINDOW_SECONDS[interval]
    ts     = (int(datetime.now(timezone.utc).timestamp()) // window) * window
    for candidate in [ts, ts + window]:
        slug = SLUG_TEMPLATES[interval].format(ts=candidate)
        data = _gamma_get("markets", {"slug": slug})
        if not data:
            continue
        for m in (data if isinstance(data, list) else [data]):
            if m.get("active") and not m.get("closed"):
                log.info(f"  Found market slug: {slug}")
                return m
    return None


def _find_market_1h() -> Optional[dict]:
    """
    Search for an active 1h SOL market.
    Tries current hour, +1h, and -1h to handle edge cases.
    Searches via /events?slug= first, then /markets?event_slug= fallback.
    """
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for h_delta in [0, 1, -1]:
        dt_try     = now + timedelta(hours=h_delta)
        event_slug = _build_1h_event_slug(dt_try)
        log.info(f"  Trying 1h slug: {event_slug}")

        # Primary: /events endpoint
        data = _gamma_get("events", {"slug": event_slug})
        if data:
            for event in (data if isinstance(data, list) else [data]):
                for m in (event.get("markets") or []):
                    if m.get("active") and not m.get("closed"):
                        log.info(f"  Found via /events: {event_slug}")
                        return m

        # Fallback: /markets?event_slug=
        data2 = _gamma_get("markets", {"event_slug": event_slug})
        if data2:
            for m in (data2 if isinstance(data2, list) else [data2]):
                if m.get("active") and not m.get("closed"):
                    log.info(f"  Found via /markets event_slug: {event_slug}")
                    return m

    return None


def wait_for_active_market(interval: str) -> dict:
    log.info(f"Searching for active SOL {interval.upper()} market ...")
    while True:
        m = _find_market_1h() if interval == "1h" else _find_market_5m_15m(interval)
        if m:
            log.info(f"  Market ID  : {m.get('id', '?')}")
            log.info(f"  End time   : {m.get('endDate') or m.get('end_date_iso', '?')}")
            return m
        log.info("  No active market — retrying in 20s ...")
        time.sleep(20)


def parse_market_tokens(market: dict) -> dict:
    import json as _j
    outcomes = market.get("outcomes", "[]")
    prices   = market.get("outcomePrices", "[0.5,0.5]")
    tokens   = market.get("clobTokenIds") or market.get("clob_token_ids", "[]")
    outcomes = _j.loads(outcomes) if isinstance(outcomes, str) else outcomes
    prices   = [float(p) for p in (_j.loads(prices) if isinstance(prices, str) else prices)]
    tokens   = _j.loads(tokens)   if isinstance(tokens, str) else tokens
    result   = {}
    for i, name in enumerate(outcomes):
        key = "UP" if name.lower() in ("up", "yes") else "DOWN"
        result[key] = {
            "token_id": tokens[i] if i < len(tokens) else None,
            "price":    prices[i] if i < len(prices) else 0.5,
        }
    return result


def get_market_end_time(market: dict) -> Optional[datetime]:
    for f in ("endDate", "end_date_iso", "closedTime"):
        val = market.get(f)
        if val:
            try:
                return datetime.fromisoformat(
                    val.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except Exception:
                pass
    return None


def get_tick_size_rest(client, token_id: str) -> float:
    try:
        r = client.get_tick_size(token_id)
        return float(r) if r else 0.01
    except Exception:
        return 0.01


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION QUERY  (real shares — not an estimate)
# ══════════════════════════════════════════════════════════════════════════════

def get_real_position(token_id: str, condition_id: Optional[str] = None) -> Optional[float]:
    """
    Return real on-chain shares for token_id using the Data API.

    Primary:  GET data-api.polymarket.com/positions
                  ?user=FUNDER&sizeThreshold=0.01&market=conditionId&limit=200
    Fallback: same endpoint without market filter (scan all positions)
    Last:     GET clob.polymarket.com/balance-allowance
    """
    owner = os.getenv("FUNDER_ADDRESS", "")
    if not owner:
        return None

    def _parse_positions(positions: list) -> Optional[float]:
        for pos in positions:
            if str(pos.get("asset_id", "")) == str(token_id):
                raw = pos.get("size") or pos.get("balance") or 0
                return float(
                    Decimal(str(raw)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                )
        return None   # token not in list → 0 shares on-chain

    # ── Primary: filter by conditionId ────────────────────────────────────────
    if condition_id:
        try:
            r = requests.get(
                f"{DATA_API}/positions",
                params={"user": owner, "sizeThreshold": "0.01",
                        "market": condition_id, "limit": "200"},
                timeout=8,
            )
            r.raise_for_status()
            result = _parse_positions(r.json() if isinstance(r.json(), list) else [])
            if result is not None:
                return result
        except Exception as exc:
            log.warning(f"  [pos] Data API (market filter): {exc}")

    # ── Fallback: scan all positions ──────────────────────────────────────────
    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": owner, "sizeThreshold": "0.01", "limit": "200"},
            timeout=8,
        )
        r.raise_for_status()
        result = _parse_positions(r.json() if isinstance(r.json(), list) else [])
        if result is not None:
            return result
        log.info("  [pos] Token not found in all-positions scan — 0 shares")
        return 0.0
    except Exception as exc:
        log.warning(f"  [pos] Data API (all positions): {exc}")

    # ── Last resort: CLOB balance-allowance ───────────────────────────────────
    try:
        r = requests.get(
            f"{CLOB_HOST}/balance-allowance",
            params={"asset_type": "CONDITIONAL_TOKEN",
                    "token_id": token_id, "owner": owner},
            timeout=5,
        )
        r.raise_for_status()
        bal = float(r.json().get("balance", 0) or 0)
        return float(Decimal(str(bal)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
    except Exception as exc:
        log.warning(f"  [pos] CLOB balance-allowance: {exc}")
    return None


def check_ctf_allowance(token_id: str) -> bool:
    """
    Verify CTF Exchange has allowance to move tokens.
    Returns True=ok, False=need to run approve_ctf.py.
    """
    owner = os.getenv("FUNDER_ADDRESS", "")
    try:
        r = requests.get(
            f"{CLOB_HOST}/balance-allowance",
            params={"asset_type": "CONDITIONAL_TOKEN",
                    "token_id": token_id, "owner": owner},
            timeout=5,
        )
        r.raise_for_status()
        alw = float(r.json().get("allowance", 0) or 0)
        log.info(f"  [allowance] CTF Exchange allowance={alw:.4f}")
        if alw == 0:
            log.error(
                "CTF Exchange allowance=0 — SELL orders will fail. "
                "Fix: python approve_ctf.py  (run once per wallet)"
            )
            return False
        return True
    except Exception as exc:
        log.warning(f"  [allowance] Check failed: {exc} — assuming ok")
        return True


# ══════════════════════════════════════════════════════════════════════════════
#  ORDER STATUS
# ══════════════════════════════════════════════════════════════════════════════

def is_order_open(client, order_id: str) -> bool:
    try:
        resp   = client.get_order(order_id)
        status = (resp or {}).get("status", "").upper()
        return status in ("OPEN", "LIVE", "UNMATCHED", "PENDING")
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  PRICE FEED
# ══════════════════════════════════════════════════════════════════════════════

def _rest_mid(token_id: str) -> Optional[float]:
    try:
        r = requests.get(f"{CLOB_HOST}/midpoint",
                         params={"token_id": token_id}, timeout=5)
        r.raise_for_status()
        return float(r.json()["mid"])
    except Exception:
        return None


def get_prices(stream: MarketStream, tok_up: str, tok_dn: str) -> Optional[dict]:
    up = stream.get_midpoint(tok_up) or _rest_mid(tok_up)
    dn = stream.get_midpoint(tok_dn) or _rest_mid(tok_dn)
    return {"UP": up, "DOWN": dn} if (up and dn) else None


# ══════════════════════════════════════════════════════════════════════════════
#  SHARES PARSER
# ══════════════════════════════════════════════════════════════════════════════

def _parse_bet_result(resp: dict, fb_price: float, fb_usdc: float):
    """
    Extract (shares, usdc) from a BUY response.
    Uses ROUND_DOWN on fallback to never overestimate shares.
    """
    try:
        shares = float(resp.get("takingAmount", 0))
        usdc   = float(resp.get("makingAmount", 0))
        if shares > 0:
            shares = float(Decimal(str(shares)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN))
        else:
            shares = float(
                (Decimal(str(fb_usdc)) / Decimal(str(fb_price)))
                .quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
            )
        return shares, (usdc if usdc > 0 else fb_usdc)
    except Exception:
        return float(
            (Decimal(str(fb_usdc)) / Decimal(str(fb_price)))
            .quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        ), fb_usdc


# ══════════════════════════════════════════════════════════════════════════════
#  BOT STATE
# ══════════════════════════════════════════════════════════════════════════════

class BotState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.side                : Optional[str]   = None
        self.token_id            : Optional[str]   = None
        self.condition_id        : Optional[str]   = None   # for Data API position query
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
        self._tick_ctr           : int             = 0

    def update_after_bet(self, price: float, usdc: float, shares: float):
        self.total_shares += shares
        self.total_spent  += usdc
        self.avg_price     = self.total_spent / self.total_shares if self.total_shares else price
        if STOP_LOSS_OFFSET is not None:
            self.effective_stop_loss = round(self.avg_price - STOP_LOSS_OFFSET, 4)
        elif STOP_LOSS is not None:
            self.effective_stop_loss = STOP_LOSS
        else:
            self.effective_stop_loss = round(self.avg_price - 0.01, 4)
        self.last_bet_price = price
        self.bets_count    += 1
        self.in_position    = True

    def summary(self) -> str:
        sl_tag = "(dyn)" if STOP_LOSS_OFFSET else "(BE)" if SL_BREAKEVEN_MODE else "(fixed)"
        sl_val = f"{self.effective_stop_loss:.4f}{sl_tag}" if self.effective_stop_loss else "none"
        mode   = f"DCA STEP={BET_STEP}" if BET_STEP else "Single bet"
        auto   = "AUTOSET=ON" if AUTOSET_BRACKETS else "AUTOSET=OFF(manual)"
        return (
            f"  Side={self.side}  Bets={self.bets_count}  "
            f"Shares={self.total_shares:.4f}  Spent=${self.total_spent:.4f}  "
            f"AvgP={self.avg_price:.4f}\n"
            f"  SL={sl_val}  TP={TAKE_PROFIT}  [{mode}]  {auto}\n"
            f"  tp_id={self.tp_order_id or 'none'}  "
            f"sl_id={self.sl_order_id or 'none'}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  BRACKET ORDERS
# ══════════════════════════════════════════════════════════════════════════════

def place_brackets(executor: OrderExecutor, state: BotState, tick_size: float):
    """
    Cancel old brackets and place new TP+SL GTC orders.
    Only called when AUTOSET_BRACKETS=True.

    1. Cancel old orders
    2. Check CTF Exchange allowance
    3. Query real on-chain position (up to 5 retries, 1s each)
    4. Place brackets using real balance
    """
    if not AUTOSET_BRACKETS:
        log.info("  AUTOSET_UP_TP_SL_ORDERS=false — skipping brackets")
        return

    for oid in [state.tp_order_id, state.sl_order_id]:
        if oid:
            try:
                executor.gtc_tracker.cancel(oid, log)
            except Exception:
                pass
    state.tp_order_id = None
    state.sl_order_id = None

    if not check_ctf_allowance(state.token_id):
        log.error("  Aborting brackets — run approve_ctf.py first")
        return

    shares_to_sell = None
    for attempt in range(1, 6):
        real = get_real_position(state.token_id, state.condition_id)
        if real is not None and real >= 0.0001:
            if real < state.total_shares:
                log.warning(
                    f"  [brackets] on-chain={real:.4f} < estimate={state.total_shares:.4f}"
                    f" — using on-chain"
                )
            else:
                log.info(f"  [brackets] position confirmed: {real:.4f} shares ✔")
            shares_to_sell = real
            break
        log.info(
            f"  [brackets] position not settled ({real if real is not None else 'err'})"
            f" — waiting 1s ({attempt}/5)"
        )
        time.sleep(1)

    if shares_to_sell is None:
        log.warning("  [brackets] using estimate after 5 failed queries")
        shares_to_sell = state.total_shares

    if shares_to_sell < 0.0001:
        log.warning("  [brackets] shares too small — skipping")
        return

    sl_disp = f"{state.effective_stop_loss:.4f}" if state.effective_stop_loss else "none"
    sl_tag  = " (BE)" if SL_BREAKEVEN_MODE else ""
    log.info(
        f"  Placing brackets  TP={TAKE_PROFIT}  "
        f"SL={sl_disp}{sl_tag}  shares={shares_to_sell:.4f}"
    )
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
        log.warning("  TP bracket failed — monitoring manually")
    if state.effective_stop_loss and not state.sl_order_id:
        log.warning("  SL bracket failed — monitoring manually")


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

    # conditionId — used for precise Data API position queries
    condition_id = (
        market.get("conditionId")
        or market.get("condition_id")
        or market.get("questionID")
        or market.get("id")
    )

    sl_cfg   = (
        f"SL_OFFSET={STOP_LOSS_OFFSET}(dyn)" if STOP_LOSS_OFFSET
        else "SL=avg(BE)" if SL_BREAKEVEN_MODE
        else f"SL={STOP_LOSS}(fixed)"
    )
    auto_str = "AUTOSET=ON" if AUTOSET_BRACKETS else "AUTOSET=OFF(manual)"

    log.info("=" * 60)
    log.info(f"  SOL DCA  |  Interval={interval.upper()}")
    log.info(f"  Market   : {market.get('id', '?')}")
    log.info(f"  Condition: {condition_id}")
    log.info(f"  End time : {end_time}")
    log.info(f"  ENTRY={ENTRY_PRICE}  BET=${AMOUNT_PER_BET}  TP={TAKE_PROFIT}")
    log.info(f"  {sl_cfg}  BET_STEP={BET_STEP}  {auto_str}")
    log.info(f"  BUY={BUY_ORDER_TYPE}  SELL={SELL_ORDER_TYPE}")
    log.info("=" * 60)

    # Open WSS before monitoring begins
    stream = MarketStream(asset_ids=[token_up, token_down])
    stream.start()
    ready  = stream.wait_ready(timeout=WSS_READY_TIMEOUT)
    if ready:
        tick_up   = stream.get_tick_size(token_up)   or tick_up
        tick_down = stream.get_tick_size(token_down) or tick_down
        mu = stream.get_midpoint(token_up)
        md = stream.get_midpoint(token_down)
        log.info(
            f"[WSS] Connected — "
            f"UP={f'{mu:.4f}' if mu else '?'}  "
            f"DOWN={f'{md:.4f}' if md else '?'}"
        )
    else:
        log.warning(f"[WSS] Not ready after {WSS_READY_TIMEOUT}s — REST fallback active")

    try:
        while True:
            now = datetime.now(timezone.utc)
            if end_time and now >= end_time:
                log.info("Window closed — cancelling all open orders.")
                executor.gtc_tracker.cancel_all(log)
                break

            time_left  = (end_time - now).total_seconds() if end_time else 9999
            mins, secs = divmod(int(time_left), 60)

            tick_up   = stream.get_tick_size(token_up)   or tick_up
            tick_down = stream.get_tick_size(token_down) or tick_down

            prices = get_prices(stream, token_up, token_down)
            if prices is None:
                log.warning("Price unavailable — skipping tick")
                time.sleep(POLL_INTERVAL)
                continue

            up_p  = prices["UP"]
            dn_p  = prices["DOWN"]
            src   = "WSS" if stream.is_connected else "REST"

            # ══════════════════════════════════════════════════════════════════
            #  PHASE 1 — Waiting for entry
            # ══════════════════════════════════════════════════════════════════
            if not state.in_position:

                if not state.entry_armed:
                    if up_p < ENTRY_PRICE and dn_p < ENTRY_PRICE:
                        state.entry_armed = True
                        log.info(
                            f"  Entry armed — both below {ENTRY_PRICE} "
                            f"(UP={up_p:.4f} DOWN={dn_p:.4f})"
                        )
                    else:
                        log.info(
                            f"[{mins:02d}:{secs:02d}]  UP={up_p:.4f}  DOWN={dn_p:.4f}"
                            f"  | Waiting to arm below ENTRY={ENTRY_PRICE}  {src}"
                        )
                        time.sleep(POLL_INTERVAL)
                        continue

                trig_side = trig_price = None
                trig_tick = 0.01
                if up_p >= ENTRY_PRICE:
                    trig_side, trig_price, trig_tick = "UP",   up_p,  tick_up
                elif dn_p >= ENTRY_PRICE:
                    trig_side, trig_price, trig_tick = "DOWN", dn_p,  tick_down

                if trig_side:
                    log.info(
                        f"*** ENTRY: {trig_side} @ {trig_price:.4f} >= {ENTRY_PRICE} ***"
                    )
                    state.side         = trig_side
                    state.token_id     = token_up if trig_side == "UP" else token_down
                    state.condition_id = condition_id
                    state.entry_price  = trig_price

                    resp = executor.place_buy(
                        token_id  = state.token_id,
                        price     = trig_price,
                        usdc_size = AMOUNT_PER_BET,
                        tick_size = trig_tick,
                    )
                    if resp and resp.get("success"):
                        shares, usdc_paid = _parse_bet_result(resp, trig_price, AMOUNT_PER_BET)
                        log.info(
                            f"  BET #1 | shares={shares:.4f}  usdc=${usdc_paid:.4f}"
                            f"  token={state.token_id[:16]}..."
                        )
                        state.update_after_bet(trig_price, usdc_paid, shares)
                        if AUTOSET_BRACKETS:
                            place_brackets(executor, state, trig_tick)
                        else:
                            log.info(
                                "  AUTOSET_UP_TP_SL_ORDERS=false — "
                                "no bracket orders placed, monitoring TP/SL manually"
                            )
                        log.info(state.summary())
                    else:
                        log.error(f"  BET #1 failed — {resp}")
                        state.reset()
                else:
                    log.info(
                        f"[{mins:02d}:{secs:02d}]  UP={up_p:.4f}  DOWN={dn_p:.4f}"
                        f"  | Armed — waiting for ENTRY={ENTRY_PRICE}  {src}"
                    )

            # ══════════════════════════════════════════════════════════════════
            #  PHASE 2 — In position
            # ══════════════════════════════════════════════════════════════════
            else:
                cp        = up_p  if state.side == "UP" else dn_p
                tick_size = tick_up if state.side == "UP" else tick_down
                sl_d      = f"{state.effective_stop_loss:.4f}" if state.effective_stop_loss else "none"
                log.info(
                    f"[{mins:02d}:{secs:02d}]  {state.side}={cp:.4f}"
                    f"  AvgP={state.avg_price:.4f}  SL={sl_d}  TP={TAKE_PROFIT}"
                    f"  Shares={state.total_shares:.4f}  {src}"
                )

                # ── Detect silent bracket fills every 8 ticks ──────────────────
                state._tick_ctr += 1
                if state._tick_ctr >= 8:
                    state._tick_ctr = 0
                    if state.tp_order_id and not is_order_open(client, state.tp_order_id):
                        pnl = (TAKE_PROFIT - state.avg_price) * state.total_shares
                        log.info(
                            f"*** TP FILLED (detected) | TP={TAKE_PROFIT}"
                            f"  AvgP={state.avg_price:.4f}  Est P&L=+${pnl:.4f} ***"
                        )
                        executor.gtc_tracker.cancel_all(log)
                        break
                    if state.sl_order_id and not is_order_open(client, state.sl_order_id):
                        sl_p = state.effective_stop_loss
                        pnl  = (sl_p - state.avg_price) * state.total_shares
                        log.info(
                            f"*** SL FILLED (detected) | SL={sl_p:.4f}"
                            f"  AvgP={state.avg_price:.4f}  Est P&L=${pnl:.4f} ***"
                        )
                        executor.gtc_tracker.cancel_all(log)
                        break

                # ── Manual TP ──────────────────────────────────────────────────
                if not state.tp_order_id and cp >= TAKE_PROFIT:
                    log.info(f"*** TP (manual): {state.side}={cp:.4f} >= {TAKE_PROFIT} ***")
                    executor.gtc_tracker.cancel_all(log)
                    real = get_real_position(state.token_id, state.condition_id)
                    sell = real if (real is not None and real > 0) else state.total_shares
                    resp = executor.place_sell_immediate(
                        token_id      = state.token_id,
                        total_shares  = sell,
                        current_price = cp,
                        tick_size     = tick_size,
                    )
                    if resp:
                        pnl = (cp - state.avg_price) * sell
                        log.info(f"  CLOSED (TP manual) | Est P&L=+${pnl:.4f}")
                        break
                    time.sleep(POLL_INTERVAL)
                    continue

                # ── Manual SL ──────────────────────────────────────────────────
                if (
                    not state.sl_order_id
                    and state.effective_stop_loss is not None
                    and cp <= state.effective_stop_loss
                ):
                    sl_tag = (
                        "(BE)"      if SL_BREAKEVEN_MODE
                        else "(dyn)" if STOP_LOSS_OFFSET
                        else "(fixed)"
                    )
                    log.info(
                        f"*** SL {sl_tag} (manual): {state.side}={cp:.4f}"
                        f" <= {state.effective_stop_loss:.4f} ***"
                    )
                    executor.gtc_tracker.cancel_all(log)
                    real = get_real_position(state.token_id, state.condition_id)
                    sell = real if (real is not None and real > 0) else state.total_shares
                    resp = executor.place_sell_immediate(
                        token_id      = state.token_id,
                        total_shares  = sell,
                        current_price = cp,
                        tick_size     = tick_size,
                    )
                    if resp:
                        pnl = (cp - state.avg_price) * sell
                        log.info(f"  CLOSED (SL manual) | Est P&L=${pnl:.4f}")
                        break
                    time.sleep(POLL_INTERVAL)
                    continue

                # ── DCA ────────────────────────────────────────────────────────
                if BET_STEP is not None:
                    next_bet = round(state.last_bet_price + BET_STEP, 4)
                    if cp >= next_bet:
                        log.info(
                            f"*** DCA #{state.bets_count + 1}: "
                            f"{state.side}={cp:.4f} >= {next_bet:.4f} ***"
                        )
                        resp = executor.place_buy(
                            token_id  = state.token_id,
                            price     = cp,
                            usdc_size = AMOUNT_PER_BET,
                            tick_size = tick_size,
                        )
                        if resp and resp.get("success"):
                            shares, usdc_paid = _parse_bet_result(resp, cp, AMOUNT_PER_BET)
                            log.info(
                                f"  DCA filled | shares={shares:.4f}  usdc=${usdc_paid:.4f}"
                            )
                            state.update_after_bet(cp, usdc_paid, shares)
                            if AUTOSET_BRACKETS:
                                place_brackets(executor, state, tick_size)
                            log.info(state.summary())
                        else:
                            log.error(f"  DCA failed — {resp}")

            time.sleep(POLL_INTERVAL)

    finally:
        log.info("[WSS] Closing stream.")
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
                choices=["5 minutes", "15 minutes", "1 hour"],
            ).ask()
            if choice is None:
                sys.exit(0)
            interval = "1h" if "hour" in choice else "15m" if "15" in choice else "5m"
        except (ImportError, Exception):
            while True:
                raw = input("Interval — enter 5, 15, or 60: ").strip()
                if raw == "5":  interval = "5m";  break
                if raw == "15": interval = "15m"; break
                if raw == "60": interval = "1h";  break

    sl_cfg = (
        f"SL_OFFSET={STOP_LOSS_OFFSET}(dyn)" if STOP_LOSS_OFFSET
        else "SL=avg(BE)" if SL_BREAKEVEN_MODE
        else f"SL={STOP_LOSS}(fixed)"
    )
    log.info("=" * 60)
    log.info("SOL DCA Snipe bot starting")
    log.info(f"  Interval  : {interval.upper()}")
    log.info(f"  ENTRY={ENTRY_PRICE}  BET=${AMOUNT_PER_BET}  TP={TAKE_PROFIT}")
    log.info(f"  {sl_cfg}  BET_STEP={BET_STEP}")
    log.info(f"  BUY={BUY_ORDER_TYPE}  SELL={SELL_ORDER_TYPE}")
    log.info(f"  AUTOSET_UP_TP_SL_ORDERS={AUTOSET_BRACKETS}")
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