"""
auto_claim.py
-------------
Monitors the proxy wallet and redeems won positions on Polymarket.
Reads its configuration from .env (the same file used by the trading bots).

Supports two redemption methods (configurable with CLAIM_METHOD in .env):

  relayer  → Sends meta-transactions to Polymarket's official Relayer.
  (default)  signatureType=0 (EOA eth_sign). Independent of the SIGNATURE_TYPE
               used by the trading bots.

  onchain  → Sends transactions directly on-chain from the EOA.
               Only works when EOA == proxy wallet (same address).

  safe     → Executes via Gnosis Safe execTransaction on-chain.
               Use when SIGNATURE_TYPE=2 (proxy wallet setup).

FLOW:
  1. GET data-api.polymarket.com/positions  → all positions for proxy wallet
  2. Filter: (curPrice >= 0.99 OR curPrice <= 0.01) AND redeemable=true
  3. Group by conditionId (avoids duplicate redemptions)
  4. For each unique condition: redeem via chosen method

MONITORING MODES (CHECK_REAL_TIME in .env):
  true   → WebSocket real-time mode
             Subscribes to the Polymarket market price feed for all held tokens.
             Triggers redemption immediately when a price hits the resolved
             threshold (>= 0.99 or <= 0.01).
             Refreshes subscription list every POSITION_REFRESH_INTERVAL seconds
             to catch newly opened positions.

  false  → Polling mode (default)
             Checks for redeemable positions every CLAIM_CHECK_INTERVAL seconds
             (default: 60).

CONTRACTS (Polygon mainnet):
  CTF:     0x4D97DCd97eC945f40cF65F87097ACe5EA0476045
  NegRisk: 0xd91e80cf2e7be2e162c6513ced06f1dd0da35296
  USDC.e:  0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174

.env configuration variables:
  POLY_PRIVATE_KEY        EOA signer key (0x...)
  FUNDER_ADDRESS          Proxy wallet address
  POLY_RPC                Polygon RPC URL (optional)
  CLAIM_CHECK_INTERVAL    Seconds between checks in polling mode (default 60)
  CLAIM_METHOD            safe | relayer | onchain (default: relayer)
  CHECK_REAL_TIME         true | false (default: false)
"""

import logging
import threading
import time
import requests
from pathlib import Path
from dotenv import dotenv_values

_ROOT = Path(__file__).resolve().parent

# Load configuration from .env (shared with trading bots)
_CFG = dotenv_values(_ROOT / ".env")


def _cfg(key: str, default: str = "") -> str:
    """Read a variable from .env."""
    return _CFG.get(key, default).strip()


logging.basicConfig(
    level   = logging.INFO,
    format  = "[%(asctime)s][%(levelname)s][AutoClaim] - %(message)s",
    datefmt = "%H:%M:%S",
)
log = logging.getLogger("AutoClaim")

# ── Config (read from .env) ────────────────────────────────────────────────────
CHECK_INTERVAL            = int(_cfg("CLAIM_CHECK_INTERVAL", "60"))
CLAIM_METHOD              = _cfg("CLAIM_METHOD", "relayer").lower()
CHECK_REAL_TIME           = _cfg("CHECK_REAL_TIME", "false").lower() in ("true", "1", "yes")
POSITION_REFRESH_INTERVAL = 120   # seconds between subscription refreshes in WS mode

DATA_API         = "https://data-api.polymarket.com"
RELAYER_URL      = "https://relayer-v2.polymarket.com/submit"
CHAIN_ID         = 137
TX_DELAY_SECONDS = 2   # delay between consecutive transactions

# Thresholds for resolved positions
RESOLVED_HIGH  = 0.99    # won: price ~$1
RESOLVED_LOW   = 0.01    # lost: price ~$0
ZERO_THRESHOLD = 0.0001

# ── Contracts ──────────────────────────────────────────────────────────────────
CTF_ADDRESS      = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADDRESS = "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296"
USDC_ADDRESS     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ── ABIs ───────────────────────────────────────────────────────────────────────
CTF_ABI = [{"name":"redeemPositions","type":"function","inputs":[
    {"name":"collateralToken","type":"address"},
    {"name":"parentCollectionId","type":"bytes32"},
    {"name":"conditionId","type":"bytes32"},
    {"name":"indexSets","type":"uint256[]"},
],"outputs":[],"stateMutability":"nonpayable"}]

