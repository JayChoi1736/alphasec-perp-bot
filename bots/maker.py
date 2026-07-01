#!/usr/bin/env python3
"""Maker bot — posts a multi-level ladder of quotes around mid, inside the band.

A "tick" is one loop pass: read mark/book -> (re)quote -> sleep `interval`.

Two things keep the book rich and stable:
  - `levels` price levels per side (a ladder), not a single quote.
  - requote only when mid drifts past half a level step, so a quiet market
    leaves the full ladder resting instead of cancel/repost flicker every tick.

  python -m bots.maker [configs/config.json]    (reads the 'maker' role block)
"""
import sys
import time

from lib.dex import PerpDexClient, BUY, SELL, POST, DEFAULT_BAND_BPS, band_bounds, load_role_config
from lib.strategy import q, clamp_band, maker_step


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.json"
    cfg = load_role_config(path, "maker")
    c = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    mid = cfg["market_id"]
    lev = int(cfg["leverage"])

    mi = c.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    lot = mi["lot"] if mi else float(cfg.get("lot_size", 0.001))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    spread, interval = float(cfg["spread"]), float(cfg["interval"])
    levels = int(cfg.get("levels", 1))
    step = float(cfg.get("level_step", spread))  # price gap between ladder levels (fraction)
    size = q(float(cfg["order_size"]), lot)
    print(f"maker {c.address} market={mid} tick={tick} lot={lot} band={band_bps}bps "
          f"levels={levels} step={step} size={size}")

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
    need = size * ref / lev * 2 * levels  # both sides, all levels
    if wallet - om < need:
        sys.exit(f"insufficient margin: free={wallet - om:.2f} need~{need:.2f} "
                 f"(set maker.deposit>0 or fund {c.address})")
    print(f"preflight ok: wallet={wallet:.2f} free={wallet - om:.2f} need~{need:.2f}")

    tps = cfg.get("tps")
    if tps:  # load mode: fire-and-forget ladder, paced to target submit rate
        c.prime()
        dt = 1.0 / float(tps)
        mark = c.mark_price(mid) or float(cfg["ref_price"])
        lo, hi = band_bounds(mark, band_bps)
        cycle = [("c", 0, 0)] + [(s, k, 1) for k in range(levels) for s in (BUY, SELL)]
        submitted, t0, nxt = 0, time.time(), time.time()
        print(f"LOAD mode: target {tps} tx/s, ladder {levels}x2 + cancel per cycle")
        while True:
            for kind, k, _ in cycle:
                try:
                    if kind == "c":
                        c.cancel_all(mid, wait=False)
                    else:
                        off = spread + k * step
                        px = clamp_band(q(mark * (1 + (off if kind == SELL else -off)), tick), tick, lo, hi)
                        c.order(mid, kind, px, size, tif=POST, wait=False)
                    submitted += 1
                except Exception as e:
                    print("submit err:", e)
                    c.resync_nonce()
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

    state = {}  # holds 'last_mid' across ticks (skip requote while quiet)
    while True:
        try:
            r = maker_step(c, mid, size, spread, step, levels, tick, band_bps, float(cfg["ref_price"]), state)
            if r:  # None = skipped requote (market quiet -> ladder stays resting)
                mark, mid_px = r
                print(f"requote mark={mark} mid={mid_px:.2f} levels={levels} "
                      f"bid0={q(mid_px * (1 - spread), tick)} ask0={q(mid_px * (1 + spread), tick)}")
        except Exception as e:  # ponytail: log-and-continue; no reconnect framework
            print("tick error:", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
