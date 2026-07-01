"""Thin perp DEX client — talks straight to a core node over JSON-RPC.

A DEX tx is `0x<cmd byte><utf8 JSON>` sent to DEX_ADDRESS (ported from
nitro-testnode/tests/helpers.js). Field encoding is per-command, learned by
probing a live node:

  - PERP_ORDER price/quantity  -> HUMAN decimal STRING ("39960", "0.01").
    The wire field is a decimal type; the engine scales by 1e18 itself.
    Sending wei here double-scales and trips the 1e29 price cap.
  - PERP_DEPOSIT / TOKEN_TRANSFER amount -> WEI integer STRING (uint256).
    Bare numbers > 2^53 are rejected, so these go as quoted strings.
  - marketId / side / leverage / timeInForce -> small bare ints.

Prices read back from the node (lvl2 levels, oracle markPrice/1e18) are HUMAN
units, matching the order price scale.
"""
import json
import threading
import time
from decimal import Decimal

DEX_ADDRESS = "0x00000000000000000000000000000000000000cc"


class RateLimiter:
    """Global pacer: hands out evenly-spaced submission slots so the AGGREGATE
    rate across all worker threads is steady — no thundering-herd burst and no
    async-backlog flush (the client never outruns the sequencer). Each worker
    calls wait() right before a submission. rate_per_s <= 0 disables pacing."""

    def __init__(self, rate_per_s):
        self.dt = 1.0 / rate_per_s if rate_per_s > 0 else 0.0
        self._lock = threading.Lock()
        self._next = None

    def wait(self):
        if self.dt <= 0:
            return
        with self._lock:
            now = time.time()
            slot = self._next if (self._next and self._next > now) else now
            self._next = slot + self.dt   # reserve the slot; catch up if we fell behind
        delay = slot - time.time()
        if delay > 0:
            time.sleep(delay)             # sleep outside the lock so threads don't serialize
REGISTRY_ADDR = "0x00000000000000000000000000000000000000cf"
WEI = 10**18
DEFAULT_BAND_BPS = 800  # ±8%, engine DefaultPriceBandBps

# DEX command bytes (helpers.js CMD)
CMD_TOKEN_TRANSFER = 0x11
CMD_PERP_DEPOSIT = 0x12
CMD_PERP_ORDER = 0x41
CMD_PERP_CANCEL = 0x42
CMD_PERP_CANCEL_ALL = 0x43
CMD_PERP_SET_LEVERAGE = 0x45
CMD_PERP_SET_MARGIN_TYPE = 0x46
CMD_PERP_ORACLE_PRICE = 0x4C        # PerpOraclePriceUpdate (single market)
CMD_PERP_ORACLE_PRICE_BATCH = 0x4D  # PerpOraclePriceBatchUpdate (<=64 markets)

ARB_OWNER = "0x0000000000000000000000000000000000000070"        # addOracleSubmitter (ChainOwner)
ARB_OWNER_PUBLIC = "0x000000000000000000000000000000000000006B"  # isOracleSubmitter (read)
MAX_ORACLE_BATCH = 64  # MaxPerpOraclePriceBatchSize

BUY, SELL = 0, 1
GTC, IOC, POST, MARKET = 0, 1, 2, 3  # timeInForce (dex_perp.go)

# getMarket(uint64) return tuple — current 21-field ABI (dev + qa).
_MKT_TYPES = ("uint64", "string", "uint8", "uint16", "uint16", "uint8", "int256",
              "int256", "uint64", "int16", "uint8", "int256", "int256", "int256",
              "int256", "uint16", "int256", "int256", "int256", "int256", "uint64")
_MKT_NAMES = ("marketId", "symbol", "maxLeverage", "initialMarginRate",
              "maintenanceMarginRate", "marginModeRestriction", "makerFee", "takerFee",
              "fundingInterval", "maxFundingRate", "status", "tickSize", "lotSize",
              "minNotional", "maxOpenInterest", "priceBandBps", "baseInterestRate",
              "clampMin", "clampMax", "impactNotional", "maxAccumGap")


def to_wei_str(x):
    """Human value -> integer-wei string (deposit/transfer amounts). Decimal, not
    float: 5000 * 1e18 in float64 loses precision past 2^53."""
    return str(int(Decimal(str(x)) * WEI))


def dec_str(x):
    """Human value -> plain decimal string (order price/qty). No sci-notation."""
    return format(Decimal(str(x)), "f")


def encode_dex_input(cmd_byte, payload):
    """`0x<cmd><utf8 json>`. Pre-typed dict: str->quoted, int->bare, bool->lower."""
    js = json.dumps(payload, separators=(",", ":"))
    return "0x" + bytes([cmd_byte]).hex() + js.encode("utf8").hex()


