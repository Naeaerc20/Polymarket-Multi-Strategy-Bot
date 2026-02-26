"""
main.py
-------
Launches one or multiple Polymarket bots in parallel threads.

On startup, interactively asks:
  1. Which strategy  (DCA Snipe | YES+NO Arbitrage | Copy Trade | RSI+VWAP Signal)
  2. Which markets   (btc, eth, sol, xrp — one or many)
  3. Which interval  (5m | 15m | 1h — DCA, YES+NO, RSI+VWAP)

Each bot runs in its own thread. Threads auto-restart on crash.
Ctrl+C shuts down all bots gracefully.

Non-interactive usage:
    python main.py --strategy dca       --operate btc eth
    python main.py --strategy dca       --operate all       --interval 15m
    python main.py --strategy yesno     --operate btc sol   --interval 15m
    python main.py --strategy yesno     --operate all       --interval 5m
    python main.py --strategy copytrade
    python main.py --strategy rsivwap   --operate btc       --interval 5m
    python main.py --strategy rsivwap   --operate all       --interval 15m
"""

import argparse
import importlib
import importlib.util
import logging
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s][%(levelname)s][%(name)s] - %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("main")

AVAILABLE_MARKETS = ["btc", "eth", "sol", "xrp"]

# ── Interval options per strategy ─────────────────────────────────────────────
STRATEGY_INTERVALS = {
    "dca"      : ["5m", "15m", "1h"],
    "yesno"    : ["5m", "15m"],
    "rsivwap"  : ["5m", "15m"],
    "copytrade": [],   # no interval
}

STRATEGIES = {
    "dca": {
        "label"      : "DCA Snipe  — entry arming + bracket orders + optional DCA",
        "import_mode": "module",
        "needs_markets" : True,
        "needs_interval": True,
        "modules": {
            "btc": "strategies.DCA_Snipe.markets.btc.bot",
            "eth": "strategies.DCA_Snipe.markets.eth.bot",
            "sol": "strategies.DCA_Snipe.markets.sol.bot",
            "xrp": "strategies.DCA_Snipe.markets.xrp.bot",
        },
    },
    "yesno": {
        "label"      : "YES+NO Arbitrage  — buy UP+DOWN inside PRICE_RANGE for < $1.00",
        "import_mode": "path",
        "needs_markets" : True,
        "needs_interval": True,
        "paths": {
            "btc": ROOT / "strategies" / "YES+NO_1usd" / "markets" / "btc" / "bot.py",
            "eth": ROOT / "strategies" / "YES+NO_1usd" / "markets" / "eth" / "bot.py",
            "sol": ROOT / "strategies" / "YES+NO_1usd" / "markets" / "sol" / "bot.py",
            "xrp": ROOT / "strategies" / "YES+NO_1usd" / "markets" / "xrp" / "bot.py",
        },
    },
    "copytrade": {
        "label"      : "Copy Trade  — mirror trades from followed wallets in real time",
        "import_mode": "path",
        "needs_markets" : False,
        "needs_interval": False,
        "paths": {
            "all": ROOT / "strategies" / "Copy-Trade" / "bot.py",
        },
    },
    "rsivwap": {
        "label"      : "RSI+VWAP Signal  — Binance 5m candles → RSI+VWAP → buy UP or DOWN",
        "import_mode": "path",
        "needs_markets" : True,
        "needs_interval": True,
        "paths": {
            "btc": ROOT / "strategies" / "RSI_VWAP_Signal" / "markets" / "btc" / "bot.py",
            "eth": ROOT / "strategies" / "RSI_VWAP_Signal" / "markets" / "eth" / "bot.py",
            "sol": ROOT / "strategies" / "RSI_VWAP_Signal" / "markets" / "sol" / "bot.py",
            "xrp": ROOT / "strategies" / "RSI_VWAP_Signal" / "markets" / "xrp" / "bot.py",
        },
    },
}

_shutdown = threading.Event()


# ══════════════════════════════════════════════════════════════════════════════
#  MODULE LOADER
# ══════════════════════════════════════════════════════════════════════════════

