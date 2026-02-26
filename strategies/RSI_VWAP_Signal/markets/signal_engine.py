"""
strategies/RSI_VWAP_Signal/signal_engine.py
--------------------------------------------
Real-time RSI + VWAP signal engine.

Data source: Binance WebSocket — kline (candlestick) stream, 5-minute intervals.
No API key required. Public endpoint.

────────────────────────────────────────────────────────────────────────────────
HOW RSI IS CALCULATED
────────────────────────────────────────────────────────────────────────────────
RSI (Relative Strength Index) measures momentum on a 0–100 scale.

1. Collect last N closing prices (default period = 14 candles)
2. Separate price changes into gains (up moves) and losses (down moves)
3. avg_gain = mean of all gains over the period
   avg_loss = mean of all losses over the period
4. RS = avg_gain / avg_loss
5. RSI = 100 - (100 / (1 + RS))

Interpretation:
  RSI > 70  → overbought  — asset rose too fast, DOWN likely
  RSI < 30  → oversold    — asset fell too fast, UP likely
  RSI 45–55 → neutral     — no clear momentum
  RSI crossing 50 upward  → bullish momentum building → UP
  RSI crossing 50 downward → bearish momentum building → DOWN

────────────────────────────────────────────────────────────────────────────────
HOW VWAP IS CALCULATED
────────────────────────────────────────────────────────────────────────────────
VWAP (Volume Weighted Average Price) is the average price weighted by volume
traded at each candle. Resets every trading day (UTC midnight).

For each candle:
  typical_price = (high + low + close) / 3
  cumulative_tp_vol += typical_price × volume
  cumulative_vol    += volume
  VWAP = cumulative_tp_vol / cumulative_vol

Interpretation:
  price > VWAP → asset trading ABOVE its fair value for the day → bullish
  price < VWAP → asset trading BELOW its fair value for the day → bearish
  price crosses VWAP upward   → momentum shift bullish → buy UP signal
  price crosses VWAP downward → momentum shift bearish → buy DOWN signal

────────────────────────────────────────────────────────────────────────────────
COMBINED SIGNAL LOGIC
────────────────────────────────────────────────────────────────────────────────
Signal = UP when:
  • price > VWAP           (above fair value, bulls in control)
  • RSI_OVERSOLD < RSI < RSI_OVERBOUGHT  (not extended in either direction)
  • RSI > RSI_BULL_THRESHOLD (e.g. 52) confirming upward momentum

Signal = DOWN when:
  • price < VWAP           (below fair value, bears in control)
  • RSI_OVERSOLD < RSI < RSI_OVERBOUGHT
  • RSI < RSI_BEAR_THRESHOLD (e.g. 48) confirming downward momentum

Signal = NEUTRAL when:
  • RSI is in overbought or oversold territory (extreme = avoid)
  • price is exactly on VWAP (no clear direction)
  • Not enough candles yet to calculate RSI reliably

────────────────────────────────────────────────────────────────────────────────
CONFIDENCE SCORING
────────────────────────────────────────────────────────────────────────────────
Each signal comes with a confidence score (0.0–1.0) based on:
  • How far RSI is from neutral (50) → stronger momentum = higher confidence
  • How far price is from VWAP      → bigger divergence = higher confidence
  • Whether RSI just crossed a threshold (fresh signal = higher confidence)

The bot only places orders when confidence >= MIN_SIGNAL_CONFIDENCE (.env).
"""

import json
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional, Tuple

try:
    import websocket
except ImportError:
    import os
    os.system("pip install websocket-client --break-system-packages -q")
    import websocket

log = logging.getLogger("signal-engine")

# ── Binance WebSocket ──────────────────────────────────────────────────────────
BINANCE_WSS = "wss://stream.binance.com:9443/ws"

# Symbol map: our asset key → Binance trading pair
BINANCE_SYMBOLS: Dict[str, str] = {
    "btc": "btcusdt",
    "eth": "ethusdt",
    "sol": "solusdt",
    "xrp": "xrpusdt",
}


# ══════════════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class Candle:
    """One completed 5-minute OHLCV candle."""
    open_time : int     # Unix ms
    open      : float
    high      : float
    low       : float
    close     : float
    volume    : float
    is_closed : bool    # True = candle fully closed, False = still forming

    @property
    def typical_price(self) -> float:
        """(H + L + C) / 3 — used for VWAP calculation."""
        return (self.high + self.low + self.close) / 3

    @classmethod
    def from_binance_kline(cls, k: dict) -> "Candle":
        return cls(
            open_time = int(k["t"]),
            open      = float(k["o"]),
            high      = float(k["h"]),
            low       = float(k["l"]),
            close     = float(k["c"]),
            volume    = float(k["v"]),
            is_closed = bool(k["x"]),
        )