def band_bounds(mark, bps):
    """(lower, upper) human price band around mark; (None, None) if mark<=0."""
    if not mark or mark <= 0:
        return None, None
    f = (bps or DEFAULT_BAND_BPS) / 10000.0
    return mark * (1 - f), mark * (1 + f)


def load_role_config(path, role):
    """Merge top-level common keys with the role's ('maker'/'taker') overrides."""
    cfg = json.load(open(path))
    common = {k: v for k, v in cfg.items() if k not in ("maker", "taker")}
    return {**common, **cfg.get(role, {})}


def _to_int(v):
    if v is None:
        return 0
    if isinstance(v, str):
        return int(v, 16) if v.lstrip("-").startswith("0x") else int(v or "0")
    return int(v)


class PerpDexClient:
    def __init__(self, rpc_url, private_key, dex_address=DEX_ADDRESS, gas_limit=500000, owner=None):
        from web3 import Web3
        from eth_account import Account
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.acct = Account.from_key(private_key)
        self.address = self.acct.address
        # When `owner` is set, this client signs txs with a SESSION wallet but acts
        # on behalf of `owner` (l1owner) — orders carry sessionNonce=true (Path C).
        self.is_session = owner is not None
        self.l1owner = Web3.to_checksum_address(owner) if owner else self.address
        self.dex = Web3.to_checksum_address(dex_address)
        self.registry = Web3.to_checksum_address(REGISTRY_ADDR)
        self.gas_limit = gas_limit
        self._nonce = None  # local nonce for fire-and-forget (prime() to enable)
        self._chain_id = None
        self._gas_price = None

    # ---- write path -------------------------------------------------------
    def prime(self):
        """Cache chain_id/gas_price and the pending nonce so non-waiting sends
        can pipeline without an RPC round-trip per tx. Call after setup."""
        self._chain_id = self.w3.eth.chain_id
        self._gas_price = self.w3.eth.gas_price
        self._nonce = self.w3.eth.get_transaction_count(self.address, "pending")

    def resync_nonce(self):
        self._nonce = self.w3.eth.get_transaction_count(self.address, "pending")

    def _send(self, cmd_byte, payload, wait=True):
        """wait=True: node nonce + block on receipt (setup). wait=False: local
        nonce, cached gas, fire-and-forget -> returns tx hash, no receipt."""
        if wait:
            nonce, chain_id, gas_price = (self.w3.eth.get_transaction_count(self.address),
                                          self.w3.eth.chain_id, self.w3.eth.gas_price)
        else:
            # The tx nonce is a millisecond time-nonce: the node requires it within
            # ±24h of block time. Pin it to current epoch-ms (incrementing when we
            # burst faster than 1/ms) so it never drifts "too far from block time"
            # — a sequential counter from a stale account nonce gets rejected.
            chain_id, gas_price = self._chain_id, self._gas_price
            nonce = max(int(time.time() * 1000), (self._nonce or 0) + 1)
            self._nonce = nonce
        tx = {"to": self.dex, "data": encode_dex_input(cmd_byte, payload), "gas": self.gas_limit,
              "nonce": nonce, "chainId": chain_id, "gasPrice": gas_price, "value": 0}
        raw = self.acct.sign_transaction(tx).raw_transaction
        if not wait:
            # eth_sendRawTransactionAsync enqueues and returns immediately (no
            # sequencing wait) — ~6x the per-account throughput of the sync method.
            r = self.w3.provider.make_request("eth_sendRawTransactionAsync", [self.w3.to_hex(raw)])
            if "error" in r:
                raise RuntimeError(r["error"].get("message", r["error"]))
            return r["result"]
        h = self.w3.eth.send_raw_transaction(raw)
        rcpt = self.w3.eth.wait_for_transaction_receipt(h, timeout=60)
        if rcpt.status != 1:
            raise RuntimeError(f"DEX tx 0x{cmd_byte:02x} reverted: {h.hex()}")
        return rcpt

    def deposit(self, amount, token="2"):
        return self._send(CMD_PERP_DEPOSIT, {
            "l1owner": self.address, "token": str(token), "amount": to_wei_str(amount)})

    def token_transfer(self, to, amount, token="2"):
        # spot transfer 'value' is a human-decimal field (engine scales by 1e18),
        # unlike PERP_DEPOSIT 'amount' which is raw wei.
        return self._send(CMD_TOKEN_TRANSFER, {
            "l1owner": self.address, "to": to, "value": dec_str(amount), "token": str(token)})

    def set_leverage(self, market_id, leverage):
        return self._send(CMD_PERP_SET_LEVERAGE, {
            "l1owner": self.address, "marketId": int(market_id), "leverage": int(leverage)})

    def set_margin_type(self, market_id, margin_type):
        return self._send(CMD_PERP_SET_MARGIN_TYPE, {
            "l1owner": self.address, "marketId": int(market_id), "marginType": int(margin_type)})

    def oracle_price(self, market_id, index_price, cex_price=None, wait=True):
        """Submit one market's oracle price (cmd 0x4C). index/cex are HUMAN units;
        oracle wire wants WEI (1e18) decimal strings, unlike order price. Sender must
        be a registered OracleSubmitter and equal l1owner. Mark updates in block N+1."""
        if cex_price is None:
            cex_price = index_price
        return self._send(CMD_PERP_ORACLE_PRICE, {
            "l1owner": self.address, "marketId": int(market_id),
            "indexPrice": to_wei_str(index_price), "cexPerpPrice": to_wei_str(cex_price)}, wait=wait)

    def oracle_price_batch(self, entries, wait=True):
        """Batch oracle update (cmd 0x4D). entries: [(market_id, index, cex?), ...],
        1..64, no duplicate marketId (all-or-nothing validation)."""
        if not (1 <= len(entries) <= MAX_ORACLE_BATCH):
            raise ValueError(f"batch size must be 1..{MAX_ORACLE_BATCH}, got {len(entries)}")
        out, seen = [], set()
        for e in entries:
            mid, idx = int(e[0]), e[1]
            cex = e[2] if len(e) > 2 and e[2] is not None else idx
            if mid in seen:
                raise ValueError(f"duplicate marketId {mid} in batch")
            seen.add(mid)
            out.append({"marketId": mid, "indexPrice": to_wei_str(idx), "cexPerpPrice": to_wei_str(cex)})
        return self._send(CMD_PERP_ORACLE_PRICE_BATCH, {"l1owner": self.address, "entries": out}, wait=wait)

    def is_oracle_submitter(self, addr=None):
        """Read ArbOwnerPublic.isOracleSubmitter(addr) — preflight before feeding."""
        addr = addr or self.address
        sel = self.w3.keccak(text="isOracleSubmitter(address)")[:4]
        arg = bytes.fromhex(addr[2:].rjust(64, "0"))
        out = self.w3.eth.call({"to": self.w3.to_checksum_address(ARB_OWNER_PUBLIC),
                                "data": "0x" + (sel + arg).hex()})
        return int.from_bytes(out, "big") == 1

    def add_oracle_submitter(self, addr):
        """ArbOwner.addOracleSubmitter(addr) — ChainOwner-only, V3+. Call from a
        chain-owner client to authorize a feeder. One-time setup, not per-tick."""
        sel = self.w3.keccak(text="addOracleSubmitter(address)")[:4]
        arg = bytes.fromhex(addr[2:].rjust(64, "0"))
        tx = {"to": self.w3.to_checksum_address(ARB_OWNER), "data": "0x" + (sel + arg).hex(),
              "gas": self.gas_limit, "nonce": self.w3.eth.get_transaction_count(self.address),
              "chainId": self.w3.eth.chain_id, "gasPrice": self.w3.eth.gas_price, "value": 0}
        rcpt = self.w3.eth.wait_for_transaction_receipt(
            self.w3.eth.send_raw_transaction(self.acct.sign_transaction(tx).raw_transaction), timeout=60)
        if rcpt.status != 1:
            raise RuntimeError("addOracleSubmitter reverted (not ChainOwner, or V<3?)")
        return rcpt

    def _sess(self, payload):
        if self.is_session:
            payload["sessionNonce"] = True
        return payload

    def order(self, market_id, side, price, quantity, tif=GTC, reduce_only=False, wait=True):
        # price/quantity are HUMAN decimals as strings (engine scales by 1e18)
        return self._send(CMD_PERP_ORDER, self._sess({
            "l1owner": self.l1owner, "marketId": int(market_id), "side": int(side),
            "price": dec_str(price), "quantity": dec_str(quantity),
            "isReduceOnly": bool(reduce_only), "timeInForce": int(tif)}), wait=wait)

    def cancel(self, market_id, order_id, wait=True):
        # order_id is the tx hash of the order to cancel (PerpCancelContext)
        return self._send(CMD_PERP_CANCEL, self._sess({
            "l1owner": self.l1owner, "marketId": int(market_id), "orderId": order_id}), wait=wait)

    def cancel_all(self, market_id=0, wait=True):
        return self._send(CMD_PERP_CANCEL_ALL, self._sess({
            "l1owner": self.l1owner, "marketId": int(market_id)}), wait=wait)

    def register_session(self, session_address, expiry_s, wait=True):
        """Owner registers a session wallet (EIP-712 RegisterSessionWallet, signed
        by this owner key). After this, a client constructed with the session key
        and owner=this.address can place orders for this owner via sessionNonce."""
        import base64
        from eth_account.messages import encode_typed_data
        chain_id = self.w3.eth.chain_id
        ts = int(time.time() * 1000)
        typed = {
            "domain": {"name": "DEXSignTransaction", "version": "1", "chainId": chain_id,
                       "verifyingContract": "0x0000000000000000000000000000000000000000"},
            "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                                       {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                      "RegisterSessionWallet": [{"name": "sessionWallet", "type": "address"},
                                                {"name": "expiry", "type": "uint64"}, {"name": "nonce", "type": "uint64"}]},
            "primaryType": "RegisterSessionWallet",
            "message": {"sessionWallet": session_address, "expiry": str(int(expiry_s)), "nonce": str(ts)},
        }
        sig = self.acct.sign_message(encode_typed_data(full_message=typed)).signature
        payload = {"type": 1, "publickey": session_address, "expiresAt": int(expiry_s),
                   "nonce": ts, "l1owner": self.address,
                   "l1signature": base64.b64encode(bytes(sig)).decode("ascii")}
        return self._send(0x01, payload, wait=wait)

    # ---- read path (JSON-RPC) --------------------------------------------
    def _rpc(self, method, params):
        r = self.w3.provider.make_request(method, params)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r["result"]

    def perp_account(self):
        return self._rpc("eth_getPerpAccount", [self.address, "latest"])

    def margins(self):
        """(wallet, order_margin) in human units."""
        a = self.perp_account()
        return _to_int(a.get("walletBalance")) / 1e18, _to_int(a.get("orderMargin")) / 1e18

    def positions(self):
        return self._rpc("arb_getPerpPositions", [self.address])

    def lvl2(self, market_id):
        for m in (self._rpc("arb_getPerpLvl2Data", []) or []):
            if m.get("symbol") == str(market_id):
                return m
        return None

    def best_bid_ask(self, market_id):
        m = self.lvl2(market_id)
        if not m:
            return None, None
        bids, asks = m.get("bids") or [], m.get("asks") or []
        bb = max(float(b[0]) for b in bids) if bids else None
        ba = min(float(a[0]) for a in asks) if asks else None
        return bb, ba

    def mark_price(self, market_id):
        """Mark price in human units, or None if unset."""
        for o in (self._rpc("arb_getOraclePrices", []) or []):
            if o.get("marketId") == int(market_id):
                mp = _to_int(o.get("markPrice"))
                return mp / 1e18 if mp > 0 else None
        return None

    def market_count(self):
        """Number of registered perp markets (registry getMarketCount()). Market
        ids are 1..count."""
        sel = self.w3.keccak(text="getMarketCount()")[:4]
        out = self.w3.eth.call({"to": self.registry, "data": "0x" + sel.hex()})
        return int.from_bytes(out, "big")

    def market_info(self, market_id):
        """tick/lot/min_notional/price_band_bps/max_leverage/status, or None.

        ABI differs across deployments, so try eth_abi tuple decode first, then
        fall back to raw-word slicing (offset 0x20 -> 21 body words)."""
        sel = self.w3.keccak(text="getMarket(uint64)")[:4]
        data = "0x" + (sel + int(market_id).to_bytes(32, "big")).hex()
        try:
            out = self.w3.eth.call({"to": self.registry, "data": data})
        except Exception:
            return None
        # strategy 1: full ABI decode
        try:
            from eth_abi import decode
            vals = decode(["(" + ",".join(_MKT_TYPES) + ")"], out)[0]
            return self._mkt(dict(zip(_MKT_NAMES, vals)))
        except Exception:
            pass
        # strategy 2: raw word slice
        try:
            h = out.hex()
            word = lambda i: int(h[(1 + i) * 64:(1 + i) * 64 + 64], 16)
            return self._mkt({n: word(i) for i, n in enumerate(_MKT_NAMES)})
        except Exception:
            return None

    @staticmethod
    def _mkt(d):
        return {
            "symbol": d.get("symbol"),
            "status": int(d["status"]),
            "max_leverage": int(d["maxLeverage"]),
            "tick": int(d["tickSize"]) / 1e18,
            "lot": int(d["lotSize"]) / 1e18,
            "min_notional": int(d["minNotional"]) / 1e18,
            "price_band_bps": int(d["priceBandBps"]),
        }
