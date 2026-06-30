#!/usr/bin/env python3
"""Price-crash walker — drive mark down to index*frac (default 1/5) via orders+fills.

Mark = median(S1, S2, S3) with S1 = index + EMA_150s(midBook - index),
S2 = median(bestBid, bestAsk, lastTrade), S3 = cexPerp (oracle, stays high).
A sustained LOW book mid drags S1 (over ~150s) and S2 down; once two signals
are below the oracle, the median (and mark) follow — no oracle write needed.

Each tick: a seller IOC-sells through resting bids above the target step (so the
best bid drops), then the funder re-posts a low two-sided book just inside the
±band. The book mid is ratcheted down only as fast as the band (around the
falling mark) allows, so the EMA paces the descent.

  python crash.py [config.json] [market_id] [target_frac] [max_seconds]

config knobs (maker block): crash_size, crash_step, crash_deposit, crash_lev.
Run SOLO on the market (no concurrent match.py). The funder absorbs the long
side (needs large margin); the seller shorts into the crash (profits).
"""
import json
import sys
import time

from accounts import load_or_create, ensure_funded
from dex import (PerpDexClient, SELL, BUY, POST, IOC, DEFAULT_BAND_BPS,
                 band_bounds, load_role_config, _to_int)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    cfg = load_role_config(path, "maker")
    market = int(sys.argv[2]) if len(sys.argv) > 2 else int(cfg["market_id"])
    frac = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("crash_target_frac", 0.2))
    max_s = float(sys.argv[4]) if len(sys.argv) > 4 else float(cfg.get("crash_max_seconds", 0))

    dev = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])  # funder = liquidity/long side
    keystore = cfg.get("keystore", "accounts.json")
    seller = PerpDexClient(cfg["rpc_url"], load_or_create(keystore, "crash", 1)[0]["key"], cfg["dex_address"])
    ensure_funded(dev, [seller], gas_eth=float(cfg.get("fund_gas_eth", 1.0)),
                  deposit=float(cfg.get("crash_deposit", 500000)))
    lev = int(cfg.get("crash_lev", 20))
    for c in (dev, seller):
        try:
            c.set_leverage(market, lev)
        except Exception:
            pass
    seller.prime()

    mi = dev.market_info(market)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    q = lambda x: max(tick, round(x / tick) * tick)
    size = float(cfg.get("crash_size", 0.2))
    step = float(cfg.get("crash_step", 0.06))   # per-tick book drop, must be < band

    def oracle():
        o = [x for x in dev._rpc("arb_getOraclePrices", []) if x["marketId"] == market][0]
        return _to_int(o["markPrice"]) / 1e18, _to_int(o["indexPrice"]) / 1e18

    m0, i0 = oracle()
    target = q(i0 * frac)
    print(f"crash: market={market} mark={m0:.0f} index={i0:.0f} target={target:.0f} (idx*{frac}) "
          f"tick={tick} band={band_bps}bps size={size} step={step}")

    t0 = time.time()
    last_log = 0.0
    while True:
        m, i = oracle()
        if m <= target:
            print(f"REACHED: mark={m:.0f} <= target={target:.0f} in {time.time() - t0:.0f}s")
            break
        if max_s and time.time() - t0 > max_s:
            print(f"STOP (max_seconds): mark={m:.0f} target={target:.0f} after {time.time() - t0:.0f}s")
            break
        lo, hi = band_bounds(m, band_bps)
        desired = max(target, q(m * (1 - step)), q(lo + tick))   # book mid, inside band, not below target
        try:
            # 1) sell through any bids at/above the desired level (drop best bid)
            seller.order(market, SELL, desired, size, tif=IOC, wait=False)
            # 2) re-post a low two-sided book just under the falling mark
            dev.cancel_all(market)
            dev.order(market, BUY, q(desired - tick), size, tif=POST)
            dev.order(market, SELL, q(desired + tick), size, tif=POST)
        except Exception as e:
            print("tick err:", str(e)[:90])
            seller.resync_nonce()
        if time.time() - last_log >= 10:
            b = dev.lvl2(market) or {}
            bb = max((float(x[0]) for x in b.get("bids", [])), default=0)
            print(f"t={time.time() - t0:.0f}s mark={m:.0f} index={i:.0f} bestbid={bb:.0f} "
                  f"pushing@{desired:.0f} target={target:.0f}")
            last_log = time.time()
        time.sleep(3)


if __name__ == "__main__":
    main()
