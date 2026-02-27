"""
risk_manager.py
───────────────
Módulo de gestión de riesgo para la estrategia RSI_VWAP v2.

GUARDS IMPLEMENTADOS
────────────────────

1. TIME-DECAY GUARD  (pre-entrada)
   ─────────────────────────────────
   Divide la ventana temporal en tres zonas:

     PRIME  [0%  – EARLY_BET_WINDOW_SECS]
       Primeros N segundos tras la apertura.
       Precio más cercano al 50/50 → mejor valor por share.
       Si EARLY_BET_ONLY=true, SOLO se entra en esta zona.

     OK     [PRIME – RISK_TIME_DECAY_MAX_PCT de la ventana]
       Zona válida para entrar si no hubo señal en PRIME.

     LATE   [RISK_TIME_DECAY_MAX_PCT – cierre]
       Demasiado tarde. No se abren nuevas posiciones.
       El precio ya refleja buena parte de la información.

   Ejemplo (ventana 5m = 300s, RISK_TIME_DECAY_MAX_PCT=0.50):
     PRIME: 0–90s  (EARLY_BET_WINDOW_SECS=90)
     OK:    90–150s
     LATE:  150–300s → NO nuevas entradas

2. STOP-LOSS MONITOR  (post-entrada)
   ────────────────────────────────────
   Si el precio del token cae RISK_STOP_LOSS_PCT por debajo del precio
   de entrada promedio → retorna RiskEvent.STOP_LOSS.

   Ejemplo: avg_entry=0.48, RISK_STOP_LOSS_PCT=0.04 → SL=0.4608
   El bot debe ejecutar una venta inmediata (FAK).

3. HEDGE TRIGGER  (post-entrada)
   ─────────────────────────────────
   Si el precio cae más allá de RISK_HEDGE_TRIGGER_PCT (mayor que SL):
   → retorna RiskEvent.HEDGE.
   El bot puede comprar el lado contrario para cubrir la pérdida.

   Ejemplo: avg_entry=0.48, RISK_HEDGE_TRIGGER_PCT=0.06 → hedge a 0.4512
   Lógica: compramos DOWN con el mismo amount que el UP perdiendo,
   de modo que si DOWN resuelve a $1, cubrimos la pérdida de UP.

   Nota: HEDGE y STOP_LOSS son mutuamente excluyentes.
         El bot elige uno u otro según su configuración.

VARIABLES .env
──────────────
  RISK_STOP_LOSS_ENABLED    true | false (default true)
  RISK_STOP_LOSS_PCT        % de caída para activar SL (default 0.04 = 4%)
  RISK_HEDGE_ENABLED        true | false (default false)
  RISK_HEDGE_TRIGGER_PCT    % de caída para activar hedge (default 0.06 = 6%)
  RISK_TIME_DECAY_MAX_PCT   % de ventana tras el cual no entrar (default 0.50)
  EARLY_BET_WINDOW_SECS     Segundos de la zona PRIME (default 90)
  EARLY_BET_ONLY            true = solo entrar en PRIME (default false)
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

log = logging.getLogger("risk-manager")


# ══════════════════════════════════════════════════════════════════════════════
#  ENUMS Y TIPOS
# ══════════════════════════════════════════════════════════════════════════════

class WindowZone(str, Enum):
    """Zona temporal dentro de la ventana de mercado."""
    PRIME = "PRIME"   # Apertura — mejor precio, entrada preferida
    OK    = "OK"      # Zona válida de entrada
    LATE  = "LATE"    # Demasiado tarde — no entrar


class RiskEvent(str, Enum):
    """Resultado de check_position()."""
    OK          = "OK"           # Posición sana, mantener
    STOP_LOSS   = "STOP_LOSS"    # Precio cayó demasiado → salir
    HEDGE       = "HEDGE"        # Pérdida elevada → cubrir con posición contraria
    TIME_EXPIRED= "TIME_EXPIRED" # Ventana casi cerrada → no abrir nuevas


@dataclass
class RiskStatus:
    """Estado devuelto por RiskManager en cada tick."""
    event          : RiskEvent
    current_price  : float
    avg_entry_price: float
    sl_level       : Optional[float]  = None   # precio de stop-loss calculado
    hedge_level    : Optional[float]  = None   # precio donde se activa hedge
    zone           : WindowZone       = WindowZone.OK
    elapsed_secs   : float            = 0.0
    window_secs    : float            = 300.0

    @property
    def pnl_pct(self) -> float:
        """P&L porcentual respecto al precio de entrada."""
        if self.avg_entry_price <= 0:
            return 0.0
        return (self.current_price - self.avg_entry_price) / self.avg_entry_price

    def __str__(self) -> str:
        pnl_sign = "+" if self.pnl_pct >= 0 else ""
        return (
            f"[RiskManager] event={self.event.value:<12}  "
            f"zone={self.zone.value:<6}  "
            f"price={self.current_price:.4f}  "
            f"entry={self.avg_entry_price:.4f}  "
            f"P&L={pnl_sign}{self.pnl_pct*100:.2f}%  "
            f"SL={f'{self.sl_level:.4f}' if self.sl_level else 'n/a'}  "
            f"hedge={f'{self.hedge_level:.4f}' if self.hedge_level else 'n/a'}  "
            f"elapsed={self.elapsed_secs:.0f}s/{self.window_secs:.0f}s"
        )


# ══════════════════════════════════════════════════════════════════════════════
#  RISK MANAGER
# ══════════════════════════════════════════════════════════════════════════════

class RiskManager:
    """
    Gestiona el riesgo de una ventana de mercado.

    Uso típico:

        rm = RiskManager(
            window_secs            = 300,    # ventana 5m
            stop_loss_enabled      = True,
            stop_loss_pct          = 0.04,
            hedge_enabled          = False,
            hedge_trigger_pct      = 0.06,
            time_decay_max_pct     = 0.50,
            early_bet_window_secs  = 90,
            early_bet_only         = False,
        )
        rm.open_window()          # llamar al inicio de la ventana

        # Pre-entrada — determinar zona
        zone = rm.get_zone()
        if zone == WindowZone.LATE:
            skip_entry()

        # Post-entrada — llamar cada tick
        rm.set_position(avg_entry=0.48, current_price=0.45)
        status = rm.check_position(current_price=0.45)
        if status.event == RiskEvent.STOP_LOSS:
            sell_immediately()
        elif status.event == RiskEvent.HEDGE:
            buy_opposite_side()
    """

    def __init__(
        self,
        window_secs           : float = 300.0,
        stop_loss_enabled     : bool  = True,
        stop_loss_pct         : float = 0.04,
        hedge_enabled         : bool  = False,
        hedge_trigger_pct     : float = 0.06,
        time_decay_max_pct    : float = 0.50,
        early_bet_window_secs : float = 90.0,
        early_bet_only        : bool  = False,
    ):
        self.window_secs           = window_secs
        self.stop_loss_enabled     = stop_loss_enabled
        self.stop_loss_pct         = stop_loss_pct
        self.hedge_enabled         = hedge_enabled
        self.hedge_trigger_pct     = hedge_trigger_pct
        self.time_decay_max_pct    = time_decay_max_pct
        self.early_bet_window_secs = early_bet_window_secs
        self.early_bet_only        = early_bet_only

        self._window_open_ts  : Optional[float] = None
        self._avg_entry       : float            = 0.0
        self._in_position     : bool             = False
        self._stop_loss_fired : bool             = False
        self._hedge_fired     : bool             = False

        log.info(
            f"[RiskManager] init — "
            f"window={window_secs}s  "
            f"SL={stop_loss_enabled}({stop_loss_pct*100:.1f}%)  "
            f"hedge={hedge_enabled}({hedge_trigger_pct*100:.1f}%)  "
            f"time_decay={time_decay_max_pct*100:.0f}%  "
            f"prime={early_bet_window_secs}s  "
            f"early_only={early_bet_only}"
        )

    # ── Ciclo de vida de ventana ───────────────────────────────────────────────

    def open_window(self) -> None:
        """Llamar al inicio de cada ventana de mercado."""
        self._window_open_ts  = time.time()
        self._avg_entry       = 0.0
        self._in_position     = False
        self._stop_loss_fired = False
        self._hedge_fired     = False
        log.info("[RiskManager] Window opened")

    def set_position(self, avg_entry: float) -> None:
        """Registrar apertura de posición tras un BUY exitoso."""
        self._avg_entry   = avg_entry
        self._in_position = True
        sl  = self._sl_level()
        hdg = self._hedge_level()
        log.info(
            f"[RiskManager] Position set — "
            f"entry={avg_entry:.4f}  "
            f"SL={f'{sl:.4f}' if sl else 'disabled'}  "
            f"hedge={f'{hdg:.4f}' if hdg else 'disabled'}"
        )

    def update_avg_entry(self, new_avg: float) -> None:
        """Actualizar precio promedio de entrada tras DCA."""
        self._avg_entry = new_avg
        sl  = self._sl_level()
        hdg = self._hedge_level()
        log.info(
            f"[RiskManager] Avg entry updated — "
            f"new_avg={new_avg:.4f}  "
            f"new_SL={f'{sl:.4f}' if sl else 'disabled'}  "
            f"new_hedge={f'{hdg:.4f}' if hdg else 'disabled'}"
        )

    # ── Zona temporal ──────────────────────────────────────────────────────────

    def elapsed(self) -> float:
        """Segundos transcurridos desde la apertura de ventana."""
        if self._window_open_ts is None:
            return 0.0
        return time.time() - self._window_open_ts

    def get_zone(self) -> WindowZone:
        """
        Devuelve la zona temporal actual dentro de la ventana.
        PRIME → entrada preferida (mejor precio)
        OK    → entrada válida
        LATE  → no entrar
        """
        el     = self.elapsed()
        late_s = self.window_secs * self.time_decay_max_pct

        if el <= self.early_bet_window_secs:
            return WindowZone.PRIME
        if el <= late_s:
            return WindowZone.OK
        return WindowZone.LATE

    def can_enter(self) -> bool:
        """True si todavía es posible abrir una nueva posición."""
        zone = self.get_zone()
        if zone == WindowZone.LATE:
            return False
        if self.early_bet_only and zone != WindowZone.PRIME:
            return False
        return True

    # ── Niveles de riesgo ──────────────────────────────────────────────────────

    def _sl_level(self) -> Optional[float]:
        if not self.stop_loss_enabled or self._avg_entry <= 0:
            return None
        return round(self._avg_entry * (1 - self.stop_loss_pct), 6)

    def _hedge_level(self) -> Optional[float]:
        if not self.hedge_enabled or self._avg_entry <= 0:
            return None
        return round(self._avg_entry * (1 - self.hedge_trigger_pct), 6)

    # ── Evaluación post-entrada ────────────────────────────────────────────────

    def check_position(self, current_price: float) -> RiskStatus:
        """
        Evalúa el riesgo de la posición actual.

        Llamar en cada tick cuando hay posición abierta.
        Devuelve RiskStatus con el evento correspondiente.

        Prioridad de eventos:
          STOP_LOSS > HEDGE > TIME_EXPIRED > OK

        Una vez disparado SL o HEDGE, no vuelve a dispararse (evitar
        ejecuciones repetidas en el mismo tick si el precio no se mueve).
        """
        el        = self.elapsed()
        zone      = self.get_zone()
        sl_level  = self._sl_level()
        hdg_level = self._hedge_level()

        base = RiskStatus(
            event           = RiskEvent.OK,
            current_price   = current_price,
            avg_entry_price = self._avg_entry,
            sl_level        = sl_level,
            hedge_level     = hdg_level,
            zone            = zone,
            elapsed_secs    = el,
            window_secs     = self.window_secs,
        )

        if not self._in_position:
            return base

        # ── Stop-loss ─────────────────────────────────────────────────────────
        if (
            self.stop_loss_enabled
            and sl_level is not None
            and current_price <= sl_level
            and not self._stop_loss_fired
        ):
            self._stop_loss_fired = True
            base.event = RiskEvent.STOP_LOSS
            log.warning(
                f"[RiskManager] STOP-LOSS triggered — "
                f"price={current_price:.4f} <= SL={sl_level:.4f}  "
                f"entry={self._avg_entry:.4f}  "
                f"loss={base.pnl_pct*100:.2f}%"
            )
            return base

        # ── Hedge trigger ─────────────────────────────────────────────────────
        if (
            self.hedge_enabled
            and hdg_level is not None
            and current_price <= hdg_level
            and not self._hedge_fired
            and not self._stop_loss_fired
        ):
            self._hedge_fired = True
            base.event = RiskEvent.HEDGE
            log.warning(
                f"[RiskManager] HEDGE triggered — "
                f"price={current_price:.4f} <= hedge={hdg_level:.4f}  "
                f"entry={self._avg_entry:.4f}  "
                f"loss={base.pnl_pct*100:.2f}%"
            )
            return base

        # ── Time-expired ──────────────────────────────────────────────────────
        if zone == WindowZone.LATE and not self._in_position:
            base.event = RiskEvent.TIME_EXPIRED

        return base

    # ── Helpers de logging ─────────────────────────────────────────────────────

    def log_status(self, current_price: float) -> None:
        """Imprime el estado actual del risk manager en los logs."""
        status = self.check_position(current_price)
        log.debug(str(status))

    def zone_label(self) -> str:
        """Etiqueta para el log de zona temporal."""
        zone = self.get_zone()
        el   = self.elapsed()
        remaining = max(0, self.window_secs - el)
        m, s = divmod(int(remaining), 60)
        labels = {
            WindowZone.PRIME: f"PRIME({el:.0f}s)",
            WindowZone.OK:    f"OK({el:.0f}s)",
            WindowZone.LATE:  f"LATE({el:.0f}s)",
        }
        return f"{labels[zone]} rem={m:02d}:{s:02d}"
