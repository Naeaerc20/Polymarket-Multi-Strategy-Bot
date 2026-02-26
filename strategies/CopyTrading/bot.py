"""
strategies/Copy-Trade/bot.py
-----------------------------
Copy Trading Bot — automatically mirrors trades from followed wallets.

Detects new trades from configured traders (traders.json) via Data API
polling and immediately replicates them using the project's shared
OrderExecutor + CLOB client.

MODES:
  fixed      → spend exactly COPY_AMOUNT_USDC per copied trade
  percentage → spend COPY_PERCENTAGE% of the original trade's USDC value

.env variables (Copy-Trade strategy):
  COPY_MODE              fixed | percentage  (default: fixed)
  COPY_AMOUNT_USDC       USDC per copied trade (COPY_MODE=fixed)
  COPY_PERCENTAGE        % of original trade to copy (COPY_MODE=percentage)
  COPY_ORDER_BUY_TYPE    FAK | FOK for BUY copies  (default: FAK)
  COPY_ORDER_SELL_TYPE   FAK | FOK for SELL copies (default: FAK)
  COPY_MIN_TRADE_USDC    Skip trades smaller than this (default: 0)
  COPY_MAX_TRADE_USDC    Skip trades larger than this  (default: unlimited)
  COPY_POLL_INTERVAL     Seconds between Data API polls | null = WebSocket only (default: null)
  COPY_DRY_RUN           true | false — log only, no real orders (default: false)

Shared .env variables (wallet + credentials — same as all other bots):
  POLY_PRIVATE_KEY, FUNDER_ADDRESS, POLY_RPC, SIGNATURE_TYPE
  POLY_API_KEY, POLY_API_SECRET, POLY_API_PASSPHRASE
  BUY_ORDER_TYPE  (used as fallback if COPY_ORDER_TYPE is not set)

Usage:
  python strategies/Copy-Trade/bot.py
  python strategies/Copy-Trade/bot.py --dry-run
  python strategies/Copy-Trade/bot.py --config path/to/traders.json
"""

import os
import sys
import json
import time
import signal
import logging
import argparse
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Any

from dotenv import load_dotenv

# ── Project root ───────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / ".env")
sys.path.insert(0, str(_ROOT))

