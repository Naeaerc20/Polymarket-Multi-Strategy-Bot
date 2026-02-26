"""
strategies/Copy-Trade/trader_monitor.py
----------------------------------------
Monitors Polymarket trader activity and detects NEW trades in real time.

Uses the public Data API (polling) to detect trades that occur AFTER the
bot starts. All trades that existed before startup are marked as seen on
init — so the bot never replays old history.

Classes:
    Trade          — parsed trade from the Data API response
    TraderConfig   — config + runtime state for one followed trader
    DataAPIClient  — thin wrapper around data-api.polymarket.com
    GammaAPIClient — thin wrapper around gamma-api.polymarket.com
    TraderMonitor  — polling loop that calls on_trade_callback on new trades

Standalone usage:
    python trader_monitor.py
"""

import json
import time
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Any, Optional, Callable, Set

import requests

log = logging.getLogger("copy-trade.monitor")


# ══════════════════════════════════════════════════════════════════════════════
#  DATA MODELS
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    """
    Represents a single trade detected from the Data API.
    asset_id == token_id (Polymarket uses both names interchangeably).
    """
    trader_address: str
    condition_id: str
    asset_id: str           # same as token_id
    side: str               # "BUY" | "SELL"
    size: float             # shares
    price: float            # price per share (0–1)
    usdc_size: float        # size × price
    timestamp: int          # Unix seconds
    outcome: str            # "Yes" | "No" | "UP" | "DOWN" etc.
    outcome_index: int
    title: str              # market title
    slug: str
    transaction_hash: Optional[str] = None

    @property
    def token_id(self) -> str:
        return self.asset_id

    @classmethod
    def from_api_response(cls, data: Dict[str, Any]) -> "Trade":
        size  = float(data.get("size",  0))
        price = float(data.get("price", 0))
        return cls(
            trader_address  = data.get("proxyWallet",     ""),
            condition_id    = data.get("conditionId",     ""),
            asset_id        = data.get("asset",           ""),
            side            = data.get("side",            "BUY"),
            size            = size,
            price           = price,
            usdc_size       = size * price,
            timestamp       = data.get("timestamp",       0),
            outcome         = data.get("outcome",         ""),
            outcome_index   = data.get("outcomeIndex",    0),
            title           = data.get("title",           ""),
            slug            = data.get("slug",            ""),
            transaction_hash= data.get("transactionHash", ""),
        )

    def __str__(self) -> str:
        return (
            f"Trade({self.side} {self.size:.2f} {self.outcome} "
            f"@ ${self.price:.4f} = ${self.usdc_size:.2f} "
            f"on '{self.title[:40]}')"
        )


@dataclass
class TraderConfig:
    """Configuration + runtime state for one followed trader."""
    address: str
    nickname: str = ""
    enabled: bool = True
    copy_buys: bool = True
    copy_sells: bool = False
    max_position_size: float = float("inf")
    reverse_trading: bool = False   # if True: mirror in opposite direction
    notes: str = ""

    # Runtime (not from JSON)
    last_known_trade_ts: int = 0
    total_trades_copied: int = 0

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TraderConfig":
        return cls(
            address           = data.get("address",           ""),
            nickname          = data.get("nickname",          ""),
            enabled           = data.get("enabled",           True),
            copy_buys         = data.get("copy_buys",         True),
            copy_sells        = data.get("copy_sells",        False),
            max_position_size = data.get("max_position_size", float("inf")),
            reverse_trading   = data.get("reverse_trading",   False),
            notes             = data.get("notes",             ""),
        )


# ══════════════════════════════════════════════════════════════════════════════
#  API CLIENTS
# ══════════════════════════════════════════════════════════════════════════════

