#!/usr/bin/env python3
"""Multi-account load driver — hit a target TPS the single-account ceiling can't.

A single sender is capped at ~1 tx/block (~4 tx/s on a 250ms dev node), so we
spread the target rate across N = ceil(tps / per_account_tps) ephemeral accounts,
each firing fire-and-forget IOC orders (cross the band, expire with no fill =
pure submission load, no margin/state growth). Accounts need only gas.

  python load.py [config.json] [target_tps] [duration_s]

config: top-level rpc_url/dex_address/market_id/ref_price; optional
`load_tps`, `per_account_tps` (default 4), `accounts_seed`. Funding signer is
the maker key (the dev account).
"""
import math
import sys
import threading
import time

from web3 import Web3

from accounts import load_or_create, ensure_funded
from dex import PerpDexClient, BUY, SELL, IOC, DEFAULT_BAND_BPS, band_bounds, load_role_config


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_role_config(path, "maker")  # maker key = funding signer
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("load_tps", 20))
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("load_duration", 0)) or None
    per_acct = float(cfg.get("per_account_tps", 4))
    keystore = cfg.get("keystore", "accounts.json")
    n = max(1, math.ceil(target / per_acct))
    mid, slip = cfg["market_id"], float(cfg.get("taker_slippage", 0.01))

    dev = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    w3 = dev.w3
    mi = dev.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    mark = dev.mark_price(mid) or float(cfg["ref_price"])
    lo, hi = band_bounds(mark, band_bps)
    size = float(cfg.get("order_size", 0.01))
    print(f"load: target={target} tx/s -> {n} accounts x {per_acct}/s, market={mid} mark={mark}")

    # persistent keystore (generate missing) + top up gas before start (IOC needs no margin)
    keys = load_or_create(keystore, "load", n)
    accts = [PerpDexClient(cfg["rpc_url"], k["key"], cfg["dex_address"]) for k in keys]
    ensure_funded(dev, accts, gas_eth=1.0, deposit=0)
    for a in accts:
        a.prime()

    counts = [0] * n
    stop = threading.Event()
    dt = n / target  # each account paces at target/n

    def worker(idx, a):
        i, nxt = 0, time.time()
        while not stop.is_set():
            try:
                if i % 2 == 0:
                    px = min(math.floor(hi / tick) * tick - tick, round(mark * (1 + slip) / tick) * tick)
                    a.order(mid, BUY, px, size, tif=IOC, wait=False)
                else:
                    px = max(math.ceil(lo / tick) * tick + tick, round(mark * (1 - slip) / tick) * tick)
                    a.order(mid, SELL, px, size, tif=IOC, wait=False)
                counts[idx] += 1
            except Exception:
                a.resync_nonce()
            i += 1
            nxt += dt
            sl = nxt - time.time()
            if sl > 0:
                time.sleep(sl)

    threads = [threading.Thread(target=worker, args=(i, a), daemon=True) for i, a in enumerate(accts)]
    t0 = time.time()
    for t in threads:
        t.start()
    try:
        while duration is None or time.time() - t0 < duration:
            time.sleep(1)
            el = time.time() - t0
            print(f"submitted={sum(counts)} rate={sum(counts) / el:.1f}/s "
                  f"confirmed_blk={w3.eth.block_number}")
    except KeyboardInterrupt:
        pass
    stop.set()
    el = time.time() - t0
    print(f"DONE: {sum(counts)} submits in {el:.1f}s = {sum(counts) / el:.1f} tx/s across {n} accounts")


if __name__ == "__main__":
    main()
