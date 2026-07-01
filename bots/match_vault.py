#!/usr/bin/env python3
"""Vault-as-maker via session keys; plain accounts as takers.

A single vault (owner with margin) provides liquidity: K session wallets are
registered to the vault and POST a resting ladder on its behalf (l1owner=vault,
sessionNonce=true). A fleet of taker accounts cross those quotes with IOC orders,
so fills accrue to the vault. Submissions use async send + the global pacer.

  python -m bots.match_vault [configs/config.json] [target_tx_s] [duration_s]

config (maker block): the maker key is the VAULT owner. Knobs: vault_sessions
(default 4), taker_count (default = sessions), spread, taker_slippage, order_size,
match_deposit (per taker). Vault must already hold perp margin; takers are funded
from the vault (perp-withdraw -> transfer -> deposit).
"""
import math
import sys
import threading
import time

from eth_account import Account

from lib.accounts import load_or_create
from lib.dex import (PerpDexClient, RateLimiter, to_wei_str, BUY, SELL, POST, IOC,
                 DEFAULT_BAND_BPS, band_bounds, load_role_config, _to_int)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.json"
    cfg = load_role_config(path, "maker")
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("vault_tps", 40))
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("match_duration", 0))
    mid = cfg["market_id"]
    spread = float(cfg.get("spread", 0.001))
    slip = float(cfg.get("taker_slippage", 0.01))
    size = float(cfg.get("order_size", 0.01))
    n_sess = int(cfg.get("vault_sessions", 4))
    n_take = int(cfg.get("taker_count", n_sess))
    taker_deposit = float(cfg.get("match_deposit", 500))
    keystore = cfg.get("keystore", "keystores/accounts.json")
    expiry_s = int(time.time()) + 24 * 3600

    vault = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])  # owner (has margin)
    mi = vault.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    mark = vault.mark_price(mid) or float(cfg["ref_price"])
    lo, hi = band_bounds(mark, band_bps)
    q = lambda x: round(x / tick) * tick
    mk_ask, mk_bid = q(mark * (1 + spread)), q(mark * (1 - spread))
    tk_buy = min(math.floor(hi / tick) * tick - tick, q(mark * (1 + slip)))
    tk_sell = max(math.ceil(lo / tick) * tick + tick, q(mark * (1 - slip)))
    tk_mid = q(mark)  # non-crossing IOC (tx load, no fill)
    cross_every = max(1, int(cfg.get("taker_cross_every", 1)))       # same knob as match.py
    maker_cancel_every = max(1, int(cfg.get("maker_cancel_every", 6)))
    vwallet = _to_int(vault.perp_account().get("walletBalance")) / 1e18
    print(f"vault {vault.address} margin={vwallet:.0f} market={mid} mark={mark} "
          f"mk_bid/ask={mk_bid}/{mk_ask} tk_buy/sell={tk_buy}/{tk_sell}")

    # K session wallets registered to the vault -> maker side
    makers = []
    for sk in load_or_create(keystore, "vault_session", n_sess):
        sw = PerpDexClient(cfg["rpc_url"], sk["key"], cfg["dex_address"], owner=vault.address)
        try:
            vault.register_session(sw.address, expiry_s)
        except Exception as e:
            if "already exists" not in str(e):
                print("register warn", sw.address[:10], str(e)[:60])
        sw.prime()
        makers.append(sw)

    # taker accounts funded from the vault (perp-withdraw -> spot -> transfer -> deposit)
    takers = [PerpDexClient(cfg["rpc_url"], k["key"], cfg["dex_address"])
              for k in load_or_create(keystore, "vtaker", n_take)]
    # only top up the shortfall; withdraw only what the vault's spot is missing
    shortfall = sum(max(0.0, taker_deposit - _to_int(t.perp_account().get("walletBalance")) / 1e18) for t in takers)
    spot = _to_int(vault._rpc("eth_getTokenBalances", [vault.address, "latest"]).get("available", {}).get("2", "0")) / 1e18
    if shortfall > spot:
        try:
            vault._send(0x44, {"l1owner": vault.address, "token": "2", "amount": to_wei_str(shortfall - spot)})
        except Exception as e:
            print("vault withdraw warn (positions lock margin?):", str(e)[:70])
    for t in takers:
        if vault.w3.eth.get_balance(t.address) < vault.w3.to_wei(0.5, "ether"):
            tx = {"to": t.address, "value": vault.w3.to_wei(1, "ether"), "gas": 21000,
                  "nonce": vault.w3.eth.get_transaction_count(vault.address),
                  "chainId": vault.w3.eth.chain_id, "gasPrice": vault.w3.eth.gas_price}
            try:
                vault.w3.eth.wait_for_transaction_receipt(
                    vault.w3.eth.send_raw_transaction(vault.acct.sign_transaction(tx).raw_transaction))
            except Exception:
                pass
        wb = _to_int(t.perp_account().get("walletBalance")) / 1e18
        if wb < taker_deposit:
            vault.token_transfer(t.address, taker_deposit - wb)
            t.deposit(taker_deposit - wb)
        t.prime()
    print(f"ready: {n_sess} vault-maker sessions + {n_take} takers, target={target} tx/s")

    sent = [0] * (n_sess + n_take)
    stop = threading.Event()
    limiter = RateLimiter(target)

    def maker_loop(idx, sw):
        from collections import deque
        hq = deque(maxlen=400)
        j = 0
        while not stop.is_set():
            limiter.wait()
            try:
                if j % 60 == 0:
                    sw.cancel_all(mid, wait=False)
                    hq.clear()
                elif j % maker_cancel_every == 0 and hq:
                    sw.cancel(mid, hq.popleft(), wait=False)   # individual cancel
                elif j % 2 == 0:
                    hq.append(sw.order(mid, SELL, mk_ask, size, tif=POST, wait=False))
                else:
                    hq.append(sw.order(mid, BUY, mk_bid, size, tif=POST, wait=False))
                sent[idx] += 1
            except Exception:
                sw.resync_nonce()
            j += 1

    def taker_loop(idx, t):
        gidx = n_sess + idx
        j = 0
        while not stop.is_set():
            limiter.wait()
            try:
                if j % cross_every == 0:                        # crossing -> fill
                    t.order(mid, BUY if j % 2 == 0 else SELL, tk_buy if j % 2 == 0 else tk_sell,
                            size, tif=IOC, wait=False)
                else:                                           # non-crossing -> tx load only
                    t.order(mid, BUY, tk_mid, size, tif=IOC, wait=False)
                sent[gidx] += 1
            except Exception:
                t.resync_nonce()
            j += 1

    threads = [threading.Thread(target=maker_loop, args=(i, sw), daemon=True) for i, sw in enumerate(makers)]
    threads += [threading.Thread(target=taker_loop, args=(i, t), daemon=True) for i, t in enumerate(takers)]
    t0 = time.time()
    [t.start() for t in threads]
    try:
        while duration <= 0 or time.time() - t0 < duration:
            time.sleep(5)
            el = time.time() - t0
            vpos = vault.positions()
            vp = next((_to_int(p["size"]) / 1e18 for p in vpos if int(p["marketId"]) == mid), 0.0)
            print(f"t={el:.0f}s tx={sum(sent)} tx_rate={sum(sent) / el:.1f}/s vault_pos={vp:+.3f}")
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(1)
    el = time.time() - t0
    vpos = vault.positions()
    vp = next((_to_int(p["size"]) / 1e18 for p in vpos if int(p["marketId"]) == mid), 0.0)
    print(f"DONE: {sum(sent)} tx in {el:.0f}s = {sum(sent) / el:.1f} tx/s; vault net position={vp:+.3f}")


if __name__ == "__main__":
    main()
