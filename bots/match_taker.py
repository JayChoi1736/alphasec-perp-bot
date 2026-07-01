#!/usr/bin/env python3
"""Taker side of the matched load, as a standalone process (split from match.py).

N taker accounts cross the book at a target tx/s while keeping each position
within +/- inventory_cap (crosses toward zero), so margin never blows up over
long runs — same behavior as match.py's taker half. A background poller snapshots
positions each second and counts fills. Crossing orders fire only every
`taker_cross_every` op; the rest are non-crossing IOCs at mid (pure tx load).

Fills only land against a resting book, so run `match_maker.py` (or any maker) on
the same market. Reuses the `maker` config block (same keys as match.py) and the
`taker` keystore group.

  python -m bots.match_taker [configs/config.json] [target_tx_s] [duration_s]

duration 0 = run until Ctrl-C.
"""
import math
import sys
import threading
import time

from lib.accounts import load_or_create, ensure_funded
from lib.dex import (PerpDexClient, RateLimiter, BUY, SELL, IOC, DEFAULT_BAND_BPS,
                 band_bounds, load_role_config, _to_int)
from lib.strategy import q as sq, taker_price


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "configs/config.json"
    cfg = load_role_config(path, "maker")
    target = float(sys.argv[2]) if len(sys.argv) > 2 else float(cfg.get("match_tps", 20))  # taker tx/s
    duration = float(sys.argv[3]) if len(sys.argv) > 3 else float(cfg.get("match_duration", 0))
    per_acct = float(cfg.get("per_account_tps", 4))
    lev = int(cfg["leverage"])
    deposit = float(cfg.get("match_deposit", 100000))
    size = float(cfg.get("order_size", 0.01))
    cap = float(cfg.get("inventory_cap", 0.5))        # max |position| per account (base units)
    slip = float(cfg.get("taker_slippage", 0.01))
    cross_every = max(1, int(cfg.get("taker_cross_every", 1)))
    mid = cfg["market_id"]
    n = max(1, math.ceil(target / per_acct))  # takers needed for the target rate

    dev = PerpDexClient(cfg["rpc_url"], cfg["private_key"], cfg["dex_address"])
    mi = dev.market_info(mid)
    tick = mi["tick"] if mi else float(cfg.get("tick_size", 1))
    band_bps = (mi["price_band_bps"] if mi else 0) or DEFAULT_BAND_BPS
    mark = dev.mark_price(mid) or float(cfg["ref_price"])
    lo, hi = band_bounds(mark, band_bps)
    tk_buy = taker_price(BUY, mark, slip, tick, lo, hi)
    tk_sell = taker_price(SELL, mark, slip, tick, lo, hi)
    tk_mid = sq(mark, tick)  # between bid and ask -> a non-crossing IOC (tx load, no fill)
    print(f"match_taker: target={target} tx/s -> {n} takers (~{n * per_acct:.0f} tx/s) "
          f"market={mid} mark={mark} cap=+/-{cap} cross_every={cross_every} "
          f"tkbuy/sell={tk_buy}/{tk_sell}")

    keystore = cfg.get("keystore", "keystores/accounts.json")
    takers = [PerpDexClient(cfg["rpc_url"], k["key"], cfg["dex_address"]) for k in load_or_create(keystore, "taker", n)]
    ensure_funded(dev, takers, gas_eth=float(cfg.get("fund_gas_eth", 1.0)), deposit=deposit)

    def prep(a):
        try:
            a.cancel_all(mid)
            a.set_leverage(mid, lev)
            a.prime()
        except Exception as e:
            print("prep warn", a.address[:10], e)
    ts = [threading.Thread(target=prep, args=(a,)) for a in takers]
    [t.start() for t in ts]
    [t.join() for t in ts]
    print(f"ready {len(takers)} taker accounts ({deposit} deposit target each)")

    pos = [0.0] * len(takers)   # cached positions (base units)
    fills = [0.0]               # cumulative |position change|
    sent = [0] * len(takers)
    stop = threading.Event()
    limiter = RateLimiter(target)

    def acct_pos(a):
        for p in a.positions():
            if int(p["marketId"]) == mid:
                return _to_int(p["size"]) / 1e18
        return 0.0

    def poller():
        # baseline from real starting positions so leftover inventory on persistent
        # accounts isn't miscounted as fills
        prev = [acct_pos(a) for a in takers]
        for i in range(len(takers)):
            pos[i] = prev[i]
        while not stop.is_set():
            for i, a in enumerate(takers):
                try:
                    cur = acct_pos(a)
                except Exception:
                    continue
                pos[i] = cur
                fills[0] += abs(cur - prev[i])
                prev[i] = cur
            time.sleep(1)

    def taker_loop(idx, a):
        # Cross (fill) only every `cross_every` order so the trade rate is tunable;
        # the rest are non-crossing IOCs at mid. Crossing sweeps the position
        # +cap <-> -cap (triangle wave) for exact fill counting.
        d, j = BUY, 0
        while not stop.is_set():
            limiter.wait()
            try:
                if j % cross_every == 0:           # crossing -> fill
                    p = pos[idx]
                    if p >= cap:
                        d = SELL
                    elif p <= -cap:
                        d = BUY
                    a.order(mid, d, tk_buy if d == BUY else tk_sell, size, tif=IOC, wait=False)
                else:                              # non-crossing -> pure tx load
                    a.order(mid, BUY, tk_mid, size, tif=IOC, wait=False)
                sent[idx] += 1
            except Exception:
                a.resync_nonce()
            j += 1

    threads = [threading.Thread(target=poller, daemon=True)]
    threads += [threading.Thread(target=taker_loop, args=(i, a), daemon=True) for i, a in enumerate(takers)]
    t0 = time.time()
    [t.start() for t in threads]
    try:
        while duration <= 0 or time.time() - t0 < duration:
            time.sleep(5)
            el = time.time() - t0
            inv = pos[:]
            print(f"t={el:.0f}s tx={sum(sent)} tx_rate={sum(sent) / el:.1f}/s "
                  f"trades={fills[0] / size:.0f} trade_rate={fills[0] / size / el:.1f}/s "
                  f"taker_inv=[{min(inv):+.2f},{max(inv):+.2f}] (cap +/-{cap})")
    except KeyboardInterrupt:
        pass
    stop.set()
    time.sleep(1.5)
    el = time.time() - t0
    print(f"DONE: {sum(sent)} tx in {el:.0f}s = {sum(sent) / el:.1f} tx/s; "
          f"{fills[0] / size:.0f} fills = {fills[0] / size / el:.1f} trades/s; "
          f"final taker_inv range [{min(pos):+.2f},{max(pos):+.2f}]")


if __name__ == "__main__":
    main()
