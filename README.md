# Polymarket Trading Asset Bot

Automated prediction market trading bot for [Polymarket](https://polymarket.com). Monitors BTC, ETH, SOL, and XRP crypto asset markets and executes trades via the Polymarket CLOB API using two independent strategies.

---

## Project Structure

```
Polymarket-Trading-Asset-Bot/
│
├── .env                          ← Credentials + strategy parameters
├── .env.example                  ← Documented template (safe to commit)
├── .gitignore
│
├── main.py                       ← Interactive launcher — select strategy + markets
├── auto_claim.py                 ← Auto-redeem resolved positions via Relayer
├── order_executor.py             ← Shared order placement (FAK / FOK / GTC)
├── market_stream.py              ← WebSocket price feed (WSS + REST fallback)
├── setup.py                      ← One-command setup: prompt, install, generate, validate
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
    └── YES+NO_1usd/              ← Strategy 2: YES+NO arbitrage (buy both sides < $1.00)
        └── markets/
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
cd Polymarket-Trading-Asset-Bot
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

Open `.env` and fill in the parameters for the strategy you want to run. See the **Configuration** section below for all variables.

### 4. Start the bot

```bash
python main.py
```

On startup, `main.py` interactively asks:

```
? Select strategy:
  › DCA Snipe  — entry arming + bracket orders + optional DCA
    YES+NO Arbitrage  — buy UP+DOWN inside PRICE_RANGE for < $1.00

? Select markets:  (Space = toggle, Enter = confirm)
  ● BTC — Bitcoin
  ○ ETH — Ethereum
  ○ SOL — Solana
  ○ XRP — Ripple

# YES+NO only:
? Select market interval:
  › 5 minutes
    15 minutes
```

Non-interactive usage:

```bash
python main.py --strategy dca   --operate btc eth
python main.py --strategy dca   --operate all
python main.py --strategy yesno --operate btc sol --interval 15m
python main.py --strategy yesno --operate all     --interval 5m
```

### 5. Auto claim (optional)

Monitors the proxy wallet and automatically redeems any resolved positions:

```bash
python auto_claim.py
```

---

## Strategies

### Strategy 1 — DCA Snipe (`strategies/DCA_Snipe/`)

Monitors UP and DOWN prices via WebSocket. Waits for either side to cross `ENTRY_PRICE` from below (entry arming — prevents false triggers on window open). On trigger, places a FAK buy and immediately submits GTC bracket orders for take profit and stop loss. Optionally scales in with additional buys on each `BET_STEP` increment.

**Stop loss modes:**

| Config | Behavior |
|---|---|
| `STOP_LOSS=0.55` | Fixed — SL always at 0.55 |
| `STOP_LOSS_OFFSET=0.05` | Dynamic — SL = avg_price − 0.05, recalculates after each DCA |
| Both `null` | Break-even — SL = avg_price − 1 tick (zero loss guaranteed) |

**Parameters:**

| Variable | Description |
|---|---|
| `{ASSET}_ENTRY_PRICE` | Price UP or DOWN must cross from below to trigger a buy |
| `{ASSET}_AMOUNT_PER_BET` | USDC per buy — applies to initial entry and each DCA |
| `{ASSET}_TAKE_PROFIT` | GTC sell order price placed immediately after entry |
| `{ASSET}_STOP_LOSS` | Fixed stop loss price. `null` → use offset or break-even mode |
| `{ASSET}_STOP_LOSS_OFFSET` | Dynamic SL offset. `null` → use fixed or break-even mode |
| `{ASSET}_BET_STEP` | DCA step — buy again each time price rises by this amount. `null` = no DCA |
| `{ASSET}_POLL_INTERVAL` | Seconds between price check ticks |

---

### Strategy 2 — YES+NO Arbitrage (`strategies/YES+NO_1usd/`)

Monitors UP and DOWN prices simultaneously. When either side enters `PRICE_RANGE`, places a single FAK buy on that side. Maximum **1 buy per side** per window. No take profit, no stop loss, no DCA.

**Goal:** capture UP + DOWN for a combined price under $1.00. Since exactly one side always resolves to $1.00 at expiry, buying both for less than $1.00 combined guarantees profit regardless of outcome.

**Example:**
```
UP  bought @ 0.44  →  combined = 0.44 + 0.425 = 0.865
DOWN bought @ 0.425    profit/share = $1.00 − $0.865 = $0.135 ✔
```

**Parameters:**

| Variable | Description |
|---|---|
| `{ASSET}_PRICE_RANGE` | Trigger band `"low-high"`. Buys when price >= low AND <= high. Example: `"0.40-0.45"` |
| `{ASSET}_AMOUNT_TO_BUY` | USDC per side. Maximum spend per window = 2× this value (one UP + one DOWN) |

---

## Configuration

### Shared (all bots)

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
| `FOK_GTC_FALLBACK` | Retry FOK as GTC on low-liquidity failure |
| `WSS_READY_TIMEOUT` | Seconds to wait for WebSocket before REST fallback |
| `CLAIM_CHECK_INTERVAL` | Seconds between auto-claim checks (`auto_claim.py`) |

### Strategy 1 — DCA Snipe `.env` example

```dotenv
BTC_ENTRY_PRICE=0.60
BTC_AMOUNT_PER_BET=1.0
BTC_TAKE_PROFIT=0.80
BTC_STOP_LOSS=0.58
BTC_STOP_LOSS_OFFSET=null
BTC_BET_STEP=null
BTC_POLL_INTERVAL=0.1
```

### Strategy 2 — YES+NO Arbitrage `.env` example

```dotenv
BTC_PRICE_RANGE=0.40-0.45
BTC_AMOUNT_TO_BUY=1.0
```

---

## Price Feed Architecture

All bots use a two-layer price feed with automatic failover:

1. **WebSocket (primary)** — persistent connection to `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Prices are read from an in-memory cache — sub-millisecond latency.
2. **REST fallback** — `GET /midpoint` per token, used only when WSS is disconnected or not yet ready on window open.

Tick size boundaries (`0.01 → 0.001` near 0.04 / 0.96) are detected and synced live from the WebSocket stream.

---

## How Credential Derivation Works

Polymarket uses **L1 authentication** — `POLY_API_KEY`, `POLY_API_SECRET`, and `POLY_API_PASSPHRASE` are derived deterministically from your wallet's private key via EIP-712 signing. This means:

- No manual registration on Polymarket required
- The same private key always produces the same credentials
- Credentials can be regenerated at any time: `python setup.py --regen-keys`

---

## Security

- **Never commit `.env`** — it is listed in `.gitignore` by default
- Keep `POLY_PRIVATE_KEY` secret at all times
- Use a **dedicated wallet with limited funds** — never use your main wallet
- Credentials are stored locally only and never transmitted to third parties

---

## Dependencies

| Package | Purpose |
|---|---|
| `py-clob-client` | Polymarket CLOB API client |
| `web3` | Blockchain interaction + EIP-712 signing |
| `eth-abi` | ABI encoding for Relayer payloads (`auto_claim.py`) |
| `python-dotenv` | `.env` file management |
| `requests` | HTTP requests |
| `websocket-client` | WebSocket price feed (`market_stream.py`) |
| `questionary` | Interactive terminal prompts (`main.py`, YES+NO bots) |

Install all at once:

```bash
pip install -r requirements.txt
```

Or let `setup.py` handle it automatically.

---

## License

MIT License — see [LICENSE](LICENSE) for details.
