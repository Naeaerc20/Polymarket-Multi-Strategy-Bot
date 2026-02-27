"""
strategies/RSI_VWAP_Signal/markets/sol/bot.py  ──  v2 Adaptive Interval
──────────────────────────────────────────────────────────────────────
SOL RSI+VWAP v2 — Binance RSI+VWAP con intervalo adaptativo.

CÓMO FUNCIONA (paso a paso)
──────────────────────────────────────────────────────────────────────
1. INTERVALO ADAPTATIVO
   Antes de cada ventana de mercado, se calcula el tiempo restante.
   El RSI se calcula sobre velas de 1m con un periodo = minutos restantes
   (cap entre 3 y 30). Ejemplo: 15m restantes → RSI(15) en velas 1m.

2. PRE-CARGA (por ventana)
   Descarga las últimas rsi_period+10 velas de 1m de Binance REST.
   El RSI está listo inmediatamente sin warmup.

3. MOTOR DE SEÑAL (background thread)
   • Binance WebSocket (klines SOLUSDT 1m):
     calcula RSI (EMA de Wilder) + VWAP diario en cada vela cerrada.
   La señal es exclusivamente de Binance — sin ChainLink.

4. ZONA TEMPORAL ("prime window")
   Al inicio de cada ventana de mercado, el RiskManager clasifica el tiempo:
     PRIME [0 – EARLY_BET_WINDOW_SECS]: mejor precio por share → entrada preferida.
     OK    [PRIME – 50% de la ventana ]: entrada válida si no hubo señal en PRIME.
     LATE  [>50% de la ventana]         : NO nuevas entradas.

5. SEÑAL
   En cada tick se llama a MultiSourceEngine.get_signal(token_up, token_down):
     • Binance RSI+VWAP: dirección + confianza.
     • Polymarket CLOB book: best_ask de cada token (solo para precio de entrada).
   La señal es accionable cuando RSI cruza el umbral bull/bear y precio > VWAP.

6. ENTRADA  (precio óptimo)
   Cuando hay señal válida + zona OK/PRIME:
     a. Consulta el best_ask del CLOB para el token elegido.
     b. Usa ese precio en vez del midpoint para entrar al mejor precio disponible.
     c. Coloca BUY (FAK por defecto).
     d. Registra la posición en el RiskManager.

6. GESTIÓN DE RIESGO POST-ENTRADA
   El RiskManager evalúa en cada tick:
     • STOP-LOSS: si el precio cae ≥ RISK_STOP_LOSS_PCT bajo el precio de entrada
       → venta inmediata (FAK).
     • HEDGE: si el precio cae ≥ RISK_HEDGE_TRIGGER_PCT (mayor pérdida)
       → compra del lado contrario con el mismo amount para cubrir riesgo.
     • TP / SL bracket GTC: se colocan opcionalmente (AUTOSET_UP_TP_SL_ORDERS).

7. GESTIÓN DE VENTANAS
   Cada ventana (5m / 15m / 1h) es independiente.
   Máximo 1 entrada por dirección por ventana.
   Al cerrar la ventana, se cancelan órdenes abiertas y se espera la siguiente.

INTERVALOS SOPORTADOS
  5m  → slug: sol-updown-5m-{unix_ts}
  15m → slug: sol-updown-15m-{unix_ts}
  1h  → slug de evento ET: solana-up-or-down-{mes}-{dia}-{hora}{am/pm}-et

VARIABLES .env (SOL-específicas)
──────────────────────────────────────────────────────────────────────
  SOL_RSI_VWAP_PRICE_RANGE    Rango de precio del token para entrar (ej. "0.30-0.55")
  SOL_RSI_VWAP_AMOUNT         USDC a gastar por BET (default 1.0)
  SOL_RSI_VWAP_TAKE_PROFIT    Precio TP para bracket GTC (ej. 0.80)
  SOL_RSI_VWAP_STOP_LOSS      Precio SL fijo para bracket GTC (ej. 0.40)
  SOL_RSI_VWAP_POLL_INTERVAL  Segundos entre ticks (default 0.5)

VARIABLES .env (compartidas RSI_VWAP v2)
  RSI_PERIOD, RSI_OVERBOUGHT, RSI_OVERSOLD
  RSI_BULL_THRESHOLD, RSI_BEAR_THRESHOLD
  MIN_SIGNAL_CONFIDENCE
  EARLY_BET_WINDOW_SECS        Zona PRIME en segundos (default 90)
  EARLY_BET_ONLY               true = solo entrar en PRIME (default false)
  RISK_STOP_LOSS_ENABLED       true | false (default true)
  RISK_STOP_LOSS_PCT           % caída para SL dinámico (default 0.04)
  RISK_HEDGE_ENABLED           true | false (default false)
  RISK_HEDGE_TRIGGER_PCT       % caída para hedge (default 0.06)
  RISK_TIME_DECAY_MAX_PCT      % ventana máx. para nuevas entradas (default 0.50)
  AUTOSET_UP_TP_SL_ORDERS      true = colocar bracket GTC después del BUY
  SOL_RSI_VWAP_DRY_RUN         true = simular órdenes sin fondos reales (default false)

DRY-RUN
──────────────────────────────────────────────────────────────────────
Activar con:
  SOL_RSI_VWAP_DRY_RUN=true  en .env
  python main.py --strategy rsivwap --operate sol --interval 5m --dry-run
  python strategies/RSI_VWAP_Signal/markets/sol/bot.py  (pregunta al inicio)

En modo dry-run:
  • Las señales, consenso y zonas PRIME/OK/LATE se calculan igual.
  • El RiskManager evalúa stop-loss y hedge igual.
  • Ninguna orden real se envía al CLOB.
  • Cada acción se registra con el prefijo [DRY RUN].
  • El estado del bot se actualiza como si las órdenes hubieran sido ejecutadas,
    lo que permite simular el flujo completo de una sesión.
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


# ── Path resolution ───────────────────────────────────────────────────────────
def _find_dir_with(marker: str) -> Path:
    p = Path(__file__).resolve().parent
    for _ in range(12):
        if (p / marker).exists():
            return p
        p = p.parent
    raise FileNotFoundError(
        f"No se encontró '{marker}' en ningún directorio padre de {__file__}"
    )

_ROOT            = _find_dir_with("order_executor.py")
_SIGNAL_ENGINE_DIR = _find_dir_with("signal_engine.py")

load_dotenv(_ROOT / ".env")

for _p in [str(_ROOT), str(_SIGNAL_ENGINE_DIR)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

from order_executor     import OrderExecutor
from market_stream      import MarketStream
from signal_engine      import preload_history
from signal_engine_v2   import MultiSourceEngine, MultiSourceSignal
from risk_manager       import RiskManager, RiskEvent, WindowZone

logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s][%(levelname)s] - %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("SOL-RSI-VWAP-v2")


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS DE CONFIGURACIÓN
# ══════════════════════════════════════════════════════════════════════════════

def _parse_range(raw: str):
    parts = [p.strip() for p in raw.split("-")]
    return float(parts[0]), float(parts[1])

def _float(key, default):
    v = os.getenv(key, "").strip().lower()
    return float(v) if v not in ("", "null", "none") else default

def _int(key, default):
    v = os.getenv(key, "").strip().lower()
    return int(v) if v not in ("", "null", "none") else default

def _bool(key, default):
    v = os.getenv(key, "").strip().lower()
    if v in ("", "null", "none"): return default
    return v not in ("false", "0", "no", "off")

def _optional_float(key) -> Optional[float]:
    v = os.getenv(key, "").strip().lower()
    return None if v in ("", "null", "none") else float(v)


# ══════════════════════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════════════════════

# ── SOL-específico ────────────────────────────────────────────────────────────
PRICE_RANGE    = _parse_range(os.getenv("SOL_RSI_VWAP_PRICE_RANGE", "0.30-0.55"))
AMOUNT_TO_BUY  = _float("SOL_RSI_VWAP_AMOUNT",         1.0)
TAKE_PROFIT    = _optional_float("SOL_RSI_VWAP_TAKE_PROFIT")    # ej. 0.80
BRACKET_SL     = _optional_float("SOL_RSI_VWAP_STOP_LOSS")      # bracket GTC fijo
POLL_INTERVAL  = _float("SOL_RSI_VWAP_POLL_INTERVAL",  0.5)

# ── RSI + VWAP (Binance) ──────────────────────────────────────────────────────
RSI_PERIOD          = _int(  "RSI_PERIOD",           14)
RSI_OVERBOUGHT      = _float("RSI_OVERBOUGHT",       70.0)
RSI_OVERSOLD        = _float("RSI_OVERSOLD",         30.0)
RSI_BULL_THRESHOLD  = _float("RSI_BULL_THRESHOLD",   52.0)
RSI_BEAR_THRESHOLD  = _float("RSI_BEAR_THRESHOLD",   48.0)
MIN_CONFIDENCE      = _float("MIN_SIGNAL_CONFIDENCE", 0.55)
# CANDLE_INTERVAL es solo referencia; el bot usa siempre "1m" con periodo adaptativo

# ── Early-window BET ──────────────────────────────────────────────────────────
EARLY_BET_SECS  = _float("EARLY_BET_WINDOW_SECS",    90.0)
EARLY_BET_ONLY  = _bool( "EARLY_BET_ONLY",           False)

# ── Multi-entry DCA ───────────────────────────────────────────────────────────
MAX_BETS_PER_SIDE = _int(  "MAX_BETS_PER_SIDE",   5)      # máx entradas por dirección
BET_COOLDOWN_SECS = _float("BET_COOLDOWN_SECS",   90.0)   # segundos mínimos entre BETs

# ── Risk management ───────────────────────────────────────────────────────────
RISK_SL_ENABLED = _bool( "RISK_STOP_LOSS_ENABLED",   True)
RISK_SL_PCT     = _float("RISK_STOP_LOSS_PCT",        0.04)
RISK_HEDGE_EN   = _bool( "RISK_HEDGE_ENABLED",        False)
RISK_HEDGE_PCT  = _float("RISK_HEDGE_TRIGGER_PCT",    0.06)
RISK_TIME_PCT   = _float("RISK_TIME_DECAY_MAX_PCT",   0.50)

# ── Orden / infra ─────────────────────────────────────────────────────────────
BUY_ORDER_TYPE    = (os.getenv("BUY_ORDER_TYPE") or "FAK").upper()
AUTOSET_BRACKETS  = _bool("AUTOSET_UP_TP_SL_ORDERS", True)
WSS_READY_TIMEOUT = _float("WSS_READY_TIMEOUT",       10.0)
DRY_RUN           = _bool("SOL_RSI_VWAP_DRY_RUN",    False)

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API  = "https://data-api.polymarket.com"
CHAIN_ID  = 137

WINDOW_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}


# ══════════════════════════════════════════════════════════════════════════════
#  ADAPTIVE INTERVAL
# ══════════════════════════════════════════════════════════════════════════════

def compute_adaptive_interval(time_left_secs: float):
    """
    Calcula el intervalo adaptativo basado en el tiempo restante.

    Siempre usa velas de 1m. El periodo de RSI = minutos restantes,
    capado entre 3 y 30 para estabilidad numérica.

    Ejemplo: 15m restantes → ("1m", 15) → RSI mide los últimos 15 minutos.
             9m restantes  → ("1m", 9)
             60m restantes → ("1m", 30) [cap]
    """
    mins   = int(time_left_secs / 60)
    period = max(3, min(30, mins))
    return "1m", period


# ══════════════════════════════════════════════════════════════════════════════
#  CLOB CLIENT
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
#  DRY-RUN HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _dry_buy(direction: str, token_id: str, price: float, usdc: float) -> dict:
    """Simula un BUY: loguea la orden y devuelve una respuesta de éxito falsa."""
    shares = round(usdc / price, 4)
    log.info(
        f"  [DRY RUN] BUY {direction} — "
        f"token={token_id[:16]}...  price={price:.4f}  "
        f"usdc=${usdc:.2f}  ~shares={shares:.4f}"
    )
    return {
        "success":      True,
        "dry_run":      True,
        "takingAmount": str(shares),
        "makingAmount": str(usdc),
    }

def _dry_sell(action: str, token_id: str, shares: float, price: float) -> dict:
    """Simula un SELL: loguea la acción y devuelve respuesta de éxito falsa."""
    log.info(
        f"  [DRY RUN] SELL {action} — "
        f"token={token_id[:16]}...  shares={shares:.4f}  price={price:.4f}"
    )
    return {"success": True, "dry_run": True}

def _dry_bracket(direction: str, token_id: str, shares: float,
                 tp: Optional[float], sl: Optional[float]) -> dict:
    """Simula colocación de brackets TP+SL."""
    log.info(
        f"  [DRY RUN] BRACKETS {direction} — "
        f"shares={shares:.4f}  TP={tp}  SL={sl}"
    )
    return {"tp_order_id": "DRY_TP", "sl_order_id": "DRY_SL" if sl else None}


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
        log.error("Credenciales incompletas — ejecuta setup.py primero")
        sys.exit(1)

    creds  = ApiCreds(api_key=key, api_secret=sec, api_passphrase=pas)
    client = ClobClient(
        host=CLOB_HOST, key=pk, chain_id=CHAIN_ID,
        creds=creds, signature_type=sig, funder=fund,
    )
    client.set_api_creds(creds)
    return client


# ══════════════════════════════════════════════════════════════════════════════
#  MARKET DISCOVERY  (5m / 15m / 1h)
# ══════════════════════════════════════════════════════════════════════════════

def _et_offset(month: int) -> int:
    return -4 if 3 <= month <= 11 else -5

def _build_1h_event_slug(dt_utc: datetime) -> str:
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
        log.warning(f"Gamma API {endpoint}: {exc}")
        return None

def _find_market_5m_15m(interval: str) -> Optional[dict]:
    templates = {"5m": "sol-updown-5m-{ts}", "15m": "sol-updown-15m-{ts}"}
    window    = WINDOW_SECONDS[interval]
    ts        = (int(datetime.now(timezone.utc).timestamp()) // window) * window
    for candidate in [ts, ts + window]:
        slug = templates[interval].format(ts=candidate)
        data = _gamma_get("markets", {"slug": slug})
        if not data:
            continue
        for m in (data if isinstance(data, list) else [data]):
            if m.get("active") and not m.get("closed"):
                log.info(f"  Mercado encontrado: {slug}")
                return m
    return None

def _find_market_1h() -> Optional[dict]:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for h_delta in [0, 1, -1]:
        dt_try     = now + timedelta(hours=h_delta)
        event_slug = _build_1h_event_slug(dt_try)
        log.info(f"  Probando slug 1h: {event_slug}")

        data = _gamma_get("events", {"slug": event_slug})
        if data:
            for event in (data if isinstance(data, list) else [data]):
                for m in (event.get("markets") or []):
                    if m.get("active") and not m.get("closed"):
                        log.info(f"  Encontrado via /events: {event_slug}")
                        return m

        data2 = _gamma_get("markets", {"event_slug": event_slug})
        if data2:
            for m in (data2 if isinstance(data2, list) else [data2]):
                if m.get("active") and not m.get("closed"):
                    log.info(f"  Encontrado via /markets event_slug: {event_slug}")
                    return m
    return None

def wait_for_active_market(interval: str) -> dict:
    log.info(f"Buscando mercado activo SOL {interval.upper()} ...")
    while True:
        m = _find_market_1h() if interval == "1h" else _find_market_5m_15m(interval)
        if m:
            log.info(f"  Market ID : {m.get('id', '?')}")
            log.info(f"  End time  : {m.get('endDate') or m.get('end_date_iso', '?')}")
            return m
        log.info("  Sin mercado activo — reintentando en 20s ...")
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
        result[key] = {"token_id": tokens[i], "price": prices[i]}
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
#  PRICE FEED
# ══════════════════════════════════════════════════════════════════════════════

def _rest_mid(token_id: str) -> Optional[float]:
    try:
        r = requests.get(
            f"{CLOB_HOST}/midpoint",
            params={"token_id": token_id}, timeout=5
        )
        r.raise_for_status()
        return float(r.json()["mid"])
    except Exception:
        return None

def get_token_price(stream: MarketStream, token_id: str) -> Optional[float]:
    return stream.get_midpoint(token_id) or _rest_mid(token_id)


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION QUERY  (shares reales on-chain)
# ══════════════════════════════════════════════════════════════════════════════

def get_real_position(token_id: str, condition_id: Optional[str] = None) -> Optional[float]:
    owner = os.getenv("FUNDER_ADDRESS", "")
    if not owner:
        return None

    def _parse(positions: list) -> Optional[float]:
        for pos in positions:
            if str(pos.get("asset_id", "")) == str(token_id):
                raw = pos.get("size") or pos.get("balance") or 0
                return float(
                    Decimal(str(raw)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
                )
        return None

    if condition_id:
        try:
            r = requests.get(
                f"{DATA_API}/positions",
                params={"user": owner, "sizeThreshold": "0.01",
                        "market": condition_id, "limit": "200"},
                timeout=8,
            )
            r.raise_for_status()
            result = _parse(r.json() if isinstance(r.json(), list) else [])
            if result is not None:
                return result
        except Exception as exc:
            log.warning(f"[pos] Data API (filtro): {exc}")

    try:
        r = requests.get(
            f"{DATA_API}/positions",
            params={"user": owner, "sizeThreshold": "0.01", "limit": "200"},
            timeout=8,
        )
        r.raise_for_status()
        result = _parse(r.json() if isinstance(r.json(), list) else [])
        return result if result is not None else 0.0
    except Exception as exc:
        log.warning(f"[pos] Data API (global): {exc}")

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
        log.warning(f"[pos] CLOB balance-allowance: {exc}")
    return None


def _parse_buy_result(resp: dict, fb_price: float, fb_usdc: float):
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
        self.up_bet_count    : int             = 0
        self.dn_bet_count    : int             = 0
        self._last_up_bet_ts : float           = 0.0
        self._last_dn_bet_ts : float           = 0.0
        self.up_token_id   : Optional[str]     = None
        self.down_token_id : Optional[str]     = None
        self.condition_id  : Optional[str]     = None
        self.up_entry_price: float             = 0.0
        self.dn_entry_price: float             = 0.0
        self.up_shares     : float             = 0.0
        self.dn_shares     : float             = 0.0
        self.up_cost       : float             = 0.0
        self.dn_cost       : float             = 0.0
        self.up_signal     : Optional[MultiSourceSignal] = None
        self.dn_signal     : Optional[MultiSourceSignal] = None
        self.tp_order_id_up: Optional[str]     = None
        self.sl_order_id_up: Optional[str]     = None
        self.tp_order_id_dn: Optional[str]     = None
        self.sl_order_id_dn: Optional[str]     = None
        self.hedge_placed  : bool              = False

    @property
    def bought_up(self) -> bool:
        return self.up_bet_count > 0

    @property
    def bought_down(self) -> bool:
        return self.dn_bet_count > 0

    def bet_count(self, direction: str) -> int:
        return self.up_bet_count if direction == "UP" else self.dn_bet_count

    def last_bet_ts(self, direction: str) -> float:
        return self._last_up_bet_ts if direction == "UP" else self._last_dn_bet_ts

    def update_after_buy(
        self,
        direction : str,
        price     : float,
        shares    : float,
        cost      : float,
        signal    : MultiSourceSignal,
        token_id  : str,
    ):
        now = time.time()
        if direction == "UP":
            self.up_bet_count   += 1
            self._last_up_bet_ts = now
            self.up_cost        += cost
            self.up_shares      += shares
            self.up_entry_price  = self.up_cost / self.up_shares
            self.up_signal       = signal
            self.up_token_id     = token_id
        else:
            self.dn_bet_count   += 1
            self._last_dn_bet_ts = now
            self.dn_cost        += cost
            self.dn_shares      += shares
            self.dn_entry_price  = self.dn_cost / self.dn_shares
            self.dn_signal       = signal
            self.down_token_id   = token_id

    def summary(self) -> str:
        lines = ["SOL RSI+VWAP v2 — posición actual:"]
        if self.bought_up:
            s = self.up_signal
            lines.append(
                f"  UP   @ {self.up_entry_price:.4f} (avg, {self.up_bet_count} BETs)  "
                f"shares={self.up_shares:.4f}  cost=${self.up_cost:.4f}  "
                f"[conf={s.confidence:.2f}  consensus={s.consensus_count}/{s.sources_checked}  "
                f"RSI={s.rsi:.1f}  CL={f'{s.chainlink_price:.2f}' if s.chainlink_price else 'N/A'}]"
            )
        if self.bought_down:
            s = self.dn_signal
            lines.append(
                f"  DOWN @ {self.dn_entry_price:.4f} (avg, {self.dn_bet_count} BETs)  "
                f"shares={self.dn_shares:.4f}  cost=${self.dn_cost:.4f}  "
                f"[conf={s.confidence:.2f}  consensus={s.consensus_count}/{s.sources_checked}  "
                f"RSI={s.rsi:.1f}  CL={f'{s.chainlink_price:.2f}' if s.chainlink_price else 'N/A'}]"
            )
        return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
#  BRACKET ORDERS  (TP + SL GTC)
# ══════════════════════════════════════════════════════════════════════════════

def place_brackets_for(
    direction  : str,
    state      : BotState,
    executor   : OrderExecutor,
    tick_size  : float,
):
    """Coloca TP y SL GTC para la posición dada."""
    if not AUTOSET_BRACKETS:
        log.info(f"  AUTOSET=false — brackets no colocados para {direction}")
        return
    if TAKE_PROFIT is None:
        log.info(f"  SOL_RSI_VWAP_TAKE_PROFIT no configurado — skipping bracket TP")
        return

    token_id = state.up_token_id if direction == "UP" else state.down_token_id
    shares   = state.up_shares   if direction == "UP" else state.dn_shares
    entry    = state.up_entry_price if direction == "UP" else state.dn_entry_price
    cid      = state.condition_id

    if not token_id or shares < 0.0001:
        return

    # En dry-run no consultamos posición ni enviamos órdenes reales
    if DRY_RUN:
        result = _dry_bracket(direction, token_id, shares, TAKE_PROFIT, BRACKET_SL)
    else:
        # Verificar shares on-chain (hasta 5 intentos)
        real = None
        for attempt in range(1, 6):
            real = get_real_position(token_id, cid)
            if real is not None and real >= 0.0001:
                log.info(f"  [brackets {direction}] shares on-chain: {real:.4f} ✔")
                break
            log.info(f"  [brackets {direction}] esperando liquidación ({attempt}/5) ...")
            time.sleep(1)

        if real is None or real < 0.0001:
            real = shares
            log.warning(f"  [brackets {direction}] usando estimado: {real:.4f}")

        result = executor.place_sell_bracket(
            token_id     = token_id,
            total_shares = real,
            tp_price     = TAKE_PROFIT,
            sl_price     = BRACKET_SL,
            tick_size    = tick_size,
        )

    if direction == "UP":
        state.tp_order_id_up = result.get("tp_order_id")
        state.sl_order_id_up = result.get("sl_order_id")
    else:
        state.tp_order_id_dn = result.get("tp_order_id")
        state.sl_order_id_dn = result.get("sl_order_id")

    log.info(
        f"  [brackets {direction}] TP={TAKE_PROFIT}  "
        f"SL={BRACKET_SL}  shares={real:.4f}"
    )


def is_order_open(client, order_id: str) -> bool:
    try:
        resp   = client.get_order(order_id)
        status = (resp or {}).get("status", "").upper()
        return status in ("OPEN", "LIVE", "UNMATCHED", "PENDING")
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  MAIN WINDOW LOOP
# ══════════════════════════════════════════════════════════════════════════════

def run_window(
    market   : dict,
    executor : OrderExecutor,
    engine   : MultiSourceEngine,
    interval : str,
):
    tokens       = parse_market_tokens(market)
    end_time     = get_market_end_time(market)
    token_up     = tokens["UP"]["token_id"]
    token_down   = tokens["DOWN"]["token_id"]
    tick_up      = get_tick_size_rest(executor.client, token_up)
    tick_down    = get_tick_size_rest(executor.client, token_down)
    condition_id = (
        market.get("conditionId")
        or market.get("condition_id")
        or market.get("questionID")
        or market.get("id")
    )

    window_secs  = WINDOW_SECONDS.get(interval, 300)
    range_lo, range_hi = PRICE_RANGE

    state = BotState()
    state.condition_id  = condition_id
    state.up_token_id   = token_up
    state.down_token_id = token_down

    rm = RiskManager(
        window_secs           = window_secs,
        stop_loss_enabled     = RISK_SL_ENABLED,
        stop_loss_pct         = RISK_SL_PCT,
        hedge_enabled         = RISK_HEDGE_EN,
        hedge_trigger_pct     = RISK_HEDGE_PCT,
        time_decay_max_pct    = RISK_TIME_PCT,
        early_bet_window_secs = EARLY_BET_SECS,
        early_bet_only        = EARLY_BET_ONLY,
    )
    rm.open_window()

    log.info("=" * 65)
    log.info(f"  SOL RSI+VWAP v2 | Intervalo={interval.upper()}")
    log.info(f"  Market ID  : {market.get('id', '?')}")
    log.info(f"  Condition  : {condition_id}")
    log.info(f"  End time   : {end_time}")
    log.info(f"  PRICE_RANGE: {range_lo:.2f}–{range_hi:.2f}")
    log.info(f"  AMOUNT     : ${AMOUNT_TO_BUY:.2f}  TP={TAKE_PROFIT}  SL_bracket={BRACKET_SL}")
    log.info(f"  ORDER      : {BUY_ORDER_TYPE}  AUTOSET={AUTOSET_BRACKETS}")
    log.info(f"  PRIME_ZONE : {EARLY_BET_SECS:.0f}s  EARLY_ONLY={EARLY_BET_ONLY}")
    log.info(f"  RISK_SL    : {RISK_SL_ENABLED}({RISK_SL_PCT*100:.1f}%)")
    log.info(f"  RISK_HEDGE : {RISK_HEDGE_EN}({RISK_HEDGE_PCT*100:.1f}%)")
    log.info(f"  MIN_CONS   : {MIN_CONSENSUS}  CANDLE={CANDLE_INTERVAL}")
    log.info("=" * 65)

    # ── Abrir stream de precios Polymarket ────────────────────────────────────
    stream = MarketStream(asset_ids=[token_up, token_down])
    stream.start()
    ready = stream.wait_ready(timeout=WSS_READY_TIMEOUT)
    if ready:
        tick_up   = stream.get_tick_size(token_up)   or tick_up
        tick_down = stream.get_tick_size(token_down) or tick_down
        log.info("[Poly WSS] Conectado")
    else:
        log.warning(f"[Poly WSS] No disponible tras {WSS_READY_TIMEOUT}s — usando REST")

    _tick_ctr = 0

    try:
        while True:
            now = datetime.now(timezone.utc)
            if end_time and now >= end_time:
                log.info("Ventana cerrada — cancelando órdenes abiertas.")
                executor.gtc_tracker.cancel_all(log)
                break

            time_left  = (end_time - now).total_seconds() if end_time else 9999
            mins, secs = divmod(int(time_left), 60)
            zone_label = rm.zone_label()

            # ── Precios actuales ──────────────────────────────────────────────
            up_price   = get_token_price(stream, token_up)
            down_price = get_token_price(stream, token_down)

            if up_price is None or down_price is None:
                log.warning(f"[{mins:02d}:{secs:02d}] Precio no disponible — esperando ...")
                time.sleep(POLL_INTERVAL)
                continue

            # ── Señal multi-fuente ────────────────────────────────────────────
            signal = engine.get_signal(token_up, token_down)

            # ─────────────────────────────────────────────────────────────────
            #  FASE 1 — Sin posición → evaluar entrada
            # ─────────────────────────────────────────────────────────────────
            both_bought = state.bought_up and state.bought_down
            if not both_bought:
                can_enter = rm.can_enter()
                zone      = rm.get_zone()

                # Log de estado
                if signal:
                    rsi_s  = f"RSI={signal.rsi:.1f}"
                    sig_s  = f"sig={signal.direction}"
                    con_s  = f"conf={signal.confidence:.2f}"
                    con2_s = f"cons={signal.consensus_count}/{signal.sources_checked}"
                    cl_s   = (f"CL={signal.chainlink_direction}"
                              if signal.chainlink_available else "CL=off")
                else:
                    rsi_s = sig_s = con_s = con2_s = cl_s = "warming"

                log.info(
                    f"[{mins:02d}:{secs:02d}] [{zone_label}]  "
                    f"UP={up_price:.4f}  DOWN={down_price:.4f}  "
                    f"{rsi_s}  {sig_s}  {con_s}  {con2_s}  {cl_s}"
                )

                if not can_enter:
                    if zone == WindowZone.LATE:
                        log.info(f"  LATE ZONE — no nuevas entradas hasta siguiente ventana")
                    time.sleep(POLL_INTERVAL)
                    continue

                # ── Evaluar señal y colocar BET ───────────────────────────────
                if (
                    signal is not None
                    and signal.is_actionable
                    and signal.confidence >= MIN_CONFIDENCE
                ):
                    direction   = signal.direction
                    token_id    = token_up if direction == "UP" else token_down
                    token_price = up_price if direction == "UP" else down_price
                    tick_size   = tick_up  if direction == "UP" else tick_down
                    bet_count = state.bet_count(direction)
                    last_ts   = state.last_bet_ts(direction)

                    if bet_count >= MAX_BETS_PER_SIDE:
                        # Límite de entradas alcanzado para esta dirección
                        time.sleep(POLL_INTERVAL)
                        continue

                    if bet_count > 0 and (time.time() - last_ts) < BET_COOLDOWN_SECS:
                        # Cooldown entre BETs no expirado
                        time.sleep(POLL_INTERVAL)
                        continue

                    if not (range_lo <= token_price <= range_hi):
                        log.info(
                            f"  [{direction}] precio {token_price:.4f} fuera de rango "
                            f"{range_lo:.2f}–{range_hi:.2f} — saltando"
                        )
                        time.sleep(POLL_INTERVAL)
                        continue

                    # Obtener best ask para precio de entrada óptimo
                    best_ask    = signal.best_ask_for(direction)
                    entry_price = best_ask if best_ask else token_price

                    # Asegurar que el best_ask también está en el rango
                    if not (range_lo <= entry_price <= range_hi):
                        entry_price = token_price

                    bet_num  = state.bet_count(direction) + 1
                    zone_tag = "★PRIME★" if zone == WindowZone.PRIME else "OK"
                    log.info(
                        f"*** [{zone_tag}] BET #{bet_num}/{MAX_BETS_PER_SIDE} {direction}  "
                        f"best_ask={entry_price:.4f}  mid={token_price:.4f}  "
                        f"RSI={signal.rsi:.1f}  VWAP={signal.vwap:.4f}  "
                        f"conf={signal.confidence:.2f}  "
                        f"consensus={signal.consensus_count}/{signal.sources_checked}  "
                        f"CL_dir={signal.chainlink_direction}  "
                        f"poly_dir={signal.poly_direction} ***"
                    )

                    if DRY_RUN:
                        resp = _dry_buy(direction, token_id, entry_price, AMOUNT_TO_BUY)
                    else:
                        resp = executor.place_buy(
                            token_id  = token_id,
                            price     = entry_price,
                            usdc_size = AMOUNT_TO_BUY,
                            tick_size = tick_size,
                        )

                    if resp and resp.get("success"):
                        shares, cost = _parse_buy_result(resp, entry_price, AMOUNT_TO_BUY)
                        state.update_after_buy(direction, entry_price, shares, cost, signal, token_id)
                        avg_entry = (state.up_entry_price if direction == "UP"
                                     else state.dn_entry_price)
                        if state.bet_count(direction) == 1:
                            rm.set_position(avg_entry=avg_entry)
                        else:
                            rm.update_avg_entry(avg_entry)
                        log.info(
                            f"  ✔ {direction} comprado | "
                            f"shares={shares:.4f}  cost=${cost:.4f}  "
                            f"entry={entry_price:.4f}"
                        )
                        log.info(state.summary())

                        if AUTOSET_BRACKETS:
                            if state.bet_count(direction) > 1:
                                # Cancelar brackets previos antes de re-colocar con total de shares
                                if direction == "UP":
                                    for oid in [state.tp_order_id_up, state.sl_order_id_up]:
                                        if oid:
                                            try: executor.client.cancel_order(oid)
                                            except Exception: pass
                                    state.tp_order_id_up = None
                                    state.sl_order_id_up = None
                                else:
                                    for oid in [state.tp_order_id_dn, state.sl_order_id_dn]:
                                        if oid:
                                            try: executor.client.cancel_order(oid)
                                            except Exception: pass
                                    state.tp_order_id_dn = None
                                    state.sl_order_id_dn = None
                            place_brackets_for(direction, state, executor, tick_size)
                    else:
                        log.error(f"  ✗ BET {direction} falló | resp={resp}")

            # ─────────────────────────────────────────────────────────────────
            #  FASE 2 — En posición → gestión de riesgo
            # ─────────────────────────────────────────────────────────────────
            else:
                log.info(
                    f"[{mins:02d}:{secs:02d}] Ambos lados comprados — "
                    f"UP={up_price:.4f}  DOWN={down_price:.4f}  "
                    f"[{zone_label}]"
                )
                time.sleep(POLL_INTERVAL)
                continue

            # ── Gestión de riesgo para la posición UP (si está abierta) ──────
            if state.bought_up:
                cp        = up_price
                tick_size = tick_up
                risk_st   = rm.check_position(cp)

                _tick_ctr += 1

                # Detección silenciosa de bracket fills (cada 8 ticks)
                if _tick_ctr >= 8:
                    _tick_ctr = 0
                    if state.tp_order_id_up and not is_order_open(executor.client, state.tp_order_id_up):
                        pnl = (TAKE_PROFIT - state.up_entry_price) * state.up_shares if TAKE_PROFIT else 0
                        log.info(f"*** TP UP FILLED (detectado) | est P&L=+${pnl:.4f} ***")
                        executor.gtc_tracker.cancel_all(log)
                        break
                    if state.sl_order_id_up and not is_order_open(executor.client, state.sl_order_id_up):
                        log.info("*** SL UP FILLED (detectado) ***")
                        executor.gtc_tracker.cancel_all(log)
                        break

                # TP manual (sin bracket)
                if TAKE_PROFIT and not state.tp_order_id_up and cp >= TAKE_PROFIT:
                    log.info(f"*** TP manual UP: {cp:.4f} >= {TAKE_PROFIT} ***")
                    if not DRY_RUN:
                        executor.gtc_tracker.cancel_all(log)
                        real = get_real_position(state.up_token_id, condition_id)
                        sell = real if (real and real > 0) else state.up_shares
                        executor.place_sell_immediate(state.up_token_id, sell, cp, tick_size)
                    else:
                        _dry_sell("TP manual", state.up_token_id, state.up_shares, cp)
                    break

                # Stop-loss dinámico post-entrada
                if risk_st.event == RiskEvent.STOP_LOSS:
                    log.warning(
                        f"*** RISK SL UP — price={cp:.4f}  "
                        f"SL_level={risk_st.sl_level:.4f}  "
                        f"P&L={risk_st.pnl_pct*100:.2f}% ***"
                    )
                    if DRY_RUN:
                        _dry_sell("STOP LOSS", state.up_token_id, state.up_shares, cp)
                        log.info(f"  [DRY RUN] UP cerrado por stop-loss | price={cp:.4f}")
                        break
                    executor.gtc_tracker.cancel_all(log)
                    real = get_real_position(state.up_token_id, condition_id)
                    sell = real if (real and real > 0) else state.up_shares
                    resp = executor.place_sell_immediate(state.up_token_id, sell, cp, tick_size)
                    if resp:
                        log.info(f"  ✔ UP cerrado por stop-loss | price={cp:.4f}")
                        break
                    time.sleep(POLL_INTERVAL)
                    continue

                # Hedge automático
                if (
                    risk_st.event == RiskEvent.HEDGE
                    and not state.hedge_placed
                    and not state.bought_down
                ):
                    log.warning(
                        f"*** HEDGE triggered UP — comprando DOWN como cobertura  "
                        f"price={down_price:.4f}  "
                        f"P&L={risk_st.pnl_pct*100:.2f}% ***"
                    )
                    if range_lo <= down_price <= range_hi:
                        if DRY_RUN:
                            resp_h = _dry_buy("DOWN (hedge)", token_down, down_price, AMOUNT_TO_BUY)
                        else:
                            resp_h = executor.place_buy(
                                token_id  = token_down,
                                price     = down_price,
                                usdc_size = AMOUNT_TO_BUY,
                                tick_size = tick_down,
                            )
                        if resp_h and resp_h.get("success"):
                            shares_h, cost_h = _parse_buy_result(resp_h, down_price, AMOUNT_TO_BUY)
                            if signal:
                                state.update_after_buy(
                                    "DOWN", down_price, shares_h, cost_h, signal, token_down
                                )
                            state.hedge_placed = True
                            log.info(
                                f"  ✔ HEDGE DOWN comprado | "
                                f"shares={shares_h:.4f}  cost=${cost_h:.4f}"
                            )
                    else:
                        log.warning(
                            f"  Hedge cancelado — DOWN={down_price:.4f} fuera de rango "
                            f"{range_lo:.2f}–{range_hi:.2f}"
                        )

            time.sleep(POLL_INTERVAL)

    finally:
        stream.stop()
        log.info("[Poly WSS] Stream cerrado.")

    if state.bought_up or state.bought_down:
        log.info(state.summary())


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run(interval: Optional[str] = None, dry_run: Optional[bool] = None):
    # El parámetro dry_run (de main.py --dry-run) sobreescribe la variable de entorno
    global DRY_RUN
    if dry_run is not None:
        DRY_RUN = dry_run

    if interval is None:
        try:
            import questionary
            choice = questionary.select(
                "Selecciona el intervalo de mercado:",
                choices=["5 minutos", "15 minutos", "1 hora"],
            ).ask()
            if choice is None:
                sys.exit(0)
            interval = "1h" if "hora" in choice else "15m" if "15" in choice else "5m"
        except (ImportError, Exception):
            while True:
                raw = input("Intervalo — escribe 5, 15, o 60: ").strip()
                if raw == "5":  interval = "5m";  break
                if raw == "15": interval = "15m"; break
                if raw == "60": interval = "1h";  break

    # Preguntar dry-run si se ejecuta directamente y no se especificó
    if dry_run is None and not os.getenv("SOL_RSI_VWAP_DRY_RUN"):
        try:
            import questionary
            resp = questionary.confirm(
                "¿Ejecutar en modo DRY RUN? (sin órdenes reales)",
                default=False,
            ).ask()
            if resp is not None:
                DRY_RUN = resp
        except (ImportError, Exception):
            raw = input("DRY RUN? [s/N]: ").strip().lower()
            DRY_RUN = raw in ("s", "y", "si", "yes", "1")

    rpc_url = os.getenv("POLY_RPC", "")
    dry_tag = "  ⚠  DRY RUN — NO se enviarán órdenes reales" if DRY_RUN else ""

    log.info("=" * 65)
    log.info("SOL RSI+VWAP v2 — Multi-Source Strategy")
    if DRY_RUN:
        log.info("  *** DRY RUN — SIMULACIÓN SIN FONDOS REALES ***")
    log.info(f"  Mercado   : SOL {interval.upper()}")
    log.info(f"  Velas     : {CANDLE_INTERVAL}")
    log.info(f"  RSI       : periodo={RSI_PERIOD}  OB={RSI_OVERBOUGHT}  OS={RSI_OVERSOLD}")
    log.info(f"  Umbrales  : bull>{RSI_BULL_THRESHOLD}  bear<{RSI_BEAR_THRESHOLD}")
    log.info(f"  Conf min  : {MIN_CONFIDENCE}")
    log.info(f"  ChainLink : {'ACTIVO' if CL_ENABLED and rpc_url else 'INACTIVO (sin POLY_RPC)'}")
    log.info(f"  Consenso  : min {MIN_CONSENSUS} fuentes")
    log.info(f"  Rango     : {PRICE_RANGE[0]:.2f}–{PRICE_RANGE[1]:.2f}")
    log.info(f"  Amount    : ${AMOUNT_TO_BUY:.2f}")
    log.info(f"  DRY RUN   : {DRY_RUN}")
    log.info("=" * 65)

    # ── Construir MultiSourceEngine ───────────────────────────────────────────
    engine = MultiSourceEngine(
        asset              = "sol",
        rpc_url            = rpc_url,
        rsi_period         = RSI_PERIOD,
        rsi_overbought     = RSI_OVERBOUGHT,
        rsi_oversold       = RSI_OVERSOLD,
        rsi_bull_threshold = RSI_BULL_THRESHOLD,
        rsi_bear_threshold = RSI_BEAR_THRESHOLD,
        min_confidence     = MIN_CONFIDENCE,
        interval           = CANDLE_INTERVAL,
        chainlink_enabled  = CL_ENABLED and bool(rpc_url),
        cl_poll_interval   = CL_POLL_INT,
        cl_lookback_secs   = CL_LOOKBACK,
        cl_max_divergence  = CL_MAX_DIV,
        cl_min_change_pct  = CL_MIN_CHANGE,
        poly_signal_thresh = POLY_SIG_THRESH,
        min_consensus      = MIN_CONSENSUS,
    )

    log.info("Pre-cargando velas históricas de Binance ...")
    preload_history(engine.binance, lookback_candles=50)

    engine.start()
    log.info("Engine multi-fuente arrancado — esperando primera señal ...")

    client   = build_clob_client()
    executor = OrderExecutor(client=client, log=log)
    log.info("CLOB client autenticado ✔")

    # ── Loop principal: ventana por ventana ───────────────────────────────────
    while True:
        market = wait_for_active_market(interval)
        run_window(market, executor, engine, interval)

        end_time  = get_market_end_time(market)
        wait_secs = 30
        if end_time:
            remaining = (end_time - datetime.now(timezone.utc)).total_seconds()
            wait_secs = max(5, remaining + 5)
        log.info(f"Esperando {wait_secs:.0f}s para la siguiente ventana ...")
        time.sleep(wait_secs)


if __name__ == "__main__":
    run()