class DataAPIClient:
    """Thin wrapper around https://data-api.polymarket.com (public)."""

    BASE_URL = "https://data-api.polymarket.com"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()

    def get_user_activity(
        self,
        user_address: str,
        limit: int = 100,
        offset: int = 0,
        activity_type: Optional[str] = "TRADE",
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {
            "user"  : user_address,
            "limit" : min(limit, 500),
            "offset": offset,
        }
        if activity_type: params["type"]  = activity_type
        if start_ts:      params["start"] = start_ts
        if end_ts:        params["end"]   = end_ts

        resp = self.session.get(f"{self.BASE_URL}/activity", params=params, timeout=10)
        resp.raise_for_status()
        return resp.json()

    def get_trades(
        self,
        user_address: str,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        resp = self.session.get(
            f"{self.BASE_URL}/trades",
            params={"user": user_address, "limit": min(limit, 10000), "offset": offset},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_current_positions(self, user_address: str) -> List[Dict[str, Any]]:
        resp = self.session.get(
            f"{self.BASE_URL}/positions",
            params={"user": user_address},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()


class GammaAPIClient:
    """Thin wrapper around https://gamma-api.polymarket.com (market data)."""

    BASE_URL = "https://gamma-api.polymarket.com"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()

    def get_market_by_condition_id(self, condition_id: str) -> Optional[Dict[str, Any]]:
        try:
            resp = self.session.get(
                f"{self.BASE_URL}/markets",
                params={"condition_id": condition_id},
                timeout=10,
            )
            resp.raise_for_status()
            markets = resp.json()
            return markets[0] if markets else None
        except Exception as exc:
            log.warning(f"[Gamma] Error fetching market {condition_id[:12]}…: {exc}")
            return None

    def get_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        resp = self.session.get(
            f"{self.BASE_URL}/markets",
            params={"slug": slug},
            timeout=10,
        )
        resp.raise_for_status()
        markets = resp.json()
        return markets[0] if markets else None

    def get_token_info(self, condition_id: str, outcome_index: int) -> Optional[Dict[str, Any]]:
        market = self.get_market_by_condition_id(condition_id)
        if not market:
            return None
        for token in market.get("tokens", []):
            if token.get("outcome_index") == outcome_index:
                return token
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  TRADER MONITOR
# ══════════════════════════════════════════════════════════════════════════════

class TraderMonitor:
    """
    Polls the Data API for each tracked trader and fires on_trade_callback
    only for trades that occur AFTER the bot started.

    Startup flow:
      1. Record bot_start_time = now()
      2. For each trader: fetch recent trades → mark ALL as seen (no replay)
      3. Poll every poll_interval seconds → detect only NEW tx hashes

    Thread-safe: can be stopped externally by calling stop().
    """

    def __init__(
        self,
        traders: List[TraderConfig],
        poll_interval: Optional[float] = None,
        on_trade_callback: Optional[Callable[[Trade, TraderConfig], None]] = None,
    ):
        # poll_interval=None → WebSocket-driven mode (no periodic polling)
        # poll_interval=N    → hybrid mode: poll every N seconds as fallback
        self.poll_interval      = poll_interval
        self.on_trade_callback  = on_trade_callback

        self.data_api  = DataAPIClient()
        self.gamma_api = GammaAPIClient()

        # address (lowercase) → TraderConfig
        self.traders: Dict[str, TraderConfig] = {
            t.address.lower(): t for t in traders
        }

        self._running          = False
        self._bot_start_time   = 0
        # address (lowercase) → set of seen transaction hashes
        self._seen_tx: Dict[str, Set[str]] = {}

    # ── Trader management ──────────────────────────────────────────────────

    def add_trader(self, trader: TraderConfig) -> None:
        self.traders[trader.address.lower()] = trader
        log.info(f"[Monitor] Added: {trader.nickname or trader.address[:12]}…")

    def remove_trader(self, address: str) -> None:
        key = address.lower()
        if key in self.traders:
            del self.traders[key]
            log.info(f"[Monitor] Removed: {address[:12]}…")

    # ── Initialization ─────────────────────────────────────────────────────

    def _initialize_trader(self, address: str) -> None:
        """Fetch current trades and mark them all as seen (no replay on start)."""
        key = address.lower()
        self._seen_tx.setdefault(key, set())

        try:
            activity = self.data_api.get_user_activity(
                user_address=address, limit=50, activity_type="TRADE"
            )
            for act in activity:
                tx = act.get("transactionHash", "")
                if tx:
                    self._seen_tx[key].add(tx)

            if activity:
                latest_ts = max(a.get("timestamp", 0) for a in activity)
                self.traders[key].last_known_trade_ts = latest_ts

            log.info(
                f"[Monitor] Init {address[:12]}…"
                f" — {len(self._seen_tx[key])} existing trades marked as seen"
            )
        except Exception as exc:
            log.warning(f"[Monitor] Init error for {address[:12]}…: {exc}")

    # ── Polling ────────────────────────────────────────────────────────────

    def check_trader(self, address: str) -> List[Trade]:
        """Return only NEW trades for this trader (unseen AND after bot_start)."""
        key       = address.lower()
        new_trades: List[Trade] = []
        self._seen_tx.setdefault(key, set())

        try:
            activity = self.data_api.get_user_activity(
                user_address=address, limit=50, activity_type="TRADE"
            )
            for act in activity:
                tx       = act.get("transactionHash", "")
                trade_ts = act.get("timestamp",       0)

                if tx and tx in self._seen_tx[key]:
                    continue                          # already processed
                if trade_ts < self._bot_start_time:
                    continue                          # happened before bot started

                trade = Trade.from_api_response(act)
                new_trades.append(trade)
                if tx:
                    self._seen_tx[key].add(tx)

            if new_trades:
                new_trades.sort(key=lambda t: t.timestamp)
                self.traders[key].last_known_trade_ts = new_trades[-1].timestamp

        except Exception as exc:
            log.warning(f"[Monitor] Poll error {address[:12]}…: {exc}")

        return new_trades

    def check_all_traders(self) -> List[tuple]:
        """Poll all enabled traders. Returns [(Trade, TraderConfig), ...]."""
        results = []
        for address, trader in self.traders.items():
            if not trader.enabled:
                continue
            for trade in self.check_trader(address):
                results.append((trade, trader))
        return results

    # ── Run loop ───────────────────────────────────────────────────────────

    async def run_async(self) -> None:
        self._running        = True
        self._bot_start_time = int(time.time())

        enabled = [t for t in self.traders.values() if t.enabled]
        log.info(f"[Monitor] Monitoring {len(enabled)} trader(s)")
        log.info(
            f"[Monitor] Bot start: "
            f"{datetime.fromtimestamp(self._bot_start_time).strftime('%Y-%m-%d %H:%M:%S')}"
        )
        log.info(f"[Monitor] Only trades AFTER this time will be copied")

        # Mark all existing trades as seen
        for address in self.traders:
            self._initialize_trader(address)

        if self.poll_interval is None:
            log.info("[Monitor] Mode: WebSocket — real-time wallet activity feed")
        else:
            log.info(f"[Monitor] Mode: Polling every {self.poll_interval}s (WSS + REST fallback)")

        while self._running:
            try:
                for trade, trader in self.check_all_traders():
                    ts_str = datetime.fromtimestamp(trade.timestamp).strftime("%H:%M:%S")
                    log.info(
                        f"[Monitor] NEW TRADE  "
                        f"{trader.nickname or trade.trader_address[:10]}…  "
                        f"@ {ts_str}  {trade}"
                    )
                    if self.on_trade_callback:
                        try:
                            self.on_trade_callback(trade, trader)
                        except Exception as exc:
                            log.error(f"[Monitor] Callback error: {exc}")
            except Exception as exc:
                log.error(f"[Monitor] Loop error: {exc}")

            # WSS mode: short sleep just to yield the event loop — detection
            # is driven by the WebSocket feed, not by this interval.
            # Polling mode: wait the full interval between REST checks.
            await asyncio.sleep(self.poll_interval if self.poll_interval else 0.1)

    def run(self) -> None:
        """Blocking entry point."""
        try:
            asyncio.run(self.run_async())
        except KeyboardInterrupt:
            log.info("[Monitor] Stopped by user")

    def stop(self) -> None:
        self._running = False


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def load_traders_from_json(filepath: str) -> List[TraderConfig]:
    """Load TraderConfig list from traders.json."""
    with open(filepath, "r") as f:
        data = json.load(f)
    return [TraderConfig.from_dict(t) for t in data.get("traders", [])]


# ══════════════════════════════════════════════════════════════════════════════
#  STANDALONE TEST
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s][%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(__file__).parent / "traders.json"
    traders     = load_traders_from_json(str(config_path))

    def on_new_trade(trade: Trade, trader: TraderConfig):
        print(f"  → Would copy: token_id={trade.token_id}  side={trade.side}")

    monitor = TraderMonitor(
        traders=traders,
        poll_interval=None,   # None = WebSocket mode; set a float for polling fallback
        on_trade_callback=on_new_trade,
    )

    try:
        monitor.run()
    except KeyboardInterrupt:
        monitor.stop()