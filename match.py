#!/usr/bin/env python3
"""Matched load — real fills (trade TPS), not just submissions.

Splits ephemeral accounts into maker/taker pairs: makers rest ASKs at mark,
takers IOC-BUY across them -> actual matches. Takers go long monotonically and
makers short, so the real fill count is exact: sum(taker long) / order_size.

  python match.py [config.json] [target_trades_s] [duration_s]

Fills need margin, so every account is gas-funded AND perp-funded (token 2
transfer + deposit + leverage). Funding signer is the maker key (dev account).
"""
import math
import sys
import threading
import time

from web3 import Web3

from dex import (PerpDexClient, BUY, SELL, POST, IOC, DEFAULT_BAND_BPS,
                 band_bounds, load_role_config, _to_int)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_role_config(path, "maker")
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("match_tps", 20))
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("match_duration", 60))
    per_acct = float(cfg.get("per_account_tps", 4))
    lev = int(cfg["leverage"])
    deposit = float(cfg.get("match_deposit", 50000))
    size = float(cfg.get("order_size", 0.01))
    mid, slip = cfg["market_id"], float(cfg.get("taker_slippage", 0.01))
    pairs = max(1, math.ceil(target / per_acct))

    dev = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    w3 = dev.w3
    mi = dev.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    mark = dev.mark_price(mid) or float(cfg["ref_price"])
    lo, hi = band_bounds(mark, band_bps)
    ask_px = round(mark / tick) * tick                          # makers rest here
    buy_px = min(math.floor(hi / tick) * tick - tick, round(mark * (1 + slip) / tick) * tick)
    print(f"match: target={target} trades/s -> {pairs} maker + {pairs} taker, "
          f"market={mid} mark={mark} ask@{ask_px} buy@{buy_px} size={size}")

    runid = str(int(time.time()))  # fresh accounts each run so positions start flat

    def mk(role, i):
        return PerpDexClient(cfg["rpc_url"], Web3.keccak(text=f"match-{runid}-{role}-{i}").hex(), cfg["dex_address"])
    makers = [mk("m", i) for i in range(pairs)]
    takers = [mk("t", i) for i in range(pairs)]

    # fund (sequential from the single dev account): gas + spot token 2 each
    gp, cid = w3.eth.gas_price, w3.eth.chain_id
    for a in makers + takers:
        if w3.eth.get_balance(a.address) < w3.to_wei(0.5, "ether"):
            tx = {"to": a.address, "value": w3.to_wei(1, "ether"), "gas": 21000,
                  "nonce": w3.eth.get_transaction_count(dev.address), "chainId": cid, "gasPrice": gp}
            w3.eth.wait_for_transaction_receipt(
                w3.eth.send_raw_transaction(dev.acct.sign_transaction(tx).raw_transaction))
        dev.token_transfer(a.address, deposit)  # wait=True

    # each account: deposit + leverage (parallel)
    def prep(a):
        try:
            a.deposit(deposit)
            a.set_leverage(mid, lev)
            a.prime()
        except Exception as e:
            print("prep warn", a.address[:10], e)
    ts = [threading.Thread(target=prep, args=(a,)) for a in makers + takers]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    print(f"funded+deposited {2 * pairs} accounts ({deposit} each)")

    stop = threading.Event()
    dt = pairs / target

    def maker_loop(a):
        nxt = time.time()
        while not stop.is_set():
            try:
                a.order(mid, SELL, ask_px, size, tif=POST, wait=False)
            except Exception:
                a.resync_nonce()
            nxt += dt
            sl = nxt - time.time()
            if sl > 0:
                time.sleep(sl)

    def taker_loop(a):
        nxt = time.time()
        while not stop.is_set():
            try:
                a.order(mid, BUY, buy_px, size, tif=IOC, wait=False)
            except Exception:
                a.resync_nonce()
            nxt += dt
            sl = nxt - time.time()
            if sl > 0:
                time.sleep(sl)

    threads = ([threading.Thread(target=maker_loop, args=(a,), daemon=True) for a in makers] +
               [threading.Thread(target=taker_loop, args=(a,), daemon=True) for a in takers])
    t0 = time.time()
    for t in threads:
        t.start()
    while time.time() - t0 < duration:
        time.sleep(5)
        long = sum(_to_int(p["size"]) for a in takers for p in a.positions()) / 1e18
        el = time.time() - t0
        print(f"t={el:.0f}s taker_long={long:.2f} fills={long / size:.0f} trade_rate={long / size / el:.1f}/s")
    stop.set()
    time.sleep(1)
    long = sum(_to_int(p["size"]) for a in takers for p in a.positions()) / 1e18
    short = -sum(_to_int(p["size"]) for a in makers for p in a.positions()) / 1e18
    el = time.time() - t0
    fills = long / size
    print(f"DONE: {fills:.0f} fills in {el:.0f}s = {fills / el:.1f} trades/s "
          f"(taker_long={long:.2f} == maker_short={short:.2f})")


if __name__ == "__main__":
    main()
