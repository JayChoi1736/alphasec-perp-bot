#!/usr/bin/env python3
"""Maker side of the matched load, as a standalone process (split from match.py).

N maker accounts post a `match_levels`-deep ladder at a target tx/s, with
individual PERP_CANCEL churn and a cancel_all safety net — same behavior as
match.py's maker half. It produces **no fills on its own**: run `match_taker.py`
(or any taker) on the same market to cross the resting book.

Reuses the `maker` config block (same keys as match.py) and the `maker` keystore
group, so existing configs work unchanged.

  python -m bots.match_maker [configs/config.json] [target_tx_s] [duration_s]

duration 0 = run until Ctrl-C.
"""
import math
import sys
import threading
import time
from collections import deque

from lib.accounts import load_or_create, ensure_funded
from lib.dex import (PerpDexClient, RateLimiter, BUY, SELL, POST, DEFAULT_BAND_BPS,
                 load_role_config)
from lib.strategy import maker_prices


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.json"
    cfg = load_role_config(path, "maker")
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("match_tps", 20))  # maker tx/s
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("match_duration", 0))
    per_acct = float(cfg.get("per_account_tps", 4))  # node ceiling: ~1 tx/block per sender
    lev = int(cfg["leverage"])
    deposit = float(cfg.get("match_deposit", 100000))
    size = float(cfg.get("order_size", 0.01))
    spread = float(cfg.get("spread", 0.0005))
    mid = cfg["market_id"]
    n = max(1, math.ceil(target / per_acct))  # makers needed for the target rate

    dev = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    mi = dev.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    mark = dev.mark_price(mid) or float(cfg["ref_price"])
    # maker ladder: match_levels price points per side (richer-looking book)
    mlevels = int(cfg.get("match_levels", 1))
    mstep = float(cfg.get("level_step", 0.001))
    mk_bids, mk_asks = maker_prices(mark, spread, mstep, mlevels, tick)
    mk_ladder = [(SELL, px) for px in mk_asks] + [(BUY, px) for px in mk_bids]
    maker_cancel_every = max(1, int(cfg.get("maker_cancel_every", 6)))
    print(f"match_maker: target={target} tx/s -> {n} makers (~{n * per_acct:.0f} tx/s) "
          f"market={mid} mark={mark} levels={mlevels} "
          f"bids={mk_bids[0]}..{mk_bids[-1]} asks={mk_asks[0]}..{mk_asks[-1]}")

    keystore = cfg.get("keystore", "keystores/accounts.json")
    makers = [PerpDexClient(cfg["rpc_url"], k["key"], cfg["dex_address"]) for k in load_or_create(keystore, "maker", n)]
    ensure_funded(dev, makers, gas_eth=float(cfg.get("fund_gas_eth", 1.0)), deposit=deposit)

    def prep(a):  # clear stale orders from prior runs, set leverage, prime nonce
        try:
            a.cancel_all(mid)
            a.set_leverage(mid, lev)
            a.prime()
        except Exception as e:
            print("prep warn", a.address[:10], e)
    ts = [threading.Thread(target=prep, args=(a,)) for a in makers]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print(f"ready {len(makers)} maker accounts ({deposit} deposit target each)")

    sent = [0] * len(makers)
    stop = threading.Event()
    limiter = RateLimiter(target)  # steady aggregate target tx/s across all makers

    def maker_loop(idx, a):
        # Post the ladder and cancel individual tracked orders (PERP_CANCEL by
        # tx-hash) for realistic place/cancel churn; an occasional cancel_all is a
        # safety net so the idle side can't pile up and lock the maker's margin.
        hq = deque(maxlen=400)  # recent order ids (tx hashes) to cancel
        j = 0
        while not stop.is_set():
            limiter.wait()
            try:
                if j % 60 == 0:
                    a.cancel_all(mid, wait=False)
                    hq.clear()
                elif j % maker_cancel_every == 0 and hq:
                    a.cancel(mid, hq.popleft(), wait=False)
                else:
                    side, px = mk_ladder[j % len(mk_ladder)]
                    hq.append(a.order(mid, side, px, size, tif=POST, wait=False))
                sent[idx] += 1
            except Exception:
                a.resync_nonce()
            j += 1

    threads = [threading.Thread(target=maker_loop, args=(i, a), daemon=True) for i, a in enumerate(makers)]
    t0 = time.time()
    [t.start() for t in threads]
    try:
        while duration <= 0 or time.time() - t0 < duration:
            time.sleep(5)
            el = time.time() - t0
            print(f"t={el:.0f}s tx={sum(sent)} tx_rate={sum(sent) / el:.1f}/s "
                  f"blk={dev.w3.eth.block_number}")
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(1.5)
    el = time.time() - t0
    print(f"DONE: {sum(sent)} tx in {el:.0f}s = {sum(sent) / el:.1f} tx/s")


if __name__ == "__main__":
    main()
