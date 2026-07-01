#!/usr/bin/env python3
"""Taker bot — crosses the spread IOC, alternating side, clamped inside the band.

  python -m bots.taker [configs/config.json]

Side alternates by tick counter (not random) so runs are reproducible.
Reads the 'taker' role block merged over the common config.
"""
import sys
import time

from lib.dex import PerpDexClient, BUY, SELL, IOC, DEFAULT_BAND_BPS, band_bounds, load_role_config
from lib.strategy import q, taker_price, taker_step


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.json"
    cfg = load_role_config(path, "taker")
    c = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    mid = cfg["market_id"]
    lev = int(cfg["leverage"])

    mi = c.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    lot = mi["lot"] if mi else float(cfg.get("lot_size", 0.001))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    size = q(float(cfg["order_size"]), lot)
    slip = float(cfg.get("taker_slippage", 0.01))
    interval = float(cfg["interval"])
    print(f"taker {c.address} market={mid} tick={tick} lot={lot} band={band_bps}bps size={size}")

    if float(cfg.get("deposit", 0)) > 0:
        c.deposit(cfg["deposit"])
    # clear stale orders first (leverage/margin changes reject with resting orders)
    for fn in (lambda: c.cancel_all(mid),
               lambda: c.set_leverage(mid, lev),
               lambda: c.set_margin_type(mid, int(cfg["margin_type"]))):
        try:
            fn()
        except Exception as e:
            print("setup warn:", e)

    wallet, om = c.margins()
    ref = c.mark_price(mid) or float(cfg["ref_price"])
    need = size * ref / lev
    if wallet - om < need:
        sys.exit(f"insufficient margin: free={wallet - om:.2f} need~{need:.2f} "
                 f"(set taker.deposit>0 or fund {c.address})")
    print(f"preflight ok: wallet={wallet:.2f} free={wallet - om:.2f} need~{need:.2f}")

    tps = cfg.get("tps")
    if tps:  # load mode: fire-and-forget IOC crosses, paced to target submit rate
        c.prime()
        dt = 1.0 / float(tps)
        mark = c.mark_price(mid) or float(cfg["ref_price"])
        lo, hi = band_bounds(mark, band_bps)
        submitted, t0, nxt = 0, time.time(), time.time()
        print(f"LOAD mode: target {tps} tx/s, alternating IOC crosses")
        i = 0
        while True:
            try:
                side = BUY if i % 2 == 0 else SELL
                c.order(mid, side, taker_price(side, mark, slip, tick, lo, hi), size, tif=IOC, wait=False)
                submitted += 1
            except Exception as e:
                print("submit err:", e)
                c.resync_nonce()
            i += 1
            nxt += dt
            sl = nxt - time.time()
            if sl > 0:
                time.sleep(sl)
            if submitted % max(1, int(float(tps))) == 0:
                el = time.time() - t0
                mark = c.mark_price(mid) or mark
                lo, hi = band_bounds(mark, band_bps)
                print(f"submitted={submitted} rate={submitted / el:.1f}/s "
                      f"confirmed_blk={c.w3.eth.block_number} mark={mark}")

    i = 0
    while True:
        try:
            side, px, mark = taker_step(c, mid, size, slip, tick, band_bps, i, float(cfg["ref_price"]))
            print(f"#{i} {'BUY' if side == BUY else 'SELL'} {size} @{px} (mark={mark})")
        except Exception as e:  # ponytail: log-and-continue
            print("tick error:", e)
        i += 1
        time.sleep(interval)


if __name__ == "__main__":
    main()