@dataclass
class Signal:
    """Trading signal produced by the engine."""
    direction  : str    # "UP" | "DOWN" | "NEUTRAL"
    confidence : float  # 0.0 – 1.0
    rsi        : float
    vwap       : float
    price      : float
    asset      : str
    timestamp  : float = field(default_factory=time.time)

    @property
    def is_actionable(self) -> bool:
        return self.direction in ("UP", "DOWN")

    def __str__(self) -> str:
        ts = datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
        return (
            f"[{ts}] {self.asset.upper()} Signal={self.direction:<7} "
            f"conf={self.confidence:.2f}  RSI={self.rsi:.1f}  "
            f"price={self.price:.4f}  VWAP={self.vwap:.4f}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  RSI CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

class RSICalculator:
    """
    Wilder's RSI using EMA smoothing (same as TradingView default).

    Requires at least `period + 1` closes to produce a valid reading.
    Returns None until enough data is collected.
    """

    def __init__(self, period: int = 14):
        self.period    = period
        self._closes   : Deque[float] = deque(maxlen=period + 1)
        self._avg_gain : Optional[float] = None
        self._avg_loss : Optional[float] = None
        self._prev_rsi : Optional[float] = None

    def update(self, close: float) -> Optional[float]:
        """Feed a new closing price. Returns current RSI or None."""
        self._closes.append(close)

        if len(self._closes) < self.period + 1:
            return None  # not enough data yet

        closes = list(self._closes)

        # First calculation — simple average
        if self._avg_gain is None:
            gains  = [max(closes[i] - closes[i-1], 0) for i in range(1, self.period + 1)]
            losses = [max(closes[i-1] - closes[i], 0) for i in range(1, self.period + 1)]
            self._avg_gain = sum(gains)  / self.period
            self._avg_loss = sum(losses) / self.period
        else:
            # Wilder's EMA smoothing
            change = closes[-1] - closes[-2]
            gain   = max(change, 0)
            loss   = max(-change, 0)
            self._avg_gain = (self._avg_gain * (self.period - 1) + gain)  / self.period
            self._avg_loss = (self._avg_loss * (self.period - 1) + loss) / self.period

        if self._avg_loss == 0:
            rsi = 100.0
        else:
            rs  = self._avg_gain / self._avg_loss
            rsi = 100.0 - (100.0 / (1.0 + rs))

        self._prev_rsi = rsi
        return round(rsi, 2)

    @property
    def value(self) -> Optional[float]:
        return self._prev_rsi

    def reset(self):
        self._closes.clear()
        self._avg_gain = None
        self._avg_loss = None
        self._prev_rsi = None


# ══════════════════════════════════════════════════════════════════════════════
#  VWAP CALCULATOR
# ══════════════════════════════════════════════════════════════════════════════

class VWAPCalculator:
    """
    Daily VWAP — resets at UTC midnight automatically.

    Accumulates typical_price × volume across candles since midnight.
    """

    def __init__(self):
        self._cum_tp_vol : float = 0.0
        self._cum_vol    : float = 0.0
        self._session_day: int   = -1  # UTC day number
        self._vwap       : Optional[float] = None

    def _utc_day(self) -> int:
        return datetime.now(timezone.utc).timetuple().tm_yday

    def update(self, candle: Candle) -> Optional[float]:
        """Feed a closed candle. Returns current VWAP."""
        today = self._utc_day()

        # Reset at start of new UTC day
        if today != self._session_day:
            self._cum_tp_vol  = 0.0
            self._cum_vol     = 0.0
            self._session_day = today
            self._vwap        = None
            log.debug("[VWAP] New session — reset")

        if candle.volume <= 0:
            return self._vwap

        self._cum_tp_vol += candle.typical_price * candle.volume
        self._cum_vol    += candle.volume
        self._vwap        = round(self._cum_tp_vol / self._cum_vol, 6)
        return self._vwap

    @property
    def value(self) -> Optional[float]:
        return self._vwap

    def reset(self):
        self._cum_tp_vol  = 0.0
        self._cum_vol     = 0.0
        self._session_day = -1
        self._vwap        = None


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class SignalEngine:
    """
    Subscribes to Binance 5m kline WebSocket for one asset.
    On each closed candle: updates RSI + VWAP and emits a Signal.

    Parameters (from .env):
      RSI_PERIOD              Candle count for RSI (default 14)
      RSI_OVERBOUGHT          RSI above this → avoid UP entries (default 70)
      RSI_OVERSOLD            RSI below this → avoid DOWN entries (default 30)
      RSI_BULL_THRESHOLD      RSI must be above this for UP signal (default 52)
      RSI_BEAR_THRESHOLD      RSI must be below this for DOWN signal (default 48)
      MIN_SIGNAL_CONFIDENCE   Minimum confidence to emit actionable signal (default 0.55)
      SIGNAL_CANDLE_INTERVAL  Kline interval: 1m | 3m | 5m | 15m (default 5m)
    """

    def __init__(
        self,
        asset             : str,
        rsi_period        : int   = 14,
        rsi_overbought    : float = 70.0,
        rsi_oversold      : float = 30.0,
        rsi_bull_threshold: float = 52.0,
        rsi_bear_threshold: float = 48.0,
        min_confidence    : float = 0.55,
        interval          : str   = "5m",
        on_signal         : Optional[Callable[[Signal], None]] = None,
    ):
        self.asset              = asset.lower()
        self.rsi_period         = rsi_period
        self.rsi_overbought     = rsi_overbought
        self.rsi_oversold       = rsi_oversold
        self.rsi_bull_threshold = rsi_bull_threshold
        self.rsi_bear_threshold = rsi_bear_threshold
        self.min_confidence     = min_confidence
        self.interval           = interval
        self.on_signal          = on_signal

        self._rsi  = RSICalculator(period=rsi_period)
        self._vwap = VWAPCalculator()

        self._symbol      = BINANCE_SYMBOLS.get(self.asset, f"{self.asset}usdt")
        self._ws          : Optional[websocket.WebSocketApp] = None
        self._thread      : Optional[threading.Thread] = None
        self._running     = False
        self._last_signal : Optional[Signal] = None
        self._lock        = threading.Lock()

        # History for crossing detection
        self._prev_rsi    : Optional[float] = None
        self._prev_price  : Optional[float] = None

        log.info(
            f"[{asset.upper()}] SignalEngine init — "
            f"RSI({rsi_period}) OB={rsi_overbought} OS={rsi_oversold} "
            f"bull>{rsi_bull_threshold} bear<{rsi_bear_threshold} "
            f"min_conf={min_confidence} interval={interval}"
        )

    # ── WebSocket ──────────────────────────────────────────────────────────

    def _ws_url(self) -> str:
        return f"{BINANCE_WSS}/{self._symbol}@kline_{self.interval}"

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
            if msg.get("e") != "kline":
                return

            candle = Candle.from_binance_kline(msg["k"])

            # Update RSI on every tick (including open candle) for live reading
            rsi = self._rsi.update(candle.close)

            # Update VWAP only on closed candles to avoid distortion
            vwap = self._vwap.value
            if candle.is_closed:
                vwap = self._vwap.update(candle)
                log.debug(
                    f"[{self.asset.upper()}] Candle closed "
                    f"close={candle.close:.4f}  RSI={rsi}  VWAP={vwap}"
                )

            if rsi is None or vwap is None:
                log.debug(f"[{self.asset.upper()}] Waiting for indicator warmup…")
                return

            signal = self._compute_signal(candle.close, rsi, vwap)

            with self._lock:
                self._last_signal = signal
                self._prev_rsi    = rsi
                self._prev_price  = candle.close

            if signal.is_actionable and signal.confidence >= self.min_confidence:
                log.info(str(signal))
                if self.on_signal:
                    self.on_signal(signal)

        except Exception as exc:
            log.warning(f"[{self.asset.upper()}] Message parse error: {exc}")

    def _on_error(self, ws, error) -> None:
        log.warning(f"[{self.asset.upper()}] WS error: {error}")

    def _on_close(self, ws, code, msg) -> None:
        log.info(f"[{self.asset.upper()}] WS closed ({code}) — reconnecting…")
        if self._running:
            time.sleep(3)
            self._connect()

    def _on_open(self, ws) -> None:
        log.info(f"[{self.asset.upper()}] Binance WS connected ({self._symbol}@kline_{self.interval})")

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            self._ws_url(),
            on_message = self._on_message,
            on_error   = self._on_error,
            on_close   = self._on_close,
            on_open    = self._on_open,
        )
        self._ws.run_forever(ping_interval=20, ping_timeout=10)

    # ── Signal computation ─────────────────────────────────────────────────

    def _compute_signal(self, price: float, rsi: float, vwap: float) -> Signal:
        """
        Core signal logic combining RSI + VWAP.

        Confidence components:
          rsi_component  — how far RSI deviates from 50 toward the signal direction
          vwap_component — how far price deviates from VWAP (normalized)
          crossing_bonus — RSI just crossed bull/bear threshold this tick
        """
        direction   = "NEUTRAL"
        confidence  = 0.0

        above_vwap  = price > vwap
        below_vwap  = price < vwap
        rsi_extreme = rsi >= self.rsi_overbought or rsi <= self.rsi_oversold

        # Base RSI component: distance from 50 toward signal direction, normalized 0–1
        rsi_dist_up   = max(rsi - 50, 0) / 50          # 0 at RSI=50, 1 at RSI=100
        rsi_dist_down = max(50 - rsi, 0) / 50          # 0 at RSI=50, 1 at RSI=0

        # VWAP component: % distance from VWAP, capped at 3%
        vwap_pct   = abs(price - vwap) / vwap if vwap > 0 else 0
        vwap_comp  = min(vwap_pct / 0.03, 1.0)         # 0–1, saturates at 3% deviation

        # RSI crossing bonus (fresh signal this tick)
        crossing_bonus = 0.0
        if self._prev_rsi is not None:
            just_crossed_bull = self._prev_rsi < self.rsi_bull_threshold <= rsi
            just_crossed_bear = self._prev_rsi > self.rsi_bear_threshold >= rsi
            if just_crossed_bull or just_crossed_bear:
                crossing_bonus = 0.15
                log.info(
                    f"[{self.asset.upper()}] RSI crossing detected "
                    f"{self._prev_rsi:.1f} → {rsi:.1f}"
                )

        # ── UP signal ─────────────────────────────────────────────────────
        if (above_vwap
                and rsi > self.rsi_bull_threshold
                and not rsi_extreme):
            direction  = "UP"
            confidence = round(
                0.45 * rsi_dist_up +
                0.40 * vwap_comp   +
                0.15 * (1.0 if above_vwap else 0.0) +
                crossing_bonus,
                3
            )

        # ── DOWN signal ───────────────────────────────────────────────────
        elif (below_vwap
                and rsi < self.rsi_bear_threshold
                and not rsi_extreme):
            direction  = "DOWN"
            confidence = round(
                0.45 * rsi_dist_down +
                0.40 * vwap_comp     +
                0.15 * (1.0 if below_vwap else 0.0) +
                crossing_bonus,
                3
            )

        # ── Extreme RSI override → NEUTRAL ────────────────────────────────
        elif rsi_extreme:
            direction  = "NEUTRAL"
            confidence = 0.0

        return Signal(
            direction  = direction,
            confidence = min(confidence, 1.0),
            rsi        = rsi,
            vwap       = vwap,
            price      = price,
            asset      = self.asset,
        )

    # ── Public API ─────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start WebSocket in background thread."""
        self._running = True
        self._thread  = threading.Thread(
            target = self._connect,
            name   = f"signal-{self.asset}",
            daemon = True,
        )
        self._thread.start()
        log.info(f"[{self.asset.upper()}] SignalEngine started")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()
        log.info(f"[{self.asset.upper()}] SignalEngine stopped")

    @property
    def last_signal(self) -> Optional[Signal]:
        with self._lock:
            return self._last_signal

    def wait_ready(self, timeout: float = 120.0) -> bool:
        """
        Block until RSI + VWAP have enough data to produce signals.
        Returns True when ready, False on timeout.
        Requires (rsi_period + 1) closed candles — ~75 min at 5m interval.
        For faster startup we pre-load historical candles via REST (see below).
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            sig = self.last_signal
            if sig is not None:
                return True
            time.sleep(1)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  HISTORICAL PRELOAD (REST)
# ══════════════════════════════════════════════════════════════════════════════

def preload_history(engine: "SignalEngine", lookback_candles: int = 50) -> None:
    """
    Pre-warm RSI and VWAP with recent historical candles from Binance REST API.
    This avoids the ~75-minute warmup wait when the bot first starts.

    Fetches up to `lookback_candles` closed 5m candles and feeds them
    into the engine's RSI and VWAP calculators before the live stream opens.
    """
    import requests
    symbol   = engine._symbol.upper()
    interval = engine.interval
    url      = "https://api.binance.com/api/v3/klines"
    params   = {"symbol": symbol, "interval": interval, "limit": lookback_candles}

    try:
        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()
        klines = resp.json()

        log.info(f"[{engine.asset.upper()}] Preloading {len(klines)} historical candles…")

        for k in klines[:-1]:  # skip last (may be open)
            candle = Candle(
                open_time = int(k[0]),
                open      = float(k[1]),
                high      = float(k[2]),
                low       = float(k[3]),
                close     = float(k[4]),
                volume    = float(k[5]),
                is_closed = True,
            )
            engine._rsi.update(candle.close)
            engine._vwap.update(candle)

        rsi  = engine._rsi.value
        vwap = engine._vwap.value
        log.info(
            f"[{engine.asset.upper()}] Preload complete — "
            f"RSI={rsi}  VWAP={vwap}"
        )

    except Exception as exc:
        log.warning(f"[{engine.asset.upper()}] Preload failed: {exc} — will warm up live")