NEG_RISK_ABI = [{"name":"redeemPositions","type":"function","inputs":[
    {"name":"conditionId","type":"bytes32"},
    {"name":"amounts","type":"uint256[]"},
],"outputs":[],"stateMutability":"nonpayable"}]

# Gnosis Safe — only the functions we need
SAFE_ABI = [
    {
        "name": "nonce", "type": "function",
        "inputs": [], "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "getTransactionHash", "type": "function",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "_nonce",         "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "view",
    },
    {
        "name": "execTransaction", "type": "function",
        "inputs": [
            {"name": "to",             "type": "address"},
            {"name": "value",          "type": "uint256"},
            {"name": "data",           "type": "bytes"},
            {"name": "operation",      "type": "uint8"},
            {"name": "safeTxGas",      "type": "uint256"},
            {"name": "baseGas",        "type": "uint256"},
            {"name": "gasPrice",       "type": "uint256"},
            {"name": "gasToken",       "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures",     "type": "bytes"},
        ],
        "outputs": [{"name": "success", "type": "bool"}],
        "stateMutability": "payable",
    },
]

ADDR_ZERO = "0x0000000000000000000000000000000000000000"

# ── Web3 ───────────────────────────────────────────────────────────────────────
try:
    from web3 import Web3
    from web3.middleware import ExtraDataToPOAMiddleware
    from eth_account import Account
    from eth_account.messages import encode_defunct
except ImportError:
    raise ImportError("pip install web3 --break-system-packages")


def build_web3() -> Web3:
    rpc = _cfg("POLY_RPC", "https://polygon-rpc.com") or "https://polygon-rpc.com"
    w3  = Web3(Web3.HTTPProvider(rpc))
    w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
    if not w3.is_connected():
        raise ConnectionError(f"Cannot connect to RPC: {rpc}")
    return w3


# ══════════════════════════════════════════════════════════════════════════════
#  CONDITION ID ENCODING
# ══════════════════════════════════════════════════════════════════════════════

def parse_condition_id(condition_id: str) -> bytes:
    """
    Convert conditionId string to exactly 32 bytes.
    Handles: '0x' hex strings (any length), plain hex, decimal integers.
    Always zero-pads to 32 bytes (matching TypeScript hexZeroPad(..., 32)).
    """
    cid = condition_id.strip()
    if cid.startswith(("0x", "0X")):
        hex_str = cid[2:].zfill(64)        # left-pad with zeros to 64 hex chars
        return bytes.fromhex(hex_str)
    # Try as decimal integer
    try:
        return int(cid).to_bytes(32, byteorder="big")
    except ValueError:
        pass
    # Treat as raw hex (no 0x prefix)
    return bytes.fromhex(cid.zfill(64))


def is_neg_risk(position: dict) -> bool:
    """Check all known field name variants for the NegRisk flag."""
    return any(position.get(k, False) for k in ("negRisk", "negativeRisk", "neg_risk"))


# ══════════════════════════════════════════════════════════════════════════════
#  METHOD A — DIRECT ON-CHAIN  (matches TypeScript reference)
# ══════════════════════════════════════════════════════════════════════════════

def redeem_onchain(w3: Web3, condition_id: str, account) -> bool:
    """
    Build, sign, and broadcast redeemPositions directly on Polygon.
    msg.sender = account.address

    ⚠️  This only redeems CTF tokens held BY account.address.
       Use CLAIM_METHOD=relayer if tokens live in a separate proxy wallet.
    """
    try:
        cid_bytes = parse_condition_id(condition_id)

        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS),
            abi=CTF_ABI,
        )

        # Gas price + 20% buffer
        gas_price          = w3.eth.gas_price
        adjusted_gas_price = int(gas_price * 1.20)
        log.info(f"    Gas price : {w3.from_wei(adjusted_gas_price, 'gwei'):.2f} Gwei")
        log.info(f"    Condition : 0x{cid_bytes.hex()}")
        log.info(f"    IndexSets : [1, 2]")

        nonce = w3.eth.get_transaction_count(account.address)
        tx = ctf.functions.redeemPositions(
            Web3.to_checksum_address(USDC_ADDRESS),
            b"\x00" * 32,   # parentCollectionId = bytes32(0)
            cid_bytes,
            [1, 2],         # indexSets — both outcome collections
        ).build_transaction({
            "from"    : account.address,
            "gas"     : 500_000,
            "gasPrice": adjusted_gas_price,
            "nonce"   : nonce,
            "chainId" : CHAIN_ID,
        })

        signed   = account.sign_transaction(tx)
        raw_tx   = getattr(signed, "rawTransaction", None) or signed.raw_transaction
        tx_hash  = w3.eth.send_raw_transaction(raw_tx)

        log.info(f"    Submitted : {tx_hash.hex()}")
        log.info(f"    Waiting for confirmation...")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            log.info(f"    ✅ Success! Gas used: {receipt['gasUsed']}")
            return True
        else:
            log.error("    ❌ Transaction reverted")
            return False

    except Exception as exc:
        log.error(f"    ❌ On-chain error: {exc}", exc_info=True)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  METHOD B — RELAYER  (for Gnosis Safe / proxy wallet setups)