def load_bot_module(strategy_key: str, market: str):
    strategy = STRATEGIES[strategy_key]

    if strategy["import_mode"] == "module":
        dot_path = strategy["modules"][market]
        try:
            return importlib.import_module(dot_path)
        except ImportError as exc:
            raise ImportError(
                f"Cannot import {dot_path}.\n"
                f"  Make sure strategies/DCA_Snipe/markets/{market}/ exists.\n"
                f"  Error: {exc}"
            )
    else:
        path_key  = "all" if "all" in strategy["paths"] else market
        file_path = strategy["paths"][path_key]
        if not file_path.exists():
            raise FileNotFoundError(
                f"Bot file not found: {file_path}\n"
                f"  Check your project structure."
            )
        mod_name = f"{strategy_key}_{market}"
        spec     = importlib.util.spec_from_file_location(mod_name, file_path)
        module   = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


# ══════════════════════════════════════════════════════════════════════════════
#  INTERACTIVE PROMPTS
# ══════════════════════════════════════════════════════════════════════════════

def ask_strategy() -> str:
    choices = list(STRATEGIES.items())   # [(key, cfg), ...]
    labels  = [cfg["label"] for _, cfg in choices]
    keys    = [k for k, _ in choices]
    try:
        import questionary
        answer = questionary.select("Select strategy:", choices=labels).ask()
        if answer is None:
            sys.exit(0)
        return keys[labels.index(answer)]
    except (ImportError, Exception):
        pass
    print("\nAvailable strategies:")
    for i, label in enumerate(labels, 1):
        print(f"  {i}) {label}")
    while True:
        raw = input(f"Select (1-{len(labels)}): ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        print(f"  Please enter a number between 1 and {len(labels)}.")


def ask_markets() -> list:
    try:
        import questionary
        answers = questionary.checkbox(
            "Select markets:  (Space = toggle, Enter = confirm)",
            choices=[
                questionary.Choice("BTC — Bitcoin",  value="btc", checked=True),
                questionary.Choice("ETH — Ethereum", value="eth", checked=False),
                questionary.Choice("SOL — Solana",   value="sol", checked=False),
                questionary.Choice("XRP — Ripple",   value="xrp", checked=False),
            ],
        ).ask()
        if not answers:
            print("No markets selected — exiting.")
            sys.exit(0)
        return answers
    except (ImportError, Exception):
        pass
    print(f"\nAvailable markets: {', '.join(AVAILABLE_MARKETS)}")
    print("Enter markets separated by spaces, or 'all':")
    while True:
        raw = input("  → ").strip().lower()
        if not raw:
            print("  At least one market required.")
            continue
        if raw == "all":
            return list(AVAILABLE_MARKETS)
        selected = raw.split()
        invalid  = [m for m in selected if m not in AVAILABLE_MARKETS]
        if invalid:
            print(f"  Unknown: {', '.join(invalid)} — try again.")
            continue
        return selected


def ask_interval(strategy_key: str) -> str:
    options = STRATEGY_INTERVALS.get(strategy_key, ["5m", "15m"])
    labels  = {
        "5m":  "5 minutes",
        "15m": "15 minutes",
        "1h":  "1 hour",
    }
    display = [labels.get(o, o) for o in options]
    try:
        import questionary
        choice = questionary.select("Select market interval:", choices=display).ask()
        if choice is None:
            sys.exit(0)
        return options[display.index(choice)]
    except (ImportError, Exception):
        pass
    print(f"Available intervals: {', '.join(options)}")
    while True:
        raw = input("  → ").strip().lower()
        if raw in options:
            return raw
        short = {"5": "5m", "15": "15m", "60": "1h", "1h": "1h"}
        if raw in short and short[raw] in options:
            return short[raw]
        print(f"  Please enter one of: {', '.join(options)}")


# ══════════════════════════════════════════════════════════════════════════════
#  BOT THREAD
# ══════════════════════════════════════════════════════════════════════════════

class BotThread(threading.Thread):
    RESTART_DELAY = 10

    def __init__(self, market: str, strategy_key: str, run_kwargs: dict):
        super().__init__(name=f"bot-{market}", daemon=True)
        self.market       = market
        self.strategy_key = strategy_key
        self.run_kwargs   = run_kwargs

    def run(self):
        try:
            module = load_bot_module(self.strategy_key, self.market)
            log.info(f"[{self.market.upper()}] Module loaded OK.")
        except Exception as exc:
            log.error(f"[{self.market.upper()}] Failed to load module: {exc}")
            return

        while not _shutdown.is_set():
            try:
                log.info(f"[{self.market.upper()}] Starting bot ...")
                module.run(**self.run_kwargs)
                log.info(f"[{self.market.upper()}] Bot exited cleanly.")
                break
            except Exception as exc:
                if _shutdown.is_set():
                    break
                log.error(
                    f"[{self.market.upper()}] Crashed: {exc} — "
                    f"restarting in {self.RESTART_DELAY}s ..."
                )
                _shutdown.wait(timeout=self.RESTART_DELAY)

        log.info(f"[{self.market.upper()}] Thread stopped.")


# ══════════════════════════════════════════════════════════════════════════════
#  SIGNAL HANDLER
# ══════════════════════════════════════════════════════════════════════════════

def _handle_shutdown(signum, frame):
    log.info("Shutdown signal received — stopping all bots ...")
    _shutdown.set()


# ══════════════════════════════════════════════════════════════════════════════
#  CLI ARGS
# ══════════════════════════════════════════════════════════════════════════════

def parse_args():
    parser = argparse.ArgumentParser(
        description="Polymarket Multi-Strategy Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --strategy dca       --operate btc eth      --interval 15m
  python main.py --strategy dca       --operate all          --interval 5m
  python main.py --strategy yesno     --operate btc sol      --interval 15m
  python main.py --strategy yesno     --operate all          --interval 5m
  python main.py --strategy copytrade
  python main.py --strategy rsivwap   --operate btc          --interval 5m
  python main.py --strategy rsivwap   --operate all          --interval 15m
        """,
    )
    parser.add_argument(
        "--strategy",
        choices=list(STRATEGIES.keys()),
        default=None,
        help="Strategy to run",
    )
    parser.add_argument(
        "--operate",
        nargs="+",
        metavar="MARKET",
        default=None,
        help="Markets to run: btc eth sol xrp (or 'all')",
    )
    parser.add_argument(
        "--interval",
        choices=["5m", "15m", "1h"],
        default=None,
        help="Market interval (required for dca, yesno, rsivwap)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Copy-Trade only: log trades without placing real orders",
    )
    return parser.parse_args()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    args = parse_args()

    print("\n" + "=" * 60)
    print("  Polymarket Multi-Strategy Bot")
    print("=" * 60)

    # ── Strategy selection ─────────────────────────────────────────────────────
    strategy_key = args.strategy or ask_strategy()
    strategy_cfg = STRATEGIES[strategy_key]

    # ── Market selection ───────────────────────────────────────────────────────
    run_kwargs = {}

    if not strategy_cfg["needs_markets"]:
        # Copy-Trade: single bot, no per-market files
        markets = ["all"]
        if args.dry_run:
            run_kwargs["dry_run"] = True
    else:
        if args.operate:
            raw = [m.lower() for m in args.operate]
            markets = list(AVAILABLE_MARKETS) if "all" in raw else raw
            invalid = [m for m in markets if m not in AVAILABLE_MARKETS]
            if invalid:
                log.error(f"Unknown market(s): {', '.join(invalid)}")
                sys.exit(1)
        else:
            markets = ask_markets()

    # ── Interval selection ─────────────────────────────────────────────────────
    if strategy_cfg["needs_interval"]:
        interval               = args.interval or ask_interval(strategy_key)
        run_kwargs["interval"] = interval
    else:
        interval = None

    # ── Startup banner ─────────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info(f"Strategy : {strategy_cfg['label']}")
    log.info(f"Markets  : {', '.join(m.upper() for m in markets)}")
    if interval:
        log.info(f"Interval : {interval}")
    log.info("=" * 60)

    # ── Signal handlers ────────────────────────────────────────────────────────
    signal.signal(signal.SIGINT,  _handle_shutdown)
    signal.signal(signal.SIGTERM, _handle_shutdown)

    # ── Launch threads ─────────────────────────────────────────────────────────
    threads = []
    for market in markets:
        t = BotThread(
            market       = market,
            strategy_key = strategy_key,
            run_kwargs   = run_kwargs,
        )
        t.start()
        threads.append(t)
        time.sleep(0.5)  # stagger startup

    log.info(f"{len(threads)} bot thread(s) running. Press Ctrl+C to stop.")

    # ── Monitor ────────────────────────────────────────────────────────────────
    try:
        while not _shutdown.is_set():
            if not any(t.is_alive() for t in threads):
                log.info("All bot threads have stopped.")
                break
            _shutdown.wait(timeout=5)
    except KeyboardInterrupt:
        _shutdown.set()

    # ── Graceful shutdown ──────────────────────────────────────────────────────
    log.info("Waiting for all threads to stop ...")
    for t in threads:
        t.join(timeout=15)
        if t.is_alive():
            log.warning(f"[{t.market.upper()}] Thread did not stop in time.")

    log.info("All bots stopped. Goodbye.")


if __name__ == "__main__":
    main()