from order_executor import OrderExecutor
from trader_monitor import (
    TraderMonitor,
    TraderConfig,
    Trade,
    load_traders_from_json,
    DataAPIClient,
    GammaAPIClient,
)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s][%(levelname)s][%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("copy-trade")

# ── Strategy directory (for loading traders.json) ─────────────────────────────
STRATEGY_DIR = Path(__file__).resolve().parent

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID  = 137


# ══════════════════════════════════════════════════════════════════════════════
#  COPY CONFIG
# ══════════════════════════════════════════════════════════════════════════════

class CopyConfig:
    """
    Sizing and filtering rules for copy trades.
    Loaded from .env — all values can be overridden via CLI args.
    """

    def __init__(
        self,
        mode: str                    = "fixed",
        amount_usdc: float           = 1.0,
        percentage: float            = 10.0,
        buy_order_type: str          = "FAK",
        sell_order_type: str         = "FAK",
        min_trade_usdc: float        = 0.0,
        max_trade_usdc: float        = float("inf"),
        dry_run: bool                = False,
    ):
        self.mode            = mode.lower()             # "fixed" | "percentage"
        self.amount_usdc     = amount_usdc              # USDC per trade (fixed mode)
        self.percentage      = percentage               # % of original (percentage mode)
        self.buy_order_type  = buy_order_type.upper()   # "FAK" | "FOK" for BUY copies
        self.sell_order_type = sell_order_type.upper()  # "FAK" | "FOK" for SELL copies
        self.min_trade_usdc  = min_trade_usdc
        self.max_trade_usdc  = max_trade_usdc
        self.dry_run         = dry_run

    @classmethod
    def from_env(cls) -> "CopyConfig":
        def _float(key, default):
            v = os.getenv(key, "")
            try:
                return float(v) if v.lower() not in ("", "null", "none") else default
            except ValueError:
                return default

        return cls(
            mode            = os.getenv("COPY_MODE",             "fixed"),
            amount_usdc     = _float("COPY_AMOUNT_USDC",         1.0),
            percentage      = _float("COPY_PERCENTAGE",          10.0),
            buy_order_type  = os.getenv("COPY_ORDER_BUY_TYPE")
                              or os.getenv("BUY_ORDER_TYPE",     "FAK"),
            sell_order_type = os.getenv("COPY_ORDER_SELL_TYPE")
                              or os.getenv("SELL_ORDER_TYPE",    "FAK"),
            min_trade_usdc  = _float("COPY_MIN_TRADE_USDC",      0.0),
            max_trade_usdc  = _float("COPY_MAX_TRADE_USDC",      float("inf")),
            dry_run         = os.getenv("COPY_DRY_RUN", "false").lower() == "true",
        )

    def compute_copy_size(self, original_usdc: float) -> float:
        """Return USDC amount to spend on the copy trade."""
        if self.mode == "percentage":
            return round(original_usdc * self.percentage / 100, 2)
        return self.amount_usdc

    def should_copy(self, trade: Trade, trader: TraderConfig) -> tuple[bool, str]:
        """
        Return (True, "") if the trade should be copied, else (False, reason).
        """
        # Respect trader-level copy_buys / copy_sells flags
        if trade.side == "BUY"  and not trader.copy_buys:
            return False, "copy_buys=false for this trader"
        if trade.side == "SELL" and not trader.copy_sells:
            return False, "copy_sells=false for this trader"

        # USDC size filter
        if trade.usdc_size < self.min_trade_usdc:
            return False, f"trade too small (${trade.usdc_size:.2f} < ${self.min_trade_usdc:.2f})"
        if trade.usdc_size > self.max_trade_usdc:
            return False, f"trade too large (${trade.usdc_size:.2f} > ${self.max_trade_usdc:.2f})"

        # Position size cap (per trader)
        copy_size = self.compute_copy_size(trade.usdc_size)
        if copy_size > trader.max_position_size:
            return False, f"copy size ${copy_size:.2f} > trader max ${trader.max_position_size:.2f}"

        return True, ""


# ══════════════════════════════════════════════════════════════════════════════
#  CLOB CLIENT + ORDER EXECUTOR
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


def get_tick_size(client, token_id: str) -> float:
    try:
        return float(client.get_tick_size(token_id) or 0.01)
    except Exception:
        return 0.01


def get_midpoint(client, token_id: str) -> Optional[float]:
    """Fetch current mid price from REST (used to compute copy price)."""
    import requests as _requests
    try:
        resp = _requests.get(
            f"{CLOB_HOST}/midpoint",
            params={"token_id": token_id},
            timeout=5,
        )
        resp.raise_for_status()
        return float(resp.json()["mid"])
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  REVERSE TRADING HELPER
# ══════════════════════════════════════════════════════════════════════════════

_gamma_api = GammaAPIClient()

def get_opposite_token(trade: Trade) -> Optional[str]:
    """
    Given a trade, return the token_id of the OPPOSITE outcome in the same market.

    Example:
      Trader buys UP  token → we return the DOWN token id
      Trader buys DOWN token → we return the UP token id

    Uses condition_id + outcome_index from the trade to locate the other token
    via the Gamma API market data.
    Returns None if the opposite token cannot be resolved.
    """
    try:
        market = _gamma_api.get_market_by_condition_id(trade.condition_id)
        if not market:
            log.warning(f"  [REVERSE] Market not found for condition_id={trade.condition_id[:12]}…")
            return None

        # Try clobTokenIds first (list of token ids ordered by outcome index)
        import json as _json
        tokens_raw = market.get("clobTokenIds") or market.get("clob_token_ids", "[]")
        tokens = _json.loads(tokens_raw) if isinstance(tokens_raw, str) else tokens_raw

        if len(tokens) >= 2:
            # Find which index belongs to our token, return the other
            if tokens[0] == trade.token_id:
                return tokens[1]
            if tokens[1] == trade.token_id:
                return tokens[0]

        # Fallback: iterate market tokens list
        for token in market.get("tokens", []):
            tid = token.get("token_id") or token.get("id", "")
            if tid and tid != trade.token_id:
                return tid

        log.warning(f"  [REVERSE] Could not find opposite token for {trade.token_id[:12]}…")
        return None

    except Exception as exc:
        log.warning(f"  [REVERSE] Opposite token lookup failed: {exc}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  COPY TRADE EXECUTION
# ══════════════════════════════════════════════════════════════════════════════

def execute_copy_trade(
    trade: Trade,
    trader: TraderConfig,
    executor: OrderExecutor,
    copy_config: CopyConfig,
) -> Dict[str, Any]:
    """
    Place a copy order mirroring `trade`.

    For BUY  — place a FAK/FOK buy at current mid price.
    For SELL — place a FAK/FOK sell (requires existing shares; skipped by default).

    Returns a result dict with success/reason.
    """
    ok, reason = copy_config.should_copy(trade, trader)
    if not ok:
        return {"success": False, "reason": reason}

    copy_usdc = copy_config.compute_copy_size(trade.usdc_size)

    # ── Reverse trading ────────────────────────────────────────────────────────
    # When reverse_trading=true for this trader we invert both:
    #   • The SIDE:  BUY  → SELL  |  SELL → BUY
    #   • The TOKEN: use the opposite outcome token in the same market
    #     (e.g. trader buys UP → we buy DOWN, same condition_id different token)
    effective_side = trade.side
    token_id       = trade.token_id

    if trader.reverse_trading:
        # Flip side
        effective_side = "SELL" if trade.side == "BUY" else "BUY"

        # Resolve opposite token
        opposite_token = get_opposite_token(trade)
        if opposite_token:
            token_id = opposite_token
            log.info(
                f"  [REVERSE] {trade.side} {trade.outcome} → "
                f"{effective_side} opposite token {token_id[:12]}…"
            )
        else:
            # Could not find opposite token — abort to avoid wrong-side trade
            return {
                "success": False,
                "reason": "reverse_trading: opposite token not found — trade skipped",
            }

    tick_size     = get_tick_size(executor.client, token_id)
    current_price = get_midpoint(executor.client, token_id) or trade.price

    log.info(
        f"  {'[REVERSE] ' if trader.reverse_trading else ''}"
        f"{effective_side} | token={token_id[:12]}… "
        f"| original=${trade.usdc_size:.2f} | copy=${copy_usdc:.2f} "
        f"| price={current_price:.4f}"
    )

    order_type = copy_config.buy_order_type if effective_side == "BUY" else copy_config.sell_order_type

    if copy_config.dry_run:
        log.info(f"  [DRY RUN] Would place {order_type} {trade.side} "
                 f"${copy_usdc:.2f} @ {current_price:.4f}")
        return {"success": True, "dry_run": True, "copy_size_usdc": copy_usdc}

    # Temporarily override the executor's order type so it uses the
    # copy-specific type (COPY_ORDER_BUY_TYPE / COPY_ORDER_SELL_TYPE)
    # instead of the global BUY_ORDER_TYPE / SELL_ORDER_TYPE from .env.
    _prev_buy_type  = executor.buy_order_type
    _prev_sell_type = executor.sell_order_type

    try:
        executor.buy_order_type  = copy_config.buy_order_type
        executor.sell_order_type = copy_config.sell_order_type

        if effective_side == "BUY":
            resp = executor.place_buy(
                token_id  = token_id,
                price     = current_price,
                usdc_size = copy_usdc,
                tick_size = tick_size,
            )
        else:
            # SELL — place_sell_bracket is for TP/SL bracket pairs.
            # For a copy-sell we just want a single outright sell order.
            # We use place_buy with SELL side via the internal helpers directly.
            if executor.sell_order_type == "GTC":
                from decimal import Decimal, ROUND_DOWN
                from order_executor import _gtc_order_params
                price_f, size_f = _gtc_order_params(current_price, copy_usdc, tick_size)
                log.info(f"  SELL {size_f:.4f} shares @ {price_f:.4f}  [GTC]")
                resp = executor._place_gtc_order(token_id, price_f, size_f, "SELL")
                if resp and isinstance(resp, dict):
                    order_id = executor._extract_order_id(resp)
                    if order_id:
                        executor.gtc_tracker.schedule(order_id, executor.gtc_timeout, log)
            elif executor.sell_order_type == "FOK":
                from order_executor import _safe_order_params
                price_f, size_f = _safe_order_params(current_price, copy_usdc, tick_size)
                log.info(f"  SELL {size_f:.4f} shares @ {price_f:.4f}  [FOK]")
                resp = executor._place_fok_order(token_id, price_f, size_f, "SELL")
            else:  # FAK
                from decimal import Decimal, ROUND_DOWN
                amount_f = float(Decimal(str(copy_usdc)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
                from order_executor import _safe_order_params
                price_f, size_f = _safe_order_params(current_price, copy_usdc, tick_size)
                log.info(f"  SELL ${amount_f:.2f} USDC  worst_price={price_f:.4f}  [FAK]")
                resp = executor._place_fak_order(token_id, amount_f, "SELL", price_f, size_f)

        if resp and isinstance(resp, dict) and resp.get("success"):
            trader.total_trades_copied += 1
            return {"success": True, "copy_size_usdc": copy_usdc, "resp": resp}
        else:
            return {"success": False, "error": str(resp)}

    except Exception as exc:
        log.error(f"  Order error: {exc}")
        return {"success": False, "error": str(exc)}

    finally:
        # Always restore executor's original order types
        executor.buy_order_type  = _prev_buy_type
        executor.sell_order_type = _prev_sell_type


# ══════════════════════════════════════════════════════════════════════════════
#  BOT
# ══════════════════════════════════════════════════════════════════════════════

class CopyTradeBot:
    """
    Orchestrates the copy-trade strategy:
      1. Loads traders from traders.json
      2. Authenticates with CLOB
      3. Starts TraderMonitor polling loop
      4. On each detected trade → execute_copy_trade()
    """

    def __init__(
        self,
        traders_config_path: str,
        copy_config: CopyConfig,
    ):
        self.copy_config = copy_config
        self.traders_path = traders_config_path

        self._stats = {
            "detected" : 0,
            "executed" : 0,
            "skipped"  : 0,
            "errors"   : 0,
            "start_time": None,
        }

        # Load traders
        self.traders = self._load_traders(traders_config_path)

        # Build CLOB client + executor
        self.client   = build_clob_client()
        self.executor = OrderExecutor(client=self.client, log=log)

        # Poll interval — null means rely on WebSocket (TraderMonitor handles it)
        _pi_raw       = os.getenv("COPY_POLL_INTERVAL", "null")
        _poll_interval = (
            float(_pi_raw)
            if _pi_raw.strip().lower() not in ("null", "none", "")
            else None
        )

        # Monitor
        self.monitor = TraderMonitor(
            traders           = self.traders,
            poll_interval     = _poll_interval,
            on_trade_callback = self._on_trade,
        )

        self._running = False

    # ── Loader ────────────────────────────────────────────────────────────

    def _load_traders(self, path: str) -> List[TraderConfig]:
        try:
            traders = load_traders_from_json(path)
            log.info(f"Loaded {len(traders)} trader(s) from {path}")
            enabled = [t for t in traders if t.enabled]
            if not enabled:
                log.warning("No traders are enabled — set enabled=true in traders.json")
            return traders
        except FileNotFoundError:
            log.error(f"traders.json not found at: {path}")
            log.error("Edit strategies/Copy-Trade/traders.json and add wallet addresses")
            sys.exit(1)

    # ── Trade callback ─────────────────────────────────────────────────────

    def _on_trade(self, trade: Trade, trader: TraderConfig) -> None:
        self._stats["detected"] += 1
        name = trader.nickname or trade.trader_address[:12] + "…"

        rev_tag = "  ⟲ REVERSE MODE" if trader.reverse_trading else ""
        log.info("=" * 60)
        log.info(f"TRADE DETECTED  [{name}]{rev_tag}")
        log.info(f"  Market : {trade.title[:55]}")
        log.info(f"  Action : {trade.side} {trade.size:.2f} {trade.outcome} @ ${trade.price:.4f}")
        log.info(f"  Value  : ${trade.usdc_size:.2f}  |  token: {trade.token_id[:16]}…")
        if trader.reverse_trading:
            opp = "SELL" if trade.side == "BUY" else "BUY"
            log.info(f"  Reverse: will place {opp} on OPPOSITE token")

        result = execute_copy_trade(trade, trader, self.executor, self.copy_config)

        if result.get("success"):
            self._stats["executed"] += 1
            tag = " [DRY RUN]" if result.get("dry_run") else ""
            log.info(f"  ✔ Copied${result.get('copy_size_usdc', 0):.2f}{tag}")
        else:
            self._stats["skipped"] += 1
            reason = result.get("reason") or result.get("error", "unknown")
            log.info(f"  ✗ Skipped — {reason}")

        log.info("=" * 60)

    # ── Startup banner ─────────────────────────────────────────────────────

    def _banner(self) -> None:
        mode_str = (
            f"fixed ${self.copy_config.amount_usdc:.2f}/trade"
            if self.copy_config.mode == "fixed"
            else f"{self.copy_config.percentage:.1f}% of original"
        )
        print()
        print("╔══════════════════════════════════════════════════════╗")
        print("║        POLYMARKET — COPY TRADE STRATEGY              ║")
        print("╚══════════════════════════════════════════════════════╝")
        print(f"  Mode       : {self.copy_config.mode.upper()}  ({mode_str})")
        print(f"  BUY type   : {self.copy_config.buy_order_type}")
        print(f"  SELL type  : {self.copy_config.sell_order_type}")
        print(f"  Dry run    : {self.copy_config.dry_run}")
        enabled = [t for t in self.traders if t.enabled]
        print(f"  Traders    : {len(self.traders)} total, {len(enabled)} enabled")
        for t in enabled:
            rev_tag = "  ⟲ REVERSE" if t.reverse_trading else ""
            print(f"    • {t.nickname or t.address[:16]}…  "
                  f"(buys={t.copy_buys} sells={t.copy_sells} "
                  f"max=${t.max_position_size}{rev_tag})")
        if self.copy_config.dry_run:
            print()
            print("  ⚠  DRY RUN — no real orders will be placed")
        print()

    # ── Stats ──────────────────────────────────────────────────────────────

    def _print_stats(self) -> None:
        elapsed = ""
        if self._stats["start_time"]:
            delta = datetime.now() - datetime.fromisoformat(self._stats["start_time"])
            elapsed = str(delta).split(".")[0]
        print()
        print("═" * 40)
        print("  COPY-TRADE SESSION SUMMARY")
        print("═" * 40)
        print(f"  Detected  : {self._stats['detected']}")
        print(f"  Executed  : {self._stats['executed']}")
        print(f"  Skipped   : {self._stats['skipped']}")
        print(f"  Errors    : {self._stats['errors']}")
        print(f"  Runtime   : {elapsed}")
        print("═" * 40)

    # ── Run ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        self._stats["start_time"] = datetime.now().isoformat()
        self._banner()

        def _shutdown(sig, frame):
            log.info("Shutdown signal — stopping bot …")
            self._running = False
            self.monitor.stop()
            self._print_stats()
            sys.exit(0)

        signal.signal(signal.SIGINT,  _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        log.info("CLOB client authenticated OK")
        log.info("Starting copy-trade monitor … Press Ctrl+C to stop")

        try:
            self.monitor.run()
        except Exception as exc:
            log.error(f"Bot error: {exc}")
            import traceback; traceback.print_exc()
        finally:
            self._running = False
            self._print_stats()

    def stop(self) -> None:
        self._running = False
        self.monitor.stop()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Polymarket Copy-Trade Strategy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python bot.py
  python bot.py --dry-run
  python bot.py --mode fixed   --amount 2.5
  python bot.py --mode percent --percentage 25
  python bot.py --config /path/to/traders.json
        """,
    )
    parser.add_argument("--dry-run",    action="store_true", help="Log only, no real orders")
    parser.add_argument("--config",     default=str(STRATEGY_DIR / "traders.json"),
                        help="Path to traders.json")
    parser.add_argument("--mode",       choices=["fixed", "percentage"], default=None)
    parser.add_argument("--amount",     type=float, default=None,
                        help="Fixed USDC per copied trade")
    parser.add_argument("--percentage", type=float, default=None,
                        help="Percent of original trade to copy (1-100)")
    parser.add_argument("--buy-order-type",  choices=["FAK", "FOK"], default=None,
                        help="Order type for BUY copies")
    parser.add_argument("--sell-order-type", choices=["FAK", "FOK"], default=None,
                        help="Order type for SELL copies")
    return parser.parse_args()


def run(dry_run: Optional[bool] = None) -> None:
    """
    Module entry point — called by main.py when strategy=copytrade.
    All settings loaded from .env; dry_run can be overridden by caller.
    """
    copy_config = CopyConfig.from_env()
    if dry_run is not None:
        copy_config.dry_run = dry_run

    config_path = str(STRATEGY_DIR / "traders.json")
    bot = CopyTradeBot(traders_config_path=config_path, copy_config=copy_config)
    bot.run()


if __name__ == "__main__":
    args = parse_args()

    copy_config = CopyConfig.from_env()

    # CLI overrides
    if args.dry_run:              copy_config.dry_run     = True
    if args.mode:                 copy_config.mode        = args.mode
    if args.amount is not None:   copy_config.amount_usdc = args.amount
    if args.percentage is not None: copy_config.percentage = args.percentage
    if args.buy_order_type:       copy_config.buy_order_type  = args.buy_order_type
    if args.sell_order_type:      copy_config.sell_order_type = args.sell_order_type

    bot = CopyTradeBot(
        traders_config_path = args.config,
        copy_config         = copy_config,
    )
    bot.run()