# ══════════════════════════════════════════════════════════════════════════════

def encode_redeem_calldata(w3: Web3, condition_id: str) -> str:
    """CTF.redeemPositions(USDC, bytes32(0), conditionId, [1, 2])"""
    ctf      = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    cid      = parse_condition_id(condition_id)
    calldata = ctf.encode_abi("redeemPositions",
                              args=[Web3.to_checksum_address(USDC_ADDRESS), b"\x00" * 32, cid, [1, 2]])
    raw = calldata if isinstance(calldata, str) else calldata.hex()
    return "0x" + raw.removeprefix("0x")


def encode_neg_risk_calldata(w3: Web3, condition_id: str, size_usdc: float) -> str:
    """NegRiskAdapter.redeemPositions(conditionId, [amount, amount])"""
    neg      = w3.eth.contract(address=Web3.to_checksum_address(NEG_RISK_ADDRESS), abi=NEG_RISK_ABI)
    cid      = parse_condition_id(condition_id)
    amount   = int(size_usdc * 1_000_000)
    calldata = neg.encode_abi("redeemPositions", args=[cid, [amount, amount]])
    raw = calldata if isinstance(calldata, str) else calldata.hex()
    return "0x" + raw.removeprefix("0x")


def sign_calldata(private_key: str, data_hex: str) -> str:
    """
    signatureType=0: signs keccak256(calldata) via eth_sign (personal_sign prefix).
    "\\x19Ethereum Signed Message:\\n32" + keccak256(calldata)
    """
    data_bytes = bytes.fromhex(data_hex.removeprefix("0x"))
    msg_hash   = Web3.keccak(data_bytes)
    signable   = encode_defunct(primitive=msg_hash)
    signed     = Account.from_key(private_key).sign_message(signable)
    sig_hex    = signed.signature.hex()
    return sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex


