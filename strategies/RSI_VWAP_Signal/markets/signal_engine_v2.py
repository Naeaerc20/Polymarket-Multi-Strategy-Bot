"""
strategies/RSI_VWAP_Signal/markets/signal_engine_v2.py
───────────────────────────────────────────────────────
Multi-source signal engine v2 — Binance-only.

Fuente de señal única:

  1. BINANCE  (RSI + VWAP)
     ─────────────────────
     WebSocket de klines (1m / 5m / 15m / 1h).
     Calcula RSI con suavizado EMA de Wilder y VWAP diario.
     El periodo de RSI se ajusta adaptativamente al tiempo restante
     de la ventana de Polymarket (ver compute_adaptive_interval en bot.py).
     → Emite UP | DOWN | NEUTRAL + puntuación de confianza.

  2. POLYMARKET CLOB  (order book best ask — solo precio de entrada)
     ────────────────────────────────────────────────────────────────
     GET https://clob.polymarket.com/book?token_id={id}
     Devuelve el mejor precio de compra (best ask) para UP y DOWN.
     → Solo se usa para obtener el precio óptimo de entrada.
     → NO genera señal direccional.

SEÑAL: MultiSourceSignal
  Basada exclusivamente en Binance RSI+VWAP.
  El CLOB de Polymarket solo aporta el best_ask para colocar la orden
  al precio óptimo en vez de usar el midpoint.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

import requests

# ── Importar engine base ───────────────────────────────────────────────────────
try:
    from signal_engine import SignalEngine, Signal, preload_history
except ImportError:
    import sys
    from pathlib import Path
    _here = Path(__file__).resolve().parent
    if str(_here) not in sys.path:
        sys.path.insert(0, str(_here))
    from signal_engine import SignalEngine, Signal, preload_history

log = logging.getLogger("signal-engine-v2")

CLOB_HOST = "https://clob.polymarket.com"


# ══════════════════════════════════════════════════════════════════════════════
#  DATA STRUCTURES
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class MultiSourceSignal:
    """Señal basada en Binance RSI+VWAP con best_ask de Polymarket para entrada óptima."""
    direction         : str            # "UP" | "DOWN" | "NEUTRAL"
    confidence        : float          # 0.0–1.0
    asset             : str
    timestamp         : float = field(default_factory=time.time)

    rsi               : float = 0.0
    vwap              : float = 0.0
    binance_price     : float = 0.0

    poly_up_best_ask  : Optional[float] = None
    poly_down_best_ask: Optional[float] = None

    consensus_count   : int = 1
    sources_checked   : int = 1

    @property
    def is_actionable(self) -> bool:
        return self.direction in ("UP", "DOWN")

    def best_ask_for(self, direction: str) -> Optional[float]:
        """Devuelve el mejor precio de compra para la dirección dada."""
        return self.poly_up_best_ask if direction == "UP" else self.poly_down_best_ask

    def __str__(self) -> str:
        ts = datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S")
        return (
            f"[{ts}] {self.asset.upper()} dir={self.direction:<7} "
            f"conf={self.confidence:.2f}  RSI={self.rsi:.1f}  "
            f"VWAP={self.vwap:.4f}  price={self.binance_price:.4f}"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  POLYMARKET ORDER BOOK
# ══════════════════════════════════════════════════════════════════════════════

class PolymarketBook:
    """
    Consulta el order book del CLOB de Polymarket para obtener el mejor precio
    de compra (best ask).

    GET /book?token_id={id}
      → asks: [{"price":"0.51","size":"50"}, ...]  (ascendente)

    Solo se usa para el precio de entrada óptimo — no genera señal.
    """

    def __init__(self, timeout: float = 5.0):
        self.timeout    = timeout
        self._cache     : Dict[str, Tuple[float, Optional[float]]] = {}
        self._cache_ttl = 3.0   # segundos

    def fetch_best_ask(self, token_id: str) -> Optional[float]:
        """
        Devuelve el mejor ask (precio más bajo al que alguien vende).
        Usa caché de 3 segundos para no sobrecargar la API.
        """
        now = time.time()
        if token_id in self._cache:
            cached_ts, cached_ask = self._cache[token_id]
            if now - cached_ts < self._cache_ttl:
                return cached_ask

        try:
            resp = requests.get(
                f"{CLOB_HOST}/book",
                params={"token_id": token_id},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            asks = data.get("asks", [])
            if asks:
                # asks vienen ordenados ascendente (mejor = primero)
                best = float(asks[0]["price"])
                self._cache[token_id] = (now, best)
                return best
        except Exception as exc:
            log.debug(f"[PolyBook] best_ask for {token_id[:12]}... failed: {exc}")

        self._cache[token_id] = (now, None)
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  MULTI-SOURCE ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class MultiSourceEngine:
    """
    Motor de señal basado en Binance RSI+VWAP.

    Uso:
      engine = MultiSourceEngine(asset="sol", rsi_period=15, interval="1m")
      preload_history(engine.binance, lookback_candles=25)
      engine.start()
      ...
      sig = engine.get_signal(token_up_id, token_down_id)
      if sig.is_actionable:
          best_price = sig.best_ask_for(sig.direction)

    El método get_signal():
      1. Lee la última señal del SignalEngine (Binance RSI+VWAP)
      2. Consulta el order book de Polymarket para los tokens del mercado activo
         (solo para obtener el best_ask de entrada, no para señal)
      3. Devuelve MultiSourceSignal
    """

    def __init__(
        self,
        asset              : str,
        rsi_period         : int   = 14,
        rsi_overbought     : float = 70.0,
        rsi_oversold       : float = 30.0,
        rsi_bull_threshold : float = 52.0,
        rsi_bear_threshold : float = 48.0,
        min_confidence     : float = 0.55,
        interval           : str   = "1m",
        on_signal          : Optional[Callable[[MultiSourceSignal], None]] = None,
    ):
        self.asset     = asset.lower()
        self.on_signal = on_signal

        # ── Fuente de señal: Binance RSI+VWAP ────────────────────────────────
        self.binance = SignalEngine(
            asset              = asset,
            rsi_period         = rsi_period,
            rsi_overbought     = rsi_overbought,
            rsi_oversold       = rsi_oversold,
            rsi_bull_threshold = rsi_bull_threshold,
            rsi_bear_threshold = rsi_bear_threshold,
            min_confidence     = min_confidence,
            interval           = interval,
        )

        # ── Polymarket order book (solo para best_ask de entrada) ─────────────
        self.poly_book = PolymarketBook()

        log.info(
            f"[{asset.upper()}] MultiSourceEngine init — "
            f"interval={interval}  rsi_period={rsi_period}"
        )

    # ── Ciclo de vida ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Arranca el WebSocket de Binance."""
        self.binance.start()
        log.info(f"[{self.asset.upper()}] MultiSourceEngine started")

    def stop(self) -> None:
        self.binance.stop()
        log.info(f"[{self.asset.upper()}] MultiSourceEngine stopped")

    # ── Señal ──────────────────────────────────────────────────────────────────

    def get_signal(
        self,
        token_up_id  : Optional[str] = None,
        token_down_id: Optional[str] = None,
    ) -> Optional[MultiSourceSignal]:
        """
        Devuelve la señal basada en Binance RSI+VWAP.

        token_up_id / token_down_id: IDs de los tokens del mercado activo.
        Se usan solo para obtener el best_ask de entrada óptima.
        """
        base_sig = self.binance.last_signal
        if base_sig is None:
            return None   # no hay suficientes datos todavía

        # Señal NEUTRAL — sin best_ask
        if base_sig.direction == "NEUTRAL":
            return MultiSourceSignal(
                direction     = "NEUTRAL",
                confidence    = 0.0,
                asset         = self.asset,
                rsi           = base_sig.rsi,
                vwap          = base_sig.vwap,
                binance_price = base_sig.price,
                consensus_count  = 1,
                sources_checked  = 1,
            )

        # Señal UP o DOWN — obtener best_ask para precio de entrada óptimo
        up_best_ask  : Optional[float] = None
        down_best_ask: Optional[float] = None

        if token_up_id:
            up_best_ask = self.poly_book.fetch_best_ask(token_up_id)
        if token_down_id:
            down_best_ask = self.poly_book.fetch_best_ask(token_down_id)

        sig = MultiSourceSignal(
            direction          = base_sig.direction,
            confidence         = base_sig.confidence,
            asset              = self.asset,
            rsi                = base_sig.rsi,
            vwap               = base_sig.vwap,
            binance_price      = base_sig.price,
            poly_up_best_ask   = up_best_ask,
            poly_down_best_ask = down_best_ask,
            consensus_count    = 1,
            sources_checked    = 1,
        )

        log.info(str(sig))
        if self.on_signal and sig.is_actionable:
            self.on_signal(sig)

        return sig

    def wait_ready(self, timeout: float = 120.0) -> bool:
        """Espera hasta que el engine Binance tenga suficientes datos."""
        return self.binance.wait_ready(timeout)
