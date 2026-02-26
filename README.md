# Polymarket Multi-Strategy Bot

Automated prediction market trading bot for [Polymarket](https://polymarket.com).  
Monitors BTC, ETH, SOL, and XRP crypto asset markets and executes trades via the Polymarket CLOB API using four independent strategies.

---

## Project Structure

```
Polymarket-Multi-Strategy-Bot/
│
├── .env                          ← Credentials + all strategy parameters
├── .env.example                  ← Documented template (safe to commit)
├── .gitignore
│
├── main.py                       ← Interactive launcher — select strategy + markets
├── auto_claim.py                 ← Auto-redeem resolved positions via Relayer
├── order_executor.py             ← Shared order placement (FAK / FOK / GTC)
├── market_stream.py              ← WebSocket price feed (WSS + REST fallback)
├── setup.py                      ← One-command setup: install, prompt, derive keys, validate
├── requirements.txt
├── README.md
│
└── strategies/
    │
    ├── DCA_Snipe/                ← Strategy 1: Entry arming + bracket orders + DCA
    │   └── markets/
    │       ├── btc/bot.py
    │       ├── eth/bot.py
    │       ├── sol/bot.py
    │       └── xrp/bot.py
    │
    ├── YES+NO_1usd/              ← Strategy 2: YES+NO arbitrage — buy both sides < $1.00
    │   └── markets/
    │       ├── btc/bot.py
    │       ├── eth/bot.py
    │       ├── sol/bot.py
    │       └── xrp/bot.py
    │
    ├── Copy-Trade/               ← Strategy 3: Mirror trades from followed wallets
    │   ├── bot.py
    │   ├── trader_monitor.py
    │   └── traders.json
    │
    └── RSI_VWAP_Signal/          ← Strategy 4: Binance RSI + VWAP signal → buy UP or DOWN
        ├── signal_engine.py
        └── markets/
            ├── signal_engine.py  ← place here if using this path
            ├── btc/bot.py
            ├── eth/bot.py
            ├── sol/bot.py
            └── xrp/bot.py
```

---

## Getting Started

### 1. Clone the repository

```bash
git clone https://github.com/Naeaerc20/Polymarket-Multi-Strategy-Bot.git
cd Polymarket-Multi-Strategy-Bot
```

### 2. Run setup

```bash
python setup.py
```

`setup.py` does everything in one command:

- Checks Python version (>= 3.9 required)
- Verifies project structure and bot files
- Installs all missing dependencies
- Prompts for `POLY_PRIVATE_KEY`, `FUNDER_ADDRESS`, `POLY_RPC`, `SIGNATURE_TYPE` and writes them to `.env`
- Derives `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE` via EIP-712 signing and saves them
- Validates all required variables are present
- Confirms connectivity to the Polymarket CLOB

```bash
# Validate without changing anything
python setup.py --check-only

# Force regeneration of API credentials
python setup.py --regen-keys
```

### 3. Configure strategy parameters

Open `.env` and fill in the parameters for the strategy you want to run.  
See the **Configuration** section below for all variables.

### 4. Start the bot

```bash
python main.py
```

On startup, `main.py` interactively asks:

```
? Select strategy:
  › DCA Snipe          — entry arming + bracket orders + optional DCA
    YES+NO Arbitrage   — buy UP+DOWN inside PRICE_RANGE for < $1.00
    Copy Trade         — mirror trades from followed wallets in real time
    RSI+VWAP Signal    — Binance 5m candles → RSI+VWAP → buy UP or DOWN

? Select markets:  (Space = toggle, Enter = confirm)
  ● BTC — Bitcoin
  ○ ETH — Ethereum
  ○ SOL — Solana
  ○ XRP — Ripple

# YES+NO and RSI+VWAP only:
? Select market interval:
  › 5 minutes
    15 minutes
```

Non-interactive usage:

```bash
python main.py --strategy dca      --operate btc eth
python main.py --strategy dca      --operate all
python main.py --strategy yesno    --operate btc sol --interval 15m
python main.py --strategy yesno    --operate all     --interval 5m
python main.py --strategy copytrade
python main.py --strategy rsivwap  --operate btc     --interval 5m
python main.py --strategy rsivwap  --operate all     --interval 15m
```

### 5. Auto claim (optional)

Monitors the proxy wallet and automatically redeems any resolved positions:

```bash
python auto_claim.py
```

---

## Strategies

---

### Strategy 1 — DCA Snipe (`strategies/DCA_Snipe/`)

Monitors UP and DOWN prices via WebSocket. Waits for either side to cross `ENTRY_PRICE` from below — "entry arming" prevents false triggers on window open. On trigger, places a buy order and immediately submits GTC bracket orders for take profit and stop loss. Optionally scales in with additional buys on each `BET_STEP` increment.

**Stop loss modes:**

| Config | Behavior |
|---|---|
| `STOP_LOSS=0.55` | Fixed — SL always at that price |
| `STOP_LOSS_OFFSET=0.05` | Dynamic — SL = avg_entry − 0.05, recalculates after each DCA |
| Both `null` | Break-even — SL = avg_entry (zero loss guaranteed) |

**Parameters:**

| Variable | Description |
|---|---|
| `{ASSET}_ENTRY_PRICE` | Price UP or DOWN must cross from below to trigger entry |
| `{ASSET}_AMOUNT_PER_BET` | USDC per buy — initial entry and each DCA step |
| `{ASSET}_TAKE_PROFIT` | GTC sell placed at this price immediately after entry |
| `{ASSET}_STOP_LOSS` | Fixed SL price. `null` → use offset or break-even |
| `{ASSET}_STOP_LOSS_OFFSET` | Dynamic SL offset. `null` → use fixed or break-even |
| `{ASSET}_BET_STEP` | DCA step size. `null` = single entry, no DCA |
| `{ASSET}_POLL_INTERVAL` | Seconds between price ticks |

---

### Strategy 2 — YES+NO Arbitrage (`strategies/YES+NO_1usd/`)

Monitors UP and DOWN prices simultaneously. Buys both sides to capture a combined cost under $1.00. Since exactly one side always resolves to $1.00 at expiry, buying both for less guarantees profit regardless of outcome.

**Modes:**

**Standard** (`LOSS_PREVENTION=false`): both sides independently watch `PRICE_RANGE`. Each side buys with `AMOUNT_TO_BUY` when it enters range.

**Loss Prevention** (`LOSS_PREVENTION=true`): two-phase approach.
- **Phase 1** — watches both sides for `TRIGGER_RANGE` (the higher range). Buys whichever side hits it first with an auto-calculated amount, not `AMOUNT_TO_BUY`.
- **Phase 2** — waits for the opposite side to fall into `PRICE_RANGE`, then buys it with `AMOUNT_TO_BUY`.

The trigger-side amount is calculated to guarantee profit even if that side wins:

```
trigger_amount = AMOUNT_TO_BUY × P_trigger / (1 − P_trigger) × (1 + TRIGGER_PROFIT_MARGIN)
```

Example with `P_trigger=0.52`, `AMOUNT_TO_BUY=$2.50`, `margin=5%`:
```
trigger_amount = 2.50 × 0.52/0.48 × 1.05 = $2.844

If trigger wins: 5.469 shares × $1.00 = $5.469  spent=$5.344  profit=+$0.125 ✔
If price wins:   5.814 shares × $1.00 = $5.814  spent=$5.344  profit=+$0.470 ✔
```

**Parameters:**

| Variable | Description |
|---|---|
| `{ASSET}_LOSS_PREVENTION` | `false` = standard · `true` = two-phase LP mode |
| `{ASSET}_PRICE_RANGE` | Entry band for standard mode (both sides) or LP phase 2 |
| `{ASSET}_TRIGGER_RANGE` | First-side entry band (LP only) — must be higher than PRICE_RANGE |
| `{ASSET}_AMOUNT_TO_BUY` | USDC for the PRICE side (and both sides in standard mode) |
| `{ASSET}_TRIGGER_PROFIT_MARGIN` | Profit margin on trigger side e.g. `0.05` = 5% |
| `{ASSET}_POLL_INTERVAL` | Seconds between price ticks |

---

### Strategy 3 — Copy Trade (`strategies/Copy-Trade/`)

Monitors wallets listed in `traders.json` via the Polymarket Data API. Detects new trades in real time (WebSocket-driven) and mirrors them on your account. Supports reverse trading — copies each trade in the opposite direction.

**Sizing modes:**

| `COPY_MODE` | Behavior |
|---|---|
| `fixed` | Spend exactly `COPY_AMOUNT_USDC` per copied trade |
| `percentage` | Spend `COPY_PERCENTAGE%` of the original trade's value |

**Per-trader fields in `traders.json`:**

| Field | Description |
|---|---|
| `enabled` | `true` / `false` — include this wallet |
| `copy_buys` | Mirror BUY trades |
| `copy_sells` | Mirror SELL trades |
| `reverse_trading` | `true` = invert direction + buy the opposite outcome token |
| `max_position_size` | Max USDC exposure per market |

**`reverse_trading` explained:**  
When `true`, every trade is mirrored in the opposite direction and on the opposite token of the same market:
- Trader buys UP → bot buys DOWN (opposite token, same `condition_id`)
- Trader buys DOWN → bot buys UP

The opposite token is resolved automatically via the Gamma API.

**`traders.json` example:**

```json
{
  "traders": [
    {
      "address": "0xABC...",
      "nickname": "MyTrader",
      "enabled": true,
      "copy_buys": true,
      "copy_sells": false,
      "reverse_trading": false,
      "max_position_size": 50,
      "notes": "Crypto trader"
    }
  ],
  "global_settings": {
    "enabled": true,
    "copy_delay_seconds": 0.1,
    "max_concurrent_trades": 100,
    "stop_on_error": false
  }
}
```

**Parameters:**

| Variable | Description |
|---|---|
| `COPY_MODE` | `fixed` or `percentage` |
| `COPY_AMOUNT_USDC` | USDC per trade (fixed mode) |
| `COPY_PERCENTAGE` | % of original trade value (percentage mode) |
| `COPY_ORDER_BUY_TYPE` | `FAK` \| `FOK` \| `GTC` for BUY copies |
| `COPY_ORDER_SELL_TYPE` | `FAK` \| `FOK` \| `GTC` for SELL copies |
| `COPY_MIN_TRADE_USDC` | Skip original trades smaller than this |
| `COPY_MAX_TRADE_USDC` | Skip original trades larger than this. `null` = no limit |
| `COPY_POLL_INTERVAL` | `null` = WebSocket real-time · `N` = REST poll every N seconds |
| `COPY_DRY_RUN` | `true` = log only, no real orders |

---

### Strategy 4 — RSI + VWAP Signal (`strategies/RSI_VWAP_Signal/`)

Streams live 5-minute candlestick data from Binance WebSocket (no API key required). Calculates RSI and VWAP in real time. When both indicators agree on direction, emits a signal with a confidence score. The bot buys the corresponding Polymarket token only when confidence exceeds the configured threshold and the token price is within range.

**How RSI works:**  
RSI (0–100) measures how fast the price is moving.
- RSI > 70 = overbought → avoid UP entries
- RSI < 30 = oversold → avoid DOWN entries
- RSI crossing above 52 → bullish momentum → UP signal
- RSI crossing below 48 → bearish momentum → DOWN signal

**How VWAP works:**  
VWAP is the average price weighted by volume, reset every UTC day.
- Price above VWAP → bulls in control → supports UP signal
- Price below VWAP → bears in control → supports DOWN signal

**Signal logic:**
```
UP   signal: price > VWAP  AND  RSI > RSI_BULL_THRESHOLD  AND  RSI < RSI_OVERBOUGHT
DOWN signal: price < VWAP  AND  RSI < RSI_BEAR_THRESHOLD  AND  RSI > RSI_OVERSOLD
NEUTRAL:     RSI extreme  OR  conflicting indicators  OR  warmup not complete
```

**Confidence score:**  
Weighted combination of RSI distance from 50, price distance from VWAP, and whether RSI just crossed a threshold (fresh signal bonus). Bot only acts when `confidence >= MIN_SIGNAL_CONFIDENCE`.

**Startup preload:**  
Fetches last 50 closed candles from Binance REST on launch to instantly warm up RSI and VWAP — no waiting for the live stream to accumulate data.

**Shared parameters:**

| Variable | Description |
|---|---|
| `RSI_PERIOD` | Candles for RSI (default: `14`) |
| `RSI_OVERBOUGHT` | RSI above this → avoid UP entries (default: `70`) |
| `RSI_OVERSOLD` | RSI below this → avoid DOWN entries (default: `30`) |
| `RSI_BULL_THRESHOLD` | RSI must exceed this for UP signal (default: `52`) |
| `RSI_BEAR_THRESHOLD` | RSI must be below this for DOWN signal (default: `48`) |
| `MIN_SIGNAL_CONFIDENCE` | Min confidence score to act, 0.0–1.0 (default: `0.55`) |
| `SIGNAL_CANDLE_INTERVAL` | Binance kline interval: `1m` \| `3m` \| `5m` \| `15m` (default: `5m`) |

**Per-asset parameters:**

| Variable | Description |
|---|---|
| `{ASSET}_RSI_VWAP_PRICE_RANGE` | Token price must be in this range to buy (e.g. `0.30-0.55`) |
| `{ASSET}_RSI_VWAP_AMOUNT` | USDC per buy |

---

## Shared Configuration

| Variable | Description |
|---|---|
| `POLY_PRIVATE_KEY` | EOA signer private key (`0x...`) |
| `FUNDER_ADDRESS` | Proxy wallet address — holds USDC funds |
| `POLY_RPC` | Polygon RPC URL |
| `SIGNATURE_TYPE` | `2` = proxy wallet (recommended) · `0` = EOA · `1` = Magic |
| `POLY_API_KEY` | CLOB API key (auto-generated by `setup.py`) |
| `POLY_API_SECRET` | CLOB API secret (auto-generated) |
| `POLY_API_PASSPHRASE` | CLOB API passphrase (auto-generated) |
| `BUY_ORDER_TYPE` | `FAK` \| `FOK` \| `GTC` |
| `SELL_ORDER_TYPE` | `FAK` \| `FOK` \| `GTC` — bracket orders, Strategy 1 only |
| `GTC_TIMEOUT_SECONDS` | Auto-cancel GTC after N seconds. `null` = never |
| `FOK_GTC_FALLBACK` | `true` = retry FOK as GTC on low-liquidity failure |
| `WSS_READY_TIMEOUT` | Seconds to wait for WebSocket before REST fallback |
| `CLAIM_CHECK_INTERVAL` | Seconds between auto-claim checks (`auto_claim.py`) |

---

## Price Feed Architecture

All bots use a two-layer feed with automatic failover:

1. **WebSocket (primary)** — `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Prices served from in-memory cache at sub-millisecond latency.
2. **REST fallback** — `GET /midpoint` per token. Used only when WSS is disconnected or not yet ready.

Tick size boundaries (`0.01 → 0.001` near 0.04 / 0.96) are synced live from the stream.

---

## How Credential Derivation Works

Polymarket uses **L1 authentication** — API credentials are derived deterministically from your private key via EIP-712 signing:

- No manual registration on Polymarket required
- The same private key always produces the same credentials
- Regenerate at any time: `python setup.py --regen-keys`

---

## Security

- **Never commit `.env`** — it is in `.gitignore` by default
- Keep `POLY_PRIVATE_KEY` secret at all times
- Use a **dedicated wallet with limited funds** — never your main wallet
- Credentials are stored locally only, never transmitted to third parties

---

## Dependencies

| Package | Purpose |
|---|---|
| `py-clob-client` | Polymarket CLOB API client |
| `web3` | Blockchain interaction + EIP-712 signing |
| `eth-abi` | ABI encoding for Relayer payloads |
| `python-dotenv` | `.env` file management |
| `requests` | HTTP requests |
| `websocket-client` | WebSocket connections (Polymarket + Binance) |
| `questionary` | Interactive terminal prompts |

```bash
pip install -r requirements.txt
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.