def submit_to_relayer(eoa_address: str, proxy_wallet: str, to: str,
                      data_hex: str, nonce: int, signature: str) -> dict | None:
    """POST https://relayer-v2.polymarket.com/submit  (type=EOA, signatureType=0)"""
    payload = {
        "data"       : data_hex,
        "from"       : Web3.to_checksum_address(eoa_address),
        "metadata"   : "",
        "nonce"      : str(nonce),
        "proxyWallet": Web3.to_checksum_address(proxy_wallet),
        "signature"  : signature,
        "to"         : Web3.to_checksum_address(to),
        "type"       : "EOA",
    }

    log.info(f"    Submitting to relayer | nonce={nonce} | type=EOA")
    log.info(f"      to  = {to}")
    log.info(f"      data= {data_hex[:22]}...")
    log.info(f"      sig = {signature[:22]}...")

    try:
        resp = requests.post(
            RELAYER_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        if resp.status_code in (200, 201):
            result = resp.json()
            log.info(f"    ✅ Relayer accepted!")
            log.info(f"      txID   = {result.get('transactionID','')}")
            log.info(f"      txHash = {result.get('transactionHash','')}")
            log.info(f"      state  = {result.get('state','')}")
            return result
        else:
            log.error(f"    ❌ Relayer rejected: HTTP {resp.status_code} — {resp.text[:400]}")
            return None
    except Exception as exc:
        log.error(f"    ❌ Relayer request failed: {exc}")
        return None


def redeem_via_relayer(w3: Web3, condition_id: str, size: float,
                       neg_risk: bool, private_key: str,
                       eoa_address: str, proxy_wallet: str) -> bool:
    try:
        if neg_risk:
            data_hex = encode_neg_risk_calldata(w3, condition_id, size)
            to       = NEG_RISK_ADDRESS
        else:
            data_hex = encode_redeem_calldata(w3, condition_id)
            to       = CTF_ADDRESS

        signature = sign_calldata(private_key, data_hex)
        log.info(f"    sig = {signature[:22]}...")

        result = submit_to_relayer(
            eoa_address  = eoa_address,
            proxy_wallet = proxy_wallet,
            to           = to,
            data_hex     = data_hex,
            nonce        = 0,
            signature    = signature,
        )
        return result is not None

    except Exception as exc:
        log.error(f"    ❌ Relayer encode/sign error: {exc}", exc_info=True)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  METHOD C — GNOSIS SAFE execTransaction  (for SIGNATURE_TYPE=2 proxy wallets)
# ══════════════════════════════════════════════════════════════════════════════

def redeem_via_safe(w3: Web3, condition_id: str, size: float, neg_risk: bool,
                    private_key: str, eoa_address: str, proxy_wallet: str) -> bool:
    """
    Executes redeemPositions through the proxy wallet's Gnosis Safe contract.

    Flow:
      1. Encode CTF/NegRisk redeemPositions calldata
      2. Call Safe.getTransactionHash(...) to obtain the EIP-712 hash
      3. Sign the hash with the EOA (eth_sign, v=31/32 for Gnosis Safe)
      4. Call Safe.execTransaction(...) on-chain from the EOA
    """
    try:
        # 1. Redemption calldata
        if neg_risk:
            data_hex = encode_neg_risk_calldata(w3, condition_id, size)
            to       = NEG_RISK_ADDRESS
        else:
            data_hex = encode_redeem_calldata(w3, condition_id)
            to       = CTF_ADDRESS

        data_bytes = bytes.fromhex(data_hex.removeprefix("0x"))
        to_cs      = Web3.to_checksum_address(to)

        # 2. Safe contract instance
        safe = w3.eth.contract(
            address=Web3.to_checksum_address(proxy_wallet),
            abi=SAFE_ABI,
        )

        # 3. Current Safe nonce
        safe_nonce = safe.functions.nonce().call()
        log.info(f"    Safe nonce : {safe_nonce}")

        # 4. Safe transaction hash (EIP-712 on-chain)
        safe_tx_hash: bytes = safe.functions.getTransactionHash(
            to_cs,          # to
            0,              # value
            data_bytes,     # data
            0,              # operation  (0 = CALL)
            0,              # safeTxGas
            0,              # baseGas
            0,              # gasPrice
            ADDR_ZERO,      # gasToken
            ADDR_ZERO,      # refundReceiver
            safe_nonce,     # _nonce
        ).call()

        log.info(f"    Safe tx hash: 0x{bytes(safe_tx_hash).hex()}")

        # 5. eth_sign over the Safe tx hash
        #    Gnosis Safe accepts v=31/32 for eth_sign (v_raw + 4)
        signable = encode_defunct(primitive=bytes(safe_tx_hash))
        signed   = Account.from_key(private_key).sign_message(signable)
        v        = signed.v + 4   # 27→31 or 28→32 (eth_sign type for Gnosis Safe)
        packed_sig = (
            signed.r.to_bytes(32, "big") +
            signed.s.to_bytes(32, "big") +
            bytes([v])
        )
        log.info(f"    Signature  : 0x{packed_sig.hex()[:22]}... (v={v})")

        # 6. Send execTransaction from the EOA
        gas_price          = w3.eth.gas_price
        adjusted_gas_price = int(gas_price * 1.20)
        log.info(f"    Gas price  : {w3.from_wei(adjusted_gas_price, 'gwei'):.2f} Gwei")

        nonce = w3.eth.get_transaction_count(eoa_address)
        tx = safe.functions.execTransaction(
            to_cs,          # to
            0,              # value
            data_bytes,     # data
            0,              # operation
            0,              # safeTxGas
            0,              # baseGas
            0,              # gasPrice
            ADDR_ZERO,      # gasToken
            ADDR_ZERO,      # refundReceiver
            packed_sig,     # signatures
        ).build_transaction({
            "from"    : eoa_address,
            "gas"     : 500_000,
            "gasPrice": adjusted_gas_price,
            "nonce"   : nonce,
            "chainId" : CHAIN_ID,
        })

        account    = Account.from_key(private_key)
        signed_tx  = account.sign_transaction(tx)
        raw_tx     = getattr(signed_tx, "rawTransaction", None) or signed_tx.raw_transaction
        tx_hash    = w3.eth.send_raw_transaction(raw_tx)

        log.info(f"    Submitted : {tx_hash.hex()}")
        log.info(f"    Waiting for confirmation...")

        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt["status"] == 1:
            log.info(f"    ✅ Success! Gas used: {receipt['gasUsed']}")
            return True
        else:
            log.error("    ❌ execTransaction reverted")
            return False

    except Exception as exc:
        log.error(f"    ❌ Safe execution error: {exc}", exc_info=True)
        return False


# ══════════════════════════════════════════════════════════════════════════════
#  POSITION DISCOVERY  (client-side filtering)
# ══════════════════════════════════════════════════════════════════════════════

def fetch_all_positions(wallet: str) -> list:
    """
    Fetch all positions for wallet without API-side filters.
    Filters out dust positions client-side.
    """
    try:
        resp = requests.get(
            f"{DATA_API}/positions",
            params={"user": wallet, "limit": "500"},
            timeout=15,
        )
        resp.raise_for_status()
        data      = resp.json()
        positions = data if isinstance(data, list) else data.get("positions", [])
        return [p for p in positions if float(p.get("size", 0)) > ZERO_THRESHOLD]
    except Exception as exc:
        log.warning(f"fetch_positions error: {exc}")
        return []


def get_redeemable_positions(wallet: str) -> tuple[list, list]:
    """
    Return (redeemable, active) position lists.
    Redeemable: curPrice >= RESOLVED_HIGH or curPrice <= RESOLVED_LOW, AND redeemable=True
    """
    all_positions = fetch_all_positions(wallet)

    redeemable = [
        p for p in all_positions
        if (float(p.get("curPrice", 0.5)) >= RESOLVED_HIGH or
            float(p.get("curPrice", 0.5)) <= RESOLVED_LOW)
        and p.get("redeemable") is True
    ]

    active = [
        p for p in all_positions
        if RESOLVED_LOW < float(p.get("curPrice", 0.5)) < RESOLVED_HIGH
    ]

    log.info(f"  Total positions       : {len(all_positions)}")
    log.info(f"  Resolved & redeemable : {len(redeemable)}")
    log.info(f"  Active (not touching) : {len(active)}")

    return redeemable, active


# ══════════════════════════════════════════════════════════════════════════════
#  REDEMPTION EXECUTOR  (shared between polling and real-time modes)
# ══════════════════════════════════════════════════════════════════════════════

def execute_redemptions(proxy_wallet: str, eoa_address: str, w3: Web3,
                        account, private_key: str, already_claimed: set,
                        use_onchain: bool, redeemable: list) -> None:
    """
    Given a list of redeemable positions, group by conditionId and redeem each
    condition not yet claimed this session.
    """
    if not redeemable:
        log.info("  No redeemable positions found.")
        return

    # Group by conditionId — one redemption covers ALL outcomes for a condition
    by_condition: dict[str, list] = {}
    for pos in redeemable:
        cid = pos.get("conditionId", "")
        if cid:
            by_condition.setdefault(cid, []).append(pos)

    pending = {cid: grp for cid, grp in by_condition.items()
               if cid not in already_claimed}

    if not pending:
        log.info(f"  {len(by_condition)} condition(s) — all already processed this session.")
        return

    log.info(f"  Grouped into {len(by_condition)} unique condition(s)")
    log.info(f"  *** {len(pending)} condition(s) to redeem ***")

    success_count = 0
    fail_count    = 0
    total_value   = 0.0

    for idx, (cid, group) in enumerate(pending.items(), 1):
        grp_value    = sum(float(p.get("currentValue", 0)) for p in group)
        total_value += grp_value

        log.info(f"\n{'=' * 60}")
        log.info(f"  Condition {idx}/{len(pending)}: {cid}")
        log.info(f"  Positions in condition : {len(group)}")
        log.info(f"  Expected value         : ${grp_value:.4f}")

        for pos in group:
            price  = float(pos.get("curPrice", 0))
            status = "WIN" if price >= RESOLVED_HIGH else "LOSS"
            title  = (pos.get("title") or pos.get("slug") or pos.get("asset") or "")[:55]
            log.info(f"    [{status}] {title}")
            log.info(f"      outcome={pos.get('outcome','')} | size={float(pos.get('size',0)):.4f}"
                     f" | price={price:.4f} | redeemable={pos.get('redeemable')}")

        rep  = group[0]
        neg  = is_neg_risk(rep)
        size = float(rep.get("size", 0))

        if use_onchain:
            success = redeem_onchain(w3, cid, account)
        elif CLAIM_METHOD == "safe":
            success = redeem_via_safe(
                w3, cid, size, neg, private_key, eoa_address, proxy_wallet
            )
        else:
            success = redeem_via_relayer(
                w3, cid, size, neg, private_key, eoa_address, proxy_wallet
            )

        if success:
            already_claimed.add(cid)
            success_count += 1
        else:
            fail_count += 1
            log.warning(f"  Will retry condition {cid[:20]}... next cycle")

        if idx < len(pending):
            log.info(f"  Waiting {TX_DELAY_SECONDS}s before next transaction...")
            time.sleep(TX_DELAY_SECONDS)

    log.info(f"\n{'=' * 60}")
    log.info(f"  Cycle: {success_count} success | {fail_count} failed"
             + (f" | Expected value redeemed: ${total_value:.4f}" if success_count else ""))


# ══════════════════════════════════════════════════════════════════════════════
#  POLLING MODE  (CHECK_REAL_TIME=false)
# ══════════════════════════════════════════════════════════════════════════════

def run_polling(proxy_wallet: str, eoa_address: str, w3: Web3,
                account, private_key: str, use_onchain: bool) -> None:
    """
    Checks for redeemable positions every CHECK_INTERVAL seconds.
    """
    already_claimed: set = set()

    log.info(f"  Mode           : POLLING (every {CHECK_INTERVAL}s)")
    log.info("=" * 60)

    while True:
        try:
            log.info(f"Checking {proxy_wallet[:12]}... for redeemable positions")
            redeemable, _ = get_redeemable_positions(proxy_wallet)
            execute_redemptions(
                proxy_wallet    = proxy_wallet,
                eoa_address     = eoa_address,
                w3              = w3,
                account         = account,
                private_key     = private_key,
                already_claimed = already_claimed,
                use_onchain     = use_onchain,
                redeemable      = redeemable,
            )
        except Exception as exc:
            log.error(f"Check cycle error: {exc}", exc_info=True)
        log.info(f"Next check in {CHECK_INTERVAL}s ...")
        time.sleep(CHECK_INTERVAL)


# ══════════════════════════════════════════════════════════════════════════════
#  REAL-TIME MODE  (CHECK_REAL_TIME=true)
# ══════════════════════════════════════════════════════════════════════════════

class RealTimeMonitor:
    """
    Subscribes to Polymarket's market price feed (WebSocket) for all held
    token positions.

    When a token price reaches the resolved threshold (>= 0.99 or <= 0.01),
    immediately fetches positions from the REST API to confirm redeemable=True,
    then executes redemption.

    A background timer refreshes the subscription list every
    POSITION_REFRESH_INTERVAL seconds to include positions opened after startup.
    """

    def __init__(self, proxy_wallet: str, eoa_address: str, w3: Web3,
                 account, private_key: str, use_onchain: bool):
        self.proxy_wallet  = proxy_wallet
        self.eoa_address   = eoa_address
        self.w3            = w3
        self.account       = account
        self.private_key   = private_key
        self.use_onchain   = use_onchain

        self._already_claimed:    set  = set()
        self._pending_redeem:     set  = set()   # conditions currently being redeemed
        self._token_to_condition: dict = {}       # token_id → conditionId
        self._lock       = threading.Lock()
        self._stop_event = threading.Event()
        self._stream     = None                   # MarketStream instance

    # ── WebSocket price callback ───────────────────────────────────────────────

    def _on_price_update(self, token_id: str, price: float) -> None:
        """Called by MarketStream on every price update for any subscribed token."""
        if RESOLVED_LOW < price < RESOLVED_HIGH:
            return  # position not yet resolved

        with self._lock:
            cid = self._token_to_condition.get(token_id)
            if not cid:
                return
            if cid in self._already_claimed or cid in self._pending_redeem:
                return
            self._pending_redeem.add(cid)

        result_label = "WIN ($1)" if price >= RESOLVED_HIGH else "LOSS ($0)"
        log.info(f"[WS] Price trigger: {token_id[:20]}... → {price:.4f} ({result_label})")
        log.info(f"[WS] Queuing condition {cid[:20]}... for immediate redemption")

        # Spawn a thread so the WebSocket callback is not blocked
        t = threading.Thread(
            target=self._try_redeem_condition,
            args=(cid,),
            daemon=True,
            name=f"Redeem-{cid[:12]}",
        )
        t.start()

    # ── Redemption handler ─────────────────────────────────────────────────────

    def _try_redeem_condition(self, cid: str) -> None:
        """Re-confirm redeemable=True via REST, then execute redemption."""
        try:
            # Re-fetch to confirm the API has marked the position as redeemable
            positions = fetch_all_positions(self.proxy_wallet)
            group = [
                p for p in positions
                if p.get("conditionId") == cid and p.get("redeemable") is True
            ]

            if not group:
                log.info(f"[WS] Condition {cid[:20]}... not yet marked redeemable — will retry on next price update")
                with self._lock:
                    self._pending_redeem.discard(cid)
                return

            rep  = group[0]
            neg  = is_neg_risk(rep)
            size = float(rep.get("size", 0))

            grp_value = sum(float(p.get("currentValue", 0)) for p in group)
            title     = (rep.get("title") or rep.get("slug") or "")[:55]
            log.info(f"\n{'=' * 60}")
            log.info(f"[WS] Redeeming: {title}")
            log.info(f"[WS] Condition : {cid}")
            log.info(f"[WS] Expected  : ${grp_value:.4f}")

            if self.use_onchain:
                success = redeem_onchain(self.w3, cid, self.account)
            elif CLAIM_METHOD == "safe":
                success = redeem_via_safe(
                    self.w3, cid, size, neg, self.private_key,
                    self.eoa_address, self.proxy_wallet,
                )
            else:
                success = redeem_via_relayer(
                    self.w3, cid, size, neg, self.private_key,
                    self.eoa_address, self.proxy_wallet,
                )

            with self._lock:
                self._pending_redeem.discard(cid)
                if success:
                    self._already_claimed.add(cid)

        except Exception as exc:
            log.error(f"[WS] Redemption error for {cid[:20]}...: {exc}", exc_info=True)
            with self._lock:
                self._pending_redeem.discard(cid)

    # ── Subscription management ────────────────────────────────────────────────

    def _refresh_subscriptions(self) -> None:
        """
        Fetch current positions and subscribe any new token IDs to the
        market WebSocket stream.
        """
        try:
            positions  = fetch_all_positions(self.proxy_wallet)
            new_tokens = []

            with self._lock:
                for pos in positions:
                    # 'asset' is the ERC-1155 token ID used by the market channel
                    token_id = pos.get("asset") or pos.get("asset_id") or pos.get("tokenId")
                    cid      = pos.get("conditionId", "")
                    if token_id and cid and token_id not in self._token_to_condition:
                        self._token_to_condition[token_id] = cid
                        new_tokens.append(token_id)

            if new_tokens:
                log.info(f"[WS] Subscribing to {len(new_tokens)} new token(s)")
                if self._stream:
                    self._stream.add_tokens(new_tokens)

        except Exception as exc:
            log.warning(f"[WS] Subscription refresh error: {exc}")

    # ── Main entry point ───────────────────────────────────────────────────────

    def run(self) -> None:
        """
        Start the WebSocket stream and the subscription refresh loop.
        Blocks until stop() is called or a KeyboardInterrupt.
        """
        from market_stream import MarketStream

        # Initial position load
        log.info("[WS] Loading initial positions...")
        self._refresh_subscriptions()

        with self._lock:
            initial_tokens = list(self._token_to_condition.keys())

        if not initial_tokens:
            log.warning("[WS] No positions found at startup — will subscribe as positions are opened")

        log.info(f"[WS] Starting market stream | {len(initial_tokens)} token(s) subscribed")

        self._stream = MarketStream(
            asset_ids        = initial_tokens,
            on_price_update  = self._on_price_update,
        )
        self._stream.start()

        if initial_tokens:
            ready = self._stream.wait_ready(timeout=15)
            if ready:
                log.info("[WS] Stream ready — real-time monitoring active")
            else:
                log.warning("[WS] Stream not ready within 15s, continuing anyway")
        else:
            log.info("[WS] Stream started — waiting for positions to subscribe")

        log.info("[WS] Performing initial scan for already-redeemable positions...")
        positions = fetch_all_positions(self.proxy_wallet)
        redeemable = [
            p for p in positions
            if (float(p.get("curPrice", 0.5)) >= RESOLVED_HIGH or
                float(p.get("curPrice", 0.5)) <= RESOLVED_LOW)
            and p.get("redeemable") is True
        ]
        if redeemable:
            log.info(f"[WS] Found {len(redeemable)} already-redeemable position(s) — processing now")
            execute_redemptions(
                proxy_wallet    = self.proxy_wallet,
                eoa_address     = self.eoa_address,
                w3              = self.w3,
                account         = self.account,
                private_key     = self.private_key,
                already_claimed = self._already_claimed,
                use_onchain     = self.use_onchain,
                redeemable      = redeemable,
            )
        else:
            log.info("[WS] No pending redemptions at startup")

        last_refresh = time.time()

        try:
            while not self._stop_event.is_set():
                if time.time() - last_refresh >= POSITION_REFRESH_INTERVAL:
                    log.info(f"[WS] Refreshing position subscriptions...")
                    self._refresh_subscriptions()
                    last_refresh = time.time()
                self._stop_event.wait(timeout=5)
        except KeyboardInterrupt:
            pass
        finally:
            log.info("[WS] Stopping stream...")
            self._stream.stop()
            log.info("[WS] Real-time monitor stopped.")

    def stop(self) -> None:
        self._stop_event.set()


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def run():
    private_key  = _cfg("POLY_PRIVATE_KEY")
    proxy_wallet = _cfg("FUNDER_ADDRESS")
    use_onchain  = (CLAIM_METHOD == "onchain")

    if not private_key:
        log.error("POLY_PRIVATE_KEY not set in .env"); return
    if not proxy_wallet:
        log.error("FUNDER_ADDRESS not set in .env"); return

    try:
        w3 = build_web3()
        log.info(f"  Polygon connected | block={w3.eth.block_number}")
    except Exception as exc:
        log.error(f"Web3 connection failed: {exc}"); return

    try:
        account     = Account.from_key(private_key)
        eoa_address = account.address
        matic       = w3.from_wei(w3.eth.get_balance(eoa_address), "ether")
        log.info(f"  EOA signer    : {eoa_address}")
        log.info(f"  MATIC balance : {matic:.4f} POL")
        if matic < 0.001:
            log.warning("⚠️  Low MATIC balance — may not cover gas fees")
    except Exception as exc:
        log.error(f"Invalid POLY_PRIVATE_KEY: {exc}"); return

    if use_onchain and eoa_address.lower() != proxy_wallet.lower():
        log.warning(f"⚠️  CLAIM_METHOD=onchain but EOA ({eoa_address}) != proxy wallet ({proxy_wallet})")
        log.warning("⚠️  onchain redeems FROM the EOA address, not from the proxy wallet.")
        log.warning("⚠️  If tokens are in the proxy wallet, use CLAIM_METHOD=safe in .env")

    method_label = {
        "safe"   : "GNOSIS SAFE (execTransaction)",
        "onchain": "ON-CHAIN (direct tx)",
        "relayer": "RELAYER (signatureType=0)",
    }.get(CLAIM_METHOD, CLAIM_METHOD)

    mode_label = (
        f"REAL-TIME (WebSocket + {POSITION_REFRESH_INTERVAL}s subscription refresh)"
        if CHECK_REAL_TIME
        else f"POLLING (every {CHECK_INTERVAL}s)"
    )

    log.info("=" * 60)
    log.info("AutoClaim starting")
    log.info(f"  Proxy wallet   : {proxy_wallet}")
    log.info(f"  EOA signer     : {eoa_address}")
    log.info(f"  Method         : {method_label}")
    log.info(f"  Mode           : {mode_label}")
    log.info("=" * 60)

    if CHECK_REAL_TIME:
        monitor = RealTimeMonitor(
            proxy_wallet = proxy_wallet,
            eoa_address  = eoa_address,
            w3           = w3,
            account      = account,
            private_key  = private_key,
            use_onchain  = use_onchain,
        )
        monitor.run()
    else:
        run_polling(
            proxy_wallet = proxy_wallet,
            eoa_address  = eoa_address,
            w3           = w3,
            account      = account,
            private_key  = private_key,
            use_onchain  = use_onchain,
        )


if __name__ == "__main__":
